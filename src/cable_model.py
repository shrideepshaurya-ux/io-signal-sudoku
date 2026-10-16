"""Cable blocks: ordered runs of pairs that must be placed contiguously.

Shared by the reader (range checks), allocator and validator so that all three
agree on cable semantics. Pure functions of (signals, config).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import RuleConfig, Signal


@dataclass
class Block:
    bid: int
    cable_key: Optional[str]          # None for stand-alone signals
    cable_label: str
    sids: list[int]                   # ordered by pair
    offsets: list[int]                # position of each sid inside the block
    size: int                         # slots consumed by the block
    eff: int                          # slots charged in capacity model
    groups: set[str] = field(default_factory=set)
    seg: set[str] = field(default_factory=set)
    cable_size: int = 0
    spare_pairs: int = 0

    @property
    def first_sid(self) -> int:
        return self.sids[0]


def derive_cable_sizes(signals: list[Signal], cfg: RuleConfig) -> dict[str, Optional[int]]:
    """Cable size = declared size if given, else smallest standard size that
    covers the highest pair number. ``None`` when no standard size fits."""
    std = sorted(cfg.cable.standard_sizes)
    by_cable: dict[str, list[Signal]] = {}
    for s in signals:
        if s.cable_key:
            by_cable.setdefault(s.cable_key, []).append(s)
    out: dict[str, Optional[int]] = {}
    for key, rows in by_cable.items():
        declared = [r.cable_pairs for r in rows if r.cable_pairs]
        maxpair = max([r.pair for r in rows if r.pair] or [0])
        if declared:
            size = max(declared)
        else:
            size = next((z for z in std if z >= maxpair), None)
            if size is None and maxpair == 0 and std:
                size = std[0]
        out[key] = size
    return out


def effective_size(size: int, cfg: RuleConfig) -> int:
    """Slots charged against a CIOC. In baseplate-aligned mode each block is
    rounded up to whole baseplates (conservative, always layout-feasible)."""
    if cfg.cable.enabled and cfg.cable.continuity == "baseplate_aligned":
        c = cfg.hardware.charms_per_baseplate
        return -(-size // c) * c
    return size


def build_blocks(signals: list[Signal], cfg: RuleConfig,
                 cable_sizes: Optional[dict[str, Optional[int]]] = None) -> list[Block]:
    """Build blocks from *eligible* signals."""
    if cable_sizes is None:
        cable_sizes = derive_cable_sizes(signals, cfg)
    by_cable: dict[str, list[Signal]] = {}
    singles: list[Signal] = []
    for s in signals:
        if s.cable_key and s.pair is not None and cfg.cable.enabled:
            by_cable.setdefault(s.cable_key, []).append(s)
        else:
            singles.append(s)
    blocks: list[Block] = []
    for key in sorted(by_cable):
        rows = sorted(by_cable[key], key=lambda r: (r.pair, r.sid))
        csize = cable_sizes.get(key) or max(r.pair for r in rows)
        if cfg.cable.spare_policy == "reserve_positions":
            offsets = [r.pair - 1 for r in rows]
            size = max(csize, max(offsets) + 1)
        else:
            offsets = list(range(len(rows)))
            size = len(rows)
        blk = Block(bid=len(blocks), cable_key=key, cable_label=rows[0].cable_id or key,
                    sids=[r.sid for r in rows], offsets=offsets, size=size,
                    eff=effective_size(size, cfg), cable_size=csize,
                    spare_pairs=max(0, csize - len(rows)))
        blocks.append(blk)
    for s in sorted(singles, key=lambda r: r.sid):
        blocks.append(Block(bid=len(blocks), cable_key=None, cable_label=s.cable_id or "",
                            sids=[s.sid], offsets=[0], size=1,
                            eff=effective_size(1, cfg), cable_size=0, spare_pairs=0))
    return blocks


def annotate_blocks(blocks: list[Block], by_sid: dict[int, Signal]) -> None:
    for b in blocks:
        b.groups = {by_sid[s].group for s in b.sids if by_sid[s].group}
        b.seg = {by_sid[s].seg for s in b.sids if by_sid[s].seg}
