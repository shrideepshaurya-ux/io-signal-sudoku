"""Constraint-based IO allocation (OR-Tools CP-SAT).

Model
-----
* Unit of placement is a *cable block* (ordered pairs of one cable) or a
  stand-alone signal. Blocks are assigned to CIOCs; each CIOC is a linear strip
  of ``baseplates * charms`` positions (reserved spare baseplates are the tail).
* Hard constraints in the CP model: one CIOC per block, CIOC capacity, A/B
  segregation (per CIOC or cabinet), hard grouping (same cabinet / CIOC).
* Cable continuity/pair order, one-signal-per-slot and baseplate/CHARM
  geometry are guaranteed by a deterministic, exact layout step that cannot
  fail when the per-CIOC capacity sum holds (see ``_layout``).
* Soft objectives: unplaced slots (dominant), movement from a previous
  revision, cabinets used, CIOCs used, group spread.

The result is *always* re-checked by ``validator.validate`` - the solver's own
status is never trusted.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ortools.sat.python import cp_model

from .cable_model import Block, annotate_blocks, build_blocks, derive_cable_sizes
from .hardware import Topology
from .models import AllocationResult, Finding, Placement, RuleConfig, Signal, now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _order_key(b: Block):
    g = sorted(b.groups)[0] if b.groups else "￿"
    return (g, b.cable_label, b.bid)


def _partners(cfg: RuleConfig) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if cfg.segregation.enabled:
        for p, q in cfg.segregation.conflict_pairs:
            out.setdefault(p, set()).add(q)
            out.setdefault(q, set()).add(p)
    return out


def _clusters(blocks: list[Block]) -> list[list[Block]]:
    """Connected components of blocks linked by sharing a logical group."""
    parent: dict[str, str] = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for b in blocks:
        gs = sorted(b.groups)
        for g in gs:
            parent.setdefault(g, g)
        for g in gs[1:]:
            ra, rb = find(gs[0]), find(g)
            if ra != rb:
                parent[rb] = ra
    comp: dict[str, list[Block]] = {}
    free: list[list[Block]] = []
    for b in blocks:
        if b.groups:
            comp.setdefault(find(sorted(b.groups)[0]), []).append(b)
        else:
            free.append([b])
    return list(comp.values()) + free


# ---------------------------------------------------------------------------
# greedy placement (hint + fallback)
# ---------------------------------------------------------------------------
class _Greedy:
    def __init__(self, ciocs: list[tuple[int, int]], caps: list[int], cfg: RuleConfig):
        self.ciocs, self.cap_left = ciocs, list(caps)
        self.cfg = cfg
        self.partner = _partners(cfg)
        self.seg_scope = cfg.segregation.scope
        self.seg_c: list[set[str]] = [set() for _ in ciocs]
        self.seg_k: dict[int, set[str]] = {}
        self.g_cab: dict[str, int] = {}
        self.g_cioc: dict[str, int] = {}
        self.g_count: dict[str, int] = {}
        self.assign: dict[int, int] = {}
        gr = cfg.grouping
        self.hard_cab = gr.enabled and gr.mode == "hard" and gr.scope == "cabinet"
        self.hard_cioc = gr.enabled and gr.mode == "hard" and gr.scope == "cioc"

    def can_place(self, blk: Block, c: int) -> bool:
        if blk.eff > self.cap_left[c]:
            return False
        cab = self.ciocs[c][0]
        for v in blk.seg:
            for u in self.partner.get(v, ()):
                present = self.seg_c[c] if self.seg_scope == "cioc" else self.seg_k.get(cab, set())
                if u in present:
                    return False
        if self.hard_cab:
            for g in blk.groups:
                if g in self.g_cab and self.g_cab[g] != cab:
                    return False
        if self.hard_cioc:
            for g in blk.groups:
                if g in self.g_cioc and self.g_cioc[g] != c:
                    return False
        return True

    def place(self, blk: Block, c: int):
        cab = self.ciocs[c][0]
        undo = (blk.bid, c, blk.eff, [], [], [])
        self.cap_left[c] -= blk.eff
        self.assign[blk.bid] = c
        for v in blk.seg:
            if v not in self.seg_c[c]:
                self.seg_c[c].add(v); undo[3].append(("c", c, v))
            ks = self.seg_k.setdefault(cab, set())
            if v not in ks:
                ks.add(v); undo[3].append(("k", cab, v))
        for g in blk.groups:
            self.g_count[g] = self.g_count.get(g, 0) + 1
            undo[4].append(g)
            if g not in self.g_cab:
                self.g_cab[g] = cab; undo[5].append(("cab", g))
            if g not in self.g_cioc:
                self.g_cioc[g] = c; undo[5].append(("cioc", g))
        return undo

    def rollback(self, undo):
        bid, c, eff, segs, gcnt, gset = undo
        self.cap_left[c] += eff
        self.assign.pop(bid, None)
        for kind, a, v in segs:
            (self.seg_c[a] if kind == "c" else self.seg_k[a]).discard(v)
        for g in gcnt:
            self.g_count[g] -= 1
        for kind, g in gset:
            (self.g_cab if kind == "cab" else self.g_cioc).pop(g, None)

    def _cioc_order(self, blk: Block, pref_cabs: set[int]) -> list[int]:
        idx = range(len(self.ciocs))
        if not pref_cabs:
            return list(idx)
        return sorted(idx, key=lambda c: (self.ciocs[c][0] not in pref_cabs, c))

    def run(self, blocks: list[Block], prev_c: dict[int, int]) -> list[int]:
        """Return list of unplaced block ids."""
        unplaced: list[int] = []
        by_id = {b.bid: b for b in blocks}
        # honour previous locations first
        for bid, c in prev_c.items():
            b = by_id[bid]
            if self.can_place(b, c):
                self.place(b, c)
        clusters = _clusters([b for b in blocks if b.bid not in self.assign])
        # clusters that already have placed members stay in their cabinet
        clusters.sort(key=lambda cl: -sum(b.eff for b in cl))
        for cl in clusters:
            cl = sorted(cl, key=lambda b: (-b.eff, _order_key(b)))
            if self.hard_cab and any(b.groups for b in cl):
                unplaced += self._place_cluster_cab(cl)
            else:
                for b in cl:
                    pref = {self.g_cab[g] for g in b.groups if g in self.g_cab}
                    for c in self._cioc_order(b, pref):
                        if self.can_place(b, c):
                            self.place(b, c)
                            break
                    else:
                        unplaced.append(b.bid)
        return unplaced

    def _place_cluster_cab(self, cl: list[Block]) -> list[int]:
        fixed = {self.g_cab[g] for b in cl for g in b.groups if g in self.g_cab}
        cabs = sorted({k for k, _ in self.ciocs})
        cand = sorted(fixed) if fixed else cabs
        for cab in cand:
            undo_log, ok = [], True
            members = [c for c, (k, _) in enumerate(self.ciocs) if k == cab]
            for b in cl:
                for c in members:
                    if self.can_place(b, c):
                        undo_log.append(self.place(b, c))
                        break
                else:
                    ok = False
                    break
            if ok:
                return []
            for u in reversed(undo_log):
                self.rollback(u)
        return [b.bid for b in cl]


# ---------------------------------------------------------------------------
# CP-SAT assignment
# ---------------------------------------------------------------------------
def _solve_cpsat(blocks: list[Block], ciocs: list[tuple[int, int]], caps: list[int],
                 prev_c: dict[int, int], hint: dict[int, int], cfg: RuleConfig,
                 time_limit: float) -> tuple[dict[int, int], dict]:
    w = cfg.weights
    m = cp_model.CpModel()
    nc = len(ciocs)
    cabs = sorted({k for k, _ in ciocs})
    cab_of = [k for k, _ in ciocs]
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    by_c: list[list[Block]] = [[] for _ in range(nc)]
    placed = {}
    for b in blocks:
        row = []
        for c in range(nc):
            if b.eff <= caps[c]:
                v = m.NewBoolVar(f"x{b.bid}_{c}")
                x[b.bid, c] = v
                by_c[c].append(b)
                row.append(v)
        p = m.NewBoolVar(f"p{b.bid}")
        m.Add(sum(row) == p) if row else m.Add(p == 0)
        placed[b.bid] = p
    used = [m.NewBoolVar(f"u{c}") for c in range(nc)]
    cab_used = {k: m.NewBoolVar(f"k{k}") for k in cabs}
    for c in range(nc):
        if by_c[c]:
            m.Add(sum(b.eff * x[b.bid, c] for b in by_c[c]) <= caps[c])
            for b in by_c[c]:
                m.AddImplication(x[b.bid, c], used[c])
        else:
            m.Add(used[c] == 0)
        m.AddImplication(used[c], cab_used[cab_of[c]])
    # segregation ---------------------------------------------------------
    partner = _partners(cfg)
    if partner:
        scope_units = list(range(nc)) if cfg.segregation.scope == "cioc" else cabs
        unit = (lambda c: c) if cfg.segregation.scope == "cioc" else (lambda c: cab_of[c])
        present = {v for b in blocks for v in b.seg if v in partner}
        has = {(v, u): m.NewBoolVar(f"h_{v}_{u}") for v in present for u in scope_units}
        for b in blocks:
            for v in b.seg & present:
                for c in range(nc):
                    if (b.bid, c) in x:
                        m.AddImplication(x[b.bid, c], has[v, unit(c)])
        for p, q in cfg.segregation.conflict_pairs:
            if p in present and q in present:
                for u in scope_units:
                    m.AddBoolOr([has[p, u].Not(), has[q, u].Not()])
    # grouping --------------------------------------------------------------
    obj = []
    gr = cfg.grouping
    if gr.enabled:
        gblocks: dict[str, list[Block]] = {}
        for b in blocks:
            for g in b.groups:
                gblocks.setdefault(g, []).append(b)
        for g, bl in gblocks.items():
            if len(bl) < 2:
                continue
            gk = {k: m.NewBoolVar(f"gk_{g}_{k}") for k in cabs}
            for b in bl:
                for c in range(nc):
                    if (b.bid, c) in x:
                        m.AddImplication(x[b.bid, c], gk[cab_of[c]])
            if gr.mode == "hard" and gr.scope == "cabinet":
                m.Add(sum(gk.values()) <= 1)
            else:
                obj += [w.group_cabinet_span * v for v in gk.values()]
            gc = {c: m.NewBoolVar(f"gc_{g}_{c}") for c in range(nc)}
            for b in bl:
                for c in range(nc):
                    if (b.bid, c) in x:
                        m.AddImplication(x[b.bid, c], gc[c])
            if gr.mode == "hard" and gr.scope == "cioc":
                m.Add(sum(gc.values()) <= 1)
            else:
                obj += [w.group_cioc_span * v for v in gc.values()]
    # objective ---------------------------------------------------------------
    for b in blocks:
        obj.append(w.unplaced_per_slot * b.size * (1 - placed[b.bid]))
        pc = prev_c.get(b.bid)
        if pc is not None and (b.bid, pc) in x:
            obj.append(w.movement_per_slot * b.size * (placed[b.bid] - x[b.bid, pc]))
    obj += [w.cabinet * v for v in cab_used.values()]
    obj += [w.cioc * v for v in used]
    m.Minimize(sum(obj))
    if not prev_c:  # symmetry breaking only when nothing anchors positions
        for i in range(len(cabs) - 1):
            m.AddImplication(cab_used[cabs[i + 1]], cab_used[cabs[i]])
        for c in range(nc - 1):
            if cab_of[c] == cab_of[c + 1]:
                m.AddImplication(used[c + 1], used[c])
    for (bid, c), v in x.items():
        m.AddHint(v, 1 if hint.get(bid) == c else 0)
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = float(time_limit)
    s.parameters.num_workers = max(1, min(cfg.solver.workers, os.cpu_count() or 1))
    s.parameters.random_seed = cfg.solver.seed
    t0 = time.time()
    st = s.Solve(m)
    info = {"cpsat_status": s.StatusName(st), "wall_time_s": round(time.time() - t0, 3),
            "num_bool_vars": len(x), "num_candidate_ciocs": nc}
    if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        info["objective"] = s.ObjectiveValue()
        info["best_bound"] = s.BestObjectiveBound()
        info["conflicts"] = s.NumConflicts()
        info["branches"] = s.NumBranches()
        asg = {}
        for (bid, c), v in x.items():
            if s.Value(v):
                asg[bid] = c
        return asg, info
    return {}, info


# ---------------------------------------------------------------------------
# layout inside one CIOC
# ---------------------------------------------------------------------------
def _fits_geometry(start: int, size: int, charms: int, aligned: bool) -> bool:
    if not aligned:
        return True
    if size > charms:
        return start % charms == 0
    return start // charms == (start + size - 1) // charms


def _layout(blocks: list[Block], usable: int, cfg: RuleConfig, charms: int,
            pins: dict[int, int]) -> Optional[dict[int, int]]:
    aligned = cfg.cable.enabled and cfg.cable.continuity == "baseplate_aligned"
    free = [True] * usable
    start_of: dict[int, int] = {}
    for bid, st in pins.items():
        blk = next(b for b in blocks if b.bid == bid)
        start_of[bid] = st
        for i in range(st, st + blk.size):
            free[i] = False
    cursor = 0
    for b in sorted((b for b in blocks if b.bid not in start_of), key=_order_key):
        placed = False
        for lo in (cursor, 0):
            for st in range(lo, usable - b.size + 1):
                if all(free[st:st + b.size]) and _fits_geometry(st, b.size, charms, aligned):
                    start_of[b.bid] = st
                    for i in range(st, st + b.size):
                        free[i] = False
                    cursor = st + b.size
                    placed = True
                    break
            if placed:
                break
        if not placed:
            return None
    return start_of


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def _find(findings: list[Finding], code: str, severity: str, message: str, sids: list[int],
          by_sid: dict[int, Signal], needed: str = "", rules: Optional[list[str]] = None,
          **data) -> Finding:
    cables = sorted({by_sid[s].cable_id for s in sids if by_sid[s].cable_id})
    groups = sorted({by_sid[s].group for s in sids if by_sid[s].group})
    f = Finding(code=code, severity=severity, message=message, sids=list(sids),
                cable=", ".join(cables) or None, group=", ".join(groups) or None,
                needed=needed, rules=rules or [], data=data)
    findings.append(f)
    return f


def allocate(signals: list[Signal], cfg: RuleConfig, previous: Optional[dict[str, Placement]] = None,
             revision: str = "", progress: Optional[Callable[[str], None]] = None,
             time_limit: Optional[float] = None) -> AllocationResult:
    t_start = time.time()
    say = progress or (lambda m: None)
    topo = Topology(cfg.hardware, cfg.naming)
    by_sid = {s.sid: s for s in signals}
    findings: list[Finding] = []
    unalloc: dict[int, str] = {}
    previous = previous or {}
    charms = cfg.hardware.charms_per_baseplate

    # 1. input-level exclusions -------------------------------------------------
    for s in signals:
        errs = [f for f in s.findings if f.severity == "ERROR"]
        if errs:
            unalloc[s.sid] = "; ".join(f"{f.code}: {f.message}" for f in errs)
    for s in signals:   # re-validate compatibility against the *current* matrix
        if s.sid in unalloc:
            continue
        if s.io_key is None or s.io_key not in cfg.compat:
            _find(findings, "UNKNOWN_COMPAT", "ERROR",
                  f"No CHARM / terminal block mapping for IO type {s.io_raw!r} "
                  f"(key {s.io_key!r}); not assigned an arbitrary CHARM.", [s.sid], by_sid,
                  needed="Supply a CHARM and terminal block mapping.", rules=["R-CHARM"])
            unalloc[s.sid] = "UNKNOWN_COMPAT: no CHARM/terminal block mapping"

    elig = [s for s in signals if s.sid not in unalloc]
    partner = _partners(cfg)

    # 2. cable level: A/B conflict inside one cable --------------------------
    if partner:
        by_cable: dict[str, list[Signal]] = {}
        for s in elig:
            if s.cable_key and s.pair is not None:
                by_cable.setdefault(s.cable_key, []).append(s)
        for key, rows in by_cable.items():
            vals = {r.seg for r in rows if r.seg}
            bad = sorted((v, u) for v in vals for u in partner.get(v, ()) if u in vals and v < u)
            if bad:
                sids = [r.sid for r in rows]
                _find(findings, "SEG_CABLE_CONFLICT", "ERROR",
                      f"Cable {rows[0].cable_id} carries both {bad[0][0]} and {bad[0][1]} signals, "
                      "but A/B segregation forbids sharing a CIOC while cable continuity forbids "
                      "splitting the cable.", sids, by_sid,
                      needed="Decide whether the cable may be split across CIOCs, re-designate "
                             "segregation, or re-route pairs.",
                      rules=["R-SEG", "R-CABLE"])
                for sid in sids:
                    unalloc[sid] = "SEG_CABLE_CONFLICT: cable carries conflicting A/B signals"
        elig = [s for s in elig if s.sid not in unalloc]

    cable_sizes = derive_cable_sizes(signals, cfg)
    blocks = build_blocks(elig, cfg, cable_sizes)
    annotate_blocks(blocks, by_sid)

    # 3. capacity parameters ---------------------------------------------------
    n_target = cfg.reserved_baseplates()
    per_cab_slots = cfg.hardware.slots_per_cabinet
    max_cabs = cfg.hardware.max_cabinets
    demand = sum(b.eff for b in blocks)
    n_res = n_target
    if max_cabs is not None and cfg.spare.mode == "soft":
        while n_res > 0 and max_cabs * (per_cab_slots - n_res * charms) < demand:
            n_res -= 1
    cab_cap = per_cab_slots - n_res * charms
    cioc_usable = [topo.usable_baseplates(i, n_res) * charms
                   for i in range(cfg.hardware.ciocs_per_cabinet)]
    max_cioc = max(cioc_usable)

    def drop(bl: list[Block], code: str, msg: str, needed: str, rules: list[str]):
        sids = [s for b in bl for s in b.sids]
        _find(findings, code, "ERROR", msg, sids, by_sid, needed=needed, rules=rules)
        for s in sids:
            unalloc[s] = f"{code}: {msg}"

    keep: list[Block] = []
    for b in blocks:
        if b.eff > max_cioc:
            drop([b], "BLOCK_TOO_LARGE",
                 f"Cable {b.cable_label} needs {b.eff} positions on one CIOC but a CIOC offers "
                 f"at most {max_cioc} usable positions.",
                 "Confirm whether the cable may be split or raise hardware limits.",
                 ["R-CAP", "R-CABLE"])
        else:
            keep.append(b)
    blocks = keep
    gr = cfg.grouping
    if gr.enabled and gr.mode == "hard":
        limit = cab_cap if gr.scope == "cabinet" else max_cioc
        keep = []
        for cl in _clusters(blocks):
            gnames = sorted({g for b in cl for g in b.groups})
            tot = sum(b.eff for b in cl)
            if not gnames:
                keep += cl
                continue
            if tot > limit:
                drop(cl, "GROUP_TOO_LARGE",
                     f"Logical group(s) {', '.join(gnames)} need {tot} positions but a "
                     f"{gr.scope} offers {limit} (hard grouping).",
                     "Relax the grouping scope to 'soft', split the group, or change hardware limits.",
                     ["R-GRP", "R-CAP"])
                continue
            segs = {v for b in cl for v in b.seg}
            clash = sorted((v, u) for v in segs for u in partner.get(v, ()) if u in segs and v < u)
            if clash and (gr.scope == "cioc" or cfg.segregation.scope == "cabinet"):
                drop(cl, "GROUP_SEGREGATION_CONFLICT",
                     f"Group(s) {', '.join(gnames)} contain both {clash[0][0]} and {clash[0][1]} "
                     f"signals but must share one {gr.scope} while segregation applies per "
                     f"{cfg.segregation.scope}.",
                     "Decide which rule takes priority or re-designate the signals.",
                     ["R-GRP", "R-SEG"])
                continue
            keep += cl
        blocks = sorted(keep, key=lambda b: b.bid)

    solver_info: dict = {"cabinet_capacity_slots": cab_cap, "reserved_baseplates_per_cabinet": n_res,
                         "reserved_target": n_target, "blocks": len(blocks),
                         "demand_slots": sum(b.eff for b in blocks)}
    placements: dict[int, Placement] = {}
    status = "INVALID_INPUT"
    block_cioc: dict[int, tuple[int, int]] = {}
    unplaced_blocks: list[Block] = []

    if blocks:
        # 4. candidate CIOCs --------------------------------------------------
        total = sum(b.eff for b in blocks)
        lb = max(1, math.ceil(total / cab_cap))
        prev_cab = max([p.cab for p in previous.values()], default=-1) + 1
        k_all = max_cabs if max_cabs is not None else max(lb * 3 + 4, prev_cab + 2)
        cpc = cfg.hardware.ciocs_per_cabinet

        def make_bins(k):
            ciocs = [(a, i) for a in range(k) for i in range(cpc)]
            return ciocs, [cioc_usable[i] for _, i in ciocs]

        ciocs, caps = make_bins(k_all)
        ciocs_idx = {key: i for i, key in enumerate(ciocs)}
        by_tag_prev: dict[int, int] = {}
        prev_c: dict[int, int] = {}
        for b in blocks:
            votes: dict[int, int] = {}
            for sid in b.sids:
                p = previous.get(by_sid[sid].tag_key)
                if p is not None and (p.cab, p.cioc) in ciocs_idx:
                    votes[ciocs_idx[(p.cab, p.cioc)]] = votes.get(ciocs_idx[(p.cab, p.cioc)], 0) + 1
            if votes:
                prev_c[b.bid] = max(votes, key=votes.get)
        say("Greedy initial placement")
        tg = time.time()
        gre = _Greedy(ciocs, caps, cfg)
        gre_unplaced = gre.run(blocks, prev_c)
        used_cabs = sorted({ciocs[c][0] for c in gre.assign.values()})
        k_use = (max(used_cabs) + 1) if used_cabs else 1
        k_use = max(k_use, prev_cab if prev_c else 0)
        if max_cabs is not None:
            k_use = max_cabs if gre_unplaced else min(k_use, max_cabs)
        k_use = max(1, min(k_use, k_all))
        solver_info.update(greedy_time_s=round(time.time() - tg, 3), greedy_unplaced_blocks=len(gre_unplaced),
                           candidate_cabinets=k_use, lower_bound_cabinets=lb)
        ciocs, caps = make_bins(k_use)
        hint = {bid: c for bid, c in gre.assign.items() if c < len(ciocs)}
        prev_c = {bid: c for bid, c in prev_c.items() if c < len(ciocs)}
        n_vars = len(blocks) * len(ciocs)
        assign: dict[int, int] = {}
        tl = time_limit if time_limit is not None else cfg.solver.time_limit_s
        if n_vars <= cfg.solver.max_model_vars:
            say(f"CP-SAT: {len(blocks)} blocks x {len(ciocs)} CIOCs")
            assign, info = _solve_cpsat(blocks, ciocs, caps, prev_c, hint, cfg, tl)
            solver_info.update(info, method="CP-SAT")
            cps = info["cpsat_status"]
            if not assign and gre.assign:   # solver produced nothing: fall back
                assign = dict(hint)
                solver_info["method"] = "greedy (CP-SAT returned no solution)"
                cps = "UNKNOWN"
        else:
            assign = dict(hint)
            cps = "UNKNOWN"
            solver_info.update(method="greedy (model too large for CP-SAT limit)", cpsat_status="SKIPPED")
        # 5. layout -------------------------------------------------------------
        say("Laying out CHARM slots")
        per_c: dict[int, list[Block]] = {}
        bmap = {b.bid: b for b in blocks}
        for bid, c in assign.items():
            per_c.setdefault(c, []).append(bmap[bid])
        moved_for_layout = 0
        for c, bl in per_c.items():
            cab, ci = ciocs[c]
            usable = caps[c]
            pins: dict[int, int] = {}
            for b in bl:
                prevs = [previous.get(by_sid[s].tag_key) for s in b.sids]
                if any(p is None or (p.cab, p.cioc) != (cab, ci) for p in prevs):
                    continue
                st = topo.offset(prevs[0]) - b.offsets[0]
                if all(topo.offset(p) == st + o for p, o in zip(prevs, b.offsets)) \
                        and st >= 0 and st + b.size <= usable:
                    aligned = cfg.cable.enabled and cfg.cable.continuity == "baseplate_aligned"
                    if _fits_geometry(st, b.size, charms, aligned):
                        pins[b.bid] = st
            # drop overlapping pins
            occ, ok_pins = set(), {}
            for bid, st in sorted(pins.items(), key=lambda kv: kv[1]):
                rng = set(range(st, st + bmap[bid].size))
                if not (rng & occ):
                    occ |= rng
                    ok_pins[bid] = st
            lay = _layout(bl, usable, cfg, charms, ok_pins)
            if lay is None and ok_pins:
                lay = _layout(bl, usable, cfg, charms, {})
                moved_for_layout += len(ok_pins)
            if lay is None:   # should not happen; report instead of faking
                unplaced_blocks += bl
                continue
            for b in bl:
                block_cioc[b.bid] = (cab, ci)
                for sid, off in zip(b.sids, b.offsets):
                    bp, slot = topo.split_offset(lay[b.bid] + off)
                    placements[sid] = Placement(cab=cab, cioc=ci, bp=bp, slot=slot, block_id=b.bid)
        solver_info["pins_dropped_for_layout"] = moved_for_layout
        unplaced_blocks += [b for b in blocks if b.bid not in assign]
        unplaced_blocks = list({b.bid: b for b in unplaced_blocks}.values())

        # 6. explanations / findings for unplaced ----------------------------------
        avail = (max_cabs * cab_cap) if max_cabs is not None else None
        if unplaced_blocks:
            lost_slots = sum(b.size for b in unplaced_blocks)
            proven = solver_info.get("cpsat_status") == "OPTIMAL" and solver_info.get("method") == "CP-SAT"
            for b in unplaced_blocks:
                if proven:
                    msg = (f"{b.cable_label or by_sid[b.sids[0]].tag}: no feasible position under the "
                           f"enabled rules; demand {total} positions vs {avail} available in "
                           f"{max_cabs} cabinet(s) (reserved baseplates: {n_res}/cabinet).")
                    code, need = "INSUFFICIENT_CAPACITY", "Add cabinets, relax spare capacity, or relax grouping/segregation."
                else:
                    msg = ("Could not be placed within the current solver limits; this is not a "
                           "proof of infeasibility.")
                    code, need = "SOLVER_LIMIT", "Increase the time limit or review the rule set."
                _find(findings, code, "ERROR", msg, b.sids, by_sid, needed=need,
                      rules=["R-CAP", "R-GRP", "R-SEG"], lost_slots=lost_slots)
                for s in b.sids:
                    unalloc[s] = f"{code}: {msg}"
            status = "INFEASIBLE" if proven else "UNKNOWN"
        else:
            status = "OPTIMAL" if cps == "OPTIMAL" and solver_info.get("method") == "CP-SAT" else "FEASIBLE"
    # compat info + reasons ----------------------------------------------------
    for sid, p in placements.items():
        s = by_sid[sid]
        ce = cfg.compat[s.io_key]
        p.charm_type, p.terminal_block = ce.charm_type, ce.terminal_block
        p.configuration, p.compat_status = ce.configuration, ce.status
    block_by_id = {b.bid: b for b in blocks}
    for sid, p in placements.items():
        b = block_by_id[p.block_id]
        s = by_sid[sid]
        parts = [f"Cable {b.cable_label} pair {s.pair}: block of {len(b.sids)} signal(s) kept contiguous "
                 f"in {topo.cioc_name(p.cab, p.cioc)}" if b.cable_key else
                 "Stand-alone signal placed by capacity/segregation rules"]
        if s.group:
            parts.append(f"group {s.group} kept in {topo.system_cabinet(p.cab)}" if gr.enabled else
                         f"group {s.group}")
        if s.seg and partner:
            parts.append(f"segregation {s.seg}: CIOC holds no conflicting designation")
        prev = previous.get(s.tag_key)
        if prev is not None:
            parts.append("existing location preserved" if prev.loc == p.loc else "moved vs previous revision")
        p.reason = "; ".join(parts) + "."

    # cable block table ---------------------------------------------------------
    cable_rows: list[dict] = []
    all_by_cable: dict[str, list[Signal]] = {}
    for s in signals:
        if s.cable_key:
            all_by_cable.setdefault(s.cable_key, []).append(s)
    for key, rows in sorted(all_by_cable.items()):
        size = cable_sizes.get(key)
        placed_rows = [r for r in rows if r.sid in placements]
        locs = {(placements[r.sid].cab, placements[r.sid].cioc) for r in placed_rows}
        cable_rows.append({
            "Cable": rows[0].cable_id, "Cable size (pairs)": size, "Signals in MDB": len(rows),
            "Allocated": len(placed_rows), "Unallocated": len(rows) - len(placed_rows),
            "Spare pairs": (size - len(rows)) if size else None,
            "Cabinet(s)": ", ".join(topo.system_cabinet(c) for c, _ in sorted(locs)) if locs else "",
            "CIOC(s)": ", ".join(topo.cioc_name(c, i) for c, i in sorted(locs)) if locs else "",
        })
    complete = bool(signals) and len(placements) == len(signals)
    solver_info["runtime_s"] = round(time.time() - t_start, 3)
    spare = {"target_baseplates_per_cabinet": n_target, "achieved_baseplates_per_cabinet": n_res,
             "shortfall_baseplates_per_cabinet": n_target - n_res, "mode": cfg.spare.mode}
    if n_target - n_res > 0:
        _find(findings, "SPARE_SHORTFALL", "WARNING",
              f"Spare-capacity target of {n_target} empty baseplate(s) per cabinet could not be met: "
              f"only {n_res} reserved because the cabinet limit ({max_cabs}) leaves no further "
              "capacity.", [], by_sid, needed="Approve a lower spare target or add cabinets.",
              rules=["R-SPARE"], target=n_target, achieved=n_res)
    return AllocationResult(
        revision=revision, config_id=cfg.config_id, timestamp=now_iso(), signals=signals,
        placements=placements, unallocated={k: v for k, v in unalloc.items() if k not in placements},
        findings=findings, status=status, complete=complete, solver=solver_info,
        cable_blocks=cable_rows, spare=spare, config=cfg)
