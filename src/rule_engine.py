"""Rule registry and rule checks.

Every rule is a small class registered with ``@register``. A rule exposes its
ID, name, description, hard/soft nature, default severity, an ``enabled``
switch read from the profile, its configuration slice, and a ``check`` method
that returns :class:`Issue` objects (relevant records + human explanation).

Adding a rule
-------------
1. Subclass :class:`Rule`, set ``id``/``name``/``description``.
2. Implement ``is_enabled(cfg)`` and ``check(ctx)``.
3. Decorate with ``@register``. The validator, the Streamlit rule page and the
   report generator pick it up automatically.
Rules that also need to *influence the solver* must additionally be wired into
``allocator._solve_cpsat``; the registry only covers validation/explanation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .cable_model import derive_cable_sizes
from .hardware import Topology
from .models import Placement, RuleConfig, Signal


@dataclass
class Issue:
    rule_id: str
    severity: str                  # ERROR | WARNING | INFO
    message: str
    sids: list[int] = field(default_factory=list)
    cable: Optional[str] = None
    hardware: Optional[str] = None
    explanation: str = ""


@dataclass
class Context:
    signals: list[Signal]
    placements: dict[int, Placement]
    cfg: RuleConfig
    topo: Topology

    def __post_init__(self):
        self.by_sid = {s.sid: s for s in self.signals}


class Rule:
    id = "R-?"
    name = ""
    description = ""
    hard = True          # hard constraint vs preference
    severity = "ERROR"
    optional = True      # may be disabled by the user

    def is_enabled(self, cfg: RuleConfig) -> bool:
        return True

    def config(self, cfg: RuleConfig) -> dict[str, Any]:
        return {}

    def check(self, ctx: Context) -> list[Issue]:
        return []

    def meta(self, cfg: RuleConfig) -> dict[str, Any]:
        return {"Rule ID": self.id, "Name": self.name, "Type": "Hard" if self.hard else "Preference",
                "Enabled": self.is_enabled(cfg), "Severity": self.severity,
                "Description": self.description, "Configuration": self.config(cfg)}


REGISTRY: dict[str, Rule] = {}


def register(cls):
    inst = cls()
    REGISTRY[inst.id] = inst
    return cls


def validate_all(ctx: Context) -> tuple[list[Issue], dict[str, bool]]:
    issues: list[Issue] = []
    ran: dict[str, bool] = {}
    for rid, rule in REGISTRY.items():
        on = rule.is_enabled(ctx.cfg) or not rule.optional
        ran[rid] = on
        if on:
            issues += rule.check(ctx)
    return issues, ran


def _where(ctx: Context, sid: int) -> str:
    return ctx.topo.location_str(ctx.placements[sid])


# ---------------------------------------------------------------------------
@register
class CapacityRule(Rule):
    id, name = "R-CAP", "Hardware capacity"
    description = ("At most N CIOCs per cabinet, M baseplates per CIOC, K CHARM positions per "
                   "baseplate; one signal per position. Cannot be disabled.")
    optional = False

    def config(self, cfg):
        h = cfg.hardware
        return {"ciocs_per_cabinet": h.ciocs_per_cabinet, "baseplates_per_cioc": h.baseplates_per_cioc,
                "charms_per_baseplate": h.charms_per_baseplate, "max_cabinets": h.max_cabinets}

    def check(self, ctx):
        out, seen = [], {}
        hw = ctx.cfg.hardware
        for sid, p in ctx.placements.items():
            if not ctx.topo.in_bounds(p):
                out.append(Issue(self.id, "ERROR", f"Location out of hardware bounds: "
                                 f"cab {p.cab + 1}, CIOC {p.cioc + 1}, BP {p.bp + 1}, slot {p.slot + 1}.", [sid],
                                 explanation="Position exceeds configured CIOC/baseplate/CHARM limits."))
            if hw.max_cabinets is not None and p.cab >= hw.max_cabinets:
                out.append(Issue(self.id, "ERROR", f"Cabinet {p.cab + 1} exceeds the cabinet limit "
                                 f"{hw.max_cabinets}.", [sid]))
            if p.loc in seen:
                out.append(Issue(self.id, "ERROR", f"Slot {ctx.topo.location_str(p)} is assigned to "
                                 f"both {ctx.by_sid[seen[p.loc]].tag} and {ctx.by_sid[sid].tag}.",
                                 [seen[p.loc], sid], hardware=ctx.topo.location_str(p),
                                 explanation="One CHARM position can host only one signal."))
            seen[p.loc] = sid
        return out


@register
class SegregationRule_(Rule):
    id, name = "R-SEG", "A/B segregation"
    description = ("Signals with conflicting segregation designations (default A vs B) must not "
                   "share a CIOC (or cabinet, per scope).")

    def is_enabled(self, cfg):
        return cfg.segregation.enabled

    def config(self, cfg):
        return cfg.segregation.model_dump()

    def check(self, ctx):
        out = []
        scope = ctx.cfg.segregation.scope
        units: dict[tuple, dict[str, list[int]]] = {}
        for sid, p in ctx.placements.items():
            seg = ctx.by_sid[sid].seg
            if not seg:
                continue
            key = (p.cab, p.cioc) if scope == "cioc" else (p.cab,)
            units.setdefault(key, {}).setdefault(seg, []).append(sid)
        for key, bys in units.items():
            for a, b in ctx.cfg.segregation.conflict_pairs:
                if a in bys and b in bys:
                    sids = bys[a] + bys[b]
                    name = (ctx.topo.cioc_name(*key) if scope == "cioc" else ctx.topo.system_cabinet(*key))
                    out.append(Issue(self.id, "ERROR",
                                     f"{name} hosts both {a} ({len(bys[a])}) and {b} ({len(bys[b])}) signals.",
                                     sids, hardware=name,
                                     explanation=f"Designations {a} and {b} may not share a {scope}."))
        return out


@register
class GroupingRule_(Rule):
    id, name = "R-GRP", "Logical grouping"
    description = ("Signals of one logical group are kept in the same cabinet (or CIOC) and "
                   "allocated in sequence. Hard mode: violation is an error; soft: a warning.")
    hard = True

    def is_enabled(self, cfg):
        return cfg.grouping.enabled

    def config(self, cfg):
        return cfg.grouping.model_dump()

    def check(self, ctx):
        out = []
        gr = ctx.cfg.grouping
        sev = "ERROR" if gr.mode == "hard" else "WARNING"
        groups: dict[str, list[int]] = {}
        for sid in ctx.placements:
            g = ctx.by_sid[sid].group
            if g:
                groups.setdefault(g, []).append(sid)
        for g, sids in groups.items():
            keyf = (lambda s: ctx.placements[s].cab) if gr.scope == "cabinet" else \
                   (lambda s: (ctx.placements[s].cab, ctx.placements[s].cioc))
            units = {keyf(s) for s in sids}
            if len(units) > 1:
                out.append(Issue(self.id, sev, f"Group {g} is spread over {len(units)} {gr.scope}s.",
                                 sids, explanation=f"Group members must share one {gr.scope}."))
        if gr.sequence_check:
            by_cab: dict[int, list[tuple[tuple, Optional[str], int]]] = {}
            for sid, p in ctx.placements.items():
                by_cab.setdefault(p.cab, []).append((p.loc, ctx.by_sid[sid].group, sid))
            for cab, items in by_cab.items():
                items.sort()
                span: dict[str, list[int]] = {}
                for idx, (_, g, _) in enumerate(items):
                    if g:
                        span.setdefault(g, []).append(idx)
                for g, idxs in span.items():
                    foreign = (idxs[-1] - idxs[0] + 1) - len(idxs)
                    if foreign > 0:
                        out.append(Issue(self.id, "WARNING",
                                         f"Group {g} is interleaved with {foreign} foreign signal(s) in "
                                         f"{ctx.topo.system_cabinet(cab)}.",
                                         [items[i][2] for i in idxs],
                                         explanation="Group should occupy consecutive positions."))
        return out


@register
class CableRule_(Rule):
    id, name = "R-CABLE", "Cable continuity"
    description = ("Pairs of a cable are kept together in one CIOC and in ascending pair order; "
                   "unused pairs are cable spares, not signals.")

    def is_enabled(self, cfg):
        return cfg.cable.enabled

    def config(self, cfg):
        return cfg.cable.model_dump()

    def check(self, ctx):
        out = []
        cab_cfg = ctx.cfg.cable
        by_cable: dict[str, list[int]] = {}
        for sid in ctx.placements:
            s = ctx.by_sid[sid]
            if s.cable_key and s.pair is not None:
                by_cable.setdefault(s.cable_key, []).append(sid)
        charms = ctx.topo.charms
        for key, sids in by_cable.items():
            sids.sort(key=lambda s: ctx.by_sid[s].pair)
            label = ctx.by_sid[sids[0]].cable_id
            cioc = {(ctx.placements[s].cab, ctx.placements[s].cioc) for s in sids}
            if len(cioc) > 1:
                out.append(Issue(self.id, "ERROR", f"Cable {label} is split over {len(cioc)} CIOCs.",
                                 sids, cable=label))
                continue
            offs = [ctx.topo.offset(ctx.placements[s]) for s in sids]
            pairs = [ctx.by_sid[s].pair for s in sids]
            for i in range(len(sids) - 1):
                d = offs[i + 1] - offs[i]
                want = (pairs[i + 1] - pairs[i]) if cab_cfg.spare_policy == "reserve_positions" else 1
                if d <= 0:
                    out.append(Issue(self.id, "ERROR",
                                     f"Cable {label}: pair {pairs[i + 1]} is not after pair {pairs[i]} "
                                     "in slot order.", [sids[i], sids[i + 1]], cable=label))
                elif d != want:
                    out.append(Issue(self.id, "ERROR",
                                     f"Cable {label}: pairs {pairs[i]}->{pairs[i + 1]} are {d} positions "
                                     f"apart, expected {want}.", [sids[i], sids[i + 1]], cable=label,
                                     explanation="Cable pairs must occupy consecutive positions "
                                                 f"({cab_cfg.spare_policy} policy)."))
            if cab_cfg.continuity == "baseplate_aligned":
                start = offs[0] - (pairs[0] - 1 if cab_cfg.spare_policy == "reserve_positions" else 0)
                size = offs[-1] - start + 1
                bad = (start % charms != 0) if size > charms else (start // charms != (start + size - 1) // charms)
                if bad:
                    out.append(Issue(self.id, "ERROR",
                                     f"Cable {label} breaks baseplate alignment (start offset {start}).",
                                     sids, cable=label))
        return out


@register
class SpareRule_(Rule):
    id, name = "R-SPARE", "Spare capacity (empty baseplates)"
    description = ("Each used cabinet keeps N entirely empty baseplates for growth "
                   "(N = explicit value or ceil(fraction x baseplates per cabinet)).")

    def is_enabled(self, cfg):
        return cfg.spare.enabled

    def config(self, cfg):
        d = cfg.spare.model_dump()
        d["resolved_empty_baseplates_per_cabinet"] = cfg.reserved_baseplates()
        return d

    def check(self, ctx):
        out = []
        need = ctx.cfg.reserved_baseplates()
        if need == 0:
            return out
        per = ctx.cfg.hardware.baseplates_per_cioc
        occupied: dict[int, set[tuple[int, int]]] = {}
        for p in ctx.placements.values():
            occupied.setdefault(p.cab, set()).add((p.cioc, p.bp))
        sev = "ERROR" if ctx.cfg.spare.mode == "hard" else "WARNING"
        for cab, occ in occupied.items():
            empty = ctx.cfg.hardware.baseplates_per_cabinet - len(occ)
            if empty < need:
                out.append(Issue(self.id, sev,
                                 f"{ctx.topo.system_cabinet(cab)} has {empty} empty baseplate(s); "
                                 f"target is {need} (shortfall {need - empty}).",
                                 hardware=ctx.topo.system_cabinet(cab),
                                 explanation="Spare capacity is kept as whole empty baseplates."))
        return out


@register
class CompatRule(Rule):
    id, name = "R-CHARM", "CHARM / terminal block compatibility"
    description = ("Each signal's CHARM and terminal block must match the compatibility matrix for "
                   "its IO type/characteristic. Provisional mappings are flagged.")
    optional = False

    def config(self, cfg):
        return {k: v.model_dump() for k, v in cfg.compat.items()}

    def check(self, ctx):
        out = []
        for sid, p in ctx.placements.items():
            s = ctx.by_sid[sid]
            ce = ctx.cfg.compat.get(s.io_key or "")
            if ce is None:
                out.append(Issue(self.id, "ERROR", f"{s.tag}: no compatibility mapping for "
                                 f"{s.io_key or s.io_raw!r}; cannot be placed.", [sid]))
            elif (p.charm_type, p.terminal_block) != (ce.charm_type, ce.terminal_block):
                out.append(Issue(self.id, "ERROR",
                                 f"{s.tag}: assigned {p.charm_type}/{p.terminal_block} but matrix requires "
                                 f"{ce.charm_type}/{ce.terminal_block}.", [sid]))
            elif ce.status == "provisional":
                out.append(Issue(self.id, "INFO", f"{s.tag}: mapping '{s.io_key}' is provisional.", [sid]))
        return out


@register
class NamingRule(Rule):
    id, name = "R-NAME", "Naming conventions"
    description = "Hardware names generated from the configured patterns must be unique."
    optional = False

    def config(self, cfg):
        return cfg.naming.model_dump()

    def check(self, ctx):
        cabs = max([p.cab for p in ctx.placements.values()], default=0) + 1
        dup = ctx.topo.names_unique(cabs)
        if dup:
            return [Issue(self.id, "ERROR", f"Naming patterns generate duplicate names, e.g. {dup[:3]}.",
                          explanation="Include the cabinet/CIOC/baseplate index in every pattern.")]
        return []


def rule_catalog(cfg: RuleConfig) -> list[dict]:
    return [r.meta(cfg) for r in REGISTRY.values()]
