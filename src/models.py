"""Core data model: configuration (pydantic) and runtime records (dataclasses).

All hardware limits live in ``HardwareConfig``; nothing else in the code base
hard-codes 4 / 8 / 12.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class HardwareConfig(BaseModel):
    max_cabinets: Optional[int] = Field(
        None, ge=1, description="Upper bound on cabinets (None = unbounded).")
    ciocs_per_cabinet: int = Field(4, ge=1, le=64)
    baseplates_per_cioc: int = Field(8, ge=1, le=64)
    charms_per_baseplate: int = Field(12, ge=1, le=64)

    @property
    def slots_per_cioc(self) -> int:
        return self.baseplates_per_cioc * self.charms_per_baseplate

    @property
    def baseplates_per_cabinet(self) -> int:
        return self.ciocs_per_cabinet * self.baseplates_per_cioc

    @property
    def slots_per_cabinet(self) -> int:
        return self.ciocs_per_cabinet * self.slots_per_cioc


class NamingConfig(BaseModel):
    """Python ``str.format`` patterns; fields are 1-based: cab, cioc, bp, slot."""
    system_cabinet: str = "SYS{cab:02d}"
    controller: str = "CTRL{cab:02d}-{cioc:02d}"
    marshalling_cabinet: str = "MC{cab:02d}"
    cioc: str = "CIOC{cab:02d}-{cioc:02d}"
    baseplate: str = "BP{cab:02d}-{cioc:02d}-{bp:02d}"

    @field_validator("system_cabinet", "controller", "marshalling_cabinet", "cioc", "baseplate")
    @classmethod
    def _fmt_ok(cls, v: str) -> str:
        try:
            v.format(cab=1, cioc=1, bp=1, slot=1)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"invalid naming pattern {v!r}: {exc}") from exc
        return v


class GroupingRule(BaseModel):
    enabled: bool = True
    scope: Literal["cabinet", "cioc"] = "cabinet"
    mode: Literal["hard", "soft"] = "hard"
    sequence_check: bool = True


class SegregationRule(BaseModel):
    enabled: bool = True
    scope: Literal["cioc", "cabinet"] = "cioc"
    conflict_pairs: list[list[str]] = Field(default_factory=lambda: [["A", "B"]])

    @field_validator("conflict_pairs")
    @classmethod
    def _pairs(cls, v):
        out = []
        for p in v:
            if len(p) != 2 or p[0].strip().upper() == p[1].strip().upper():
                raise ValueError("each conflict pair needs two distinct designations")
            out.append([p[0].strip().upper(), p[1].strip().upper()])
        return out


class CableRule(BaseModel):
    enabled: bool = True
    standard_sizes: list[int] = Field(default_factory=lambda: [2, 6, 12, 24])
    continuity: Literal["linear", "baseplate_aligned"] = "linear"
    spare_policy: Literal["compact", "reserve_positions"] = "compact"
    require_cable_and_pair: bool = True


class SpareRule(BaseModel):
    enabled: bool = True
    empty_baseplates_per_cabinet: Optional[int] = Field(None, ge=0)
    fraction: float = Field(0.10, ge=0.0, le=0.9)
    mode: Literal["soft", "hard"] = "soft"


class CompatEntry(BaseModel):
    charm_type: str
    terminal_block: str
    configuration: str = ""
    status: Literal["provisional", "confirmed"] = "provisional"
    note: str = ""


def _default_compat() -> dict[str, CompatEntry]:
    std = "STANDARD TERMINAL BLOCK"
    note = "Initial mapping from the problem statement; not a manufacturer specification."
    return {
        "AI": CompatEntry(charm_type="AI CHARM", terminal_block=std, note=note),
        "AI|HART": CompatEntry(charm_type="AI CHARM", terminal_block=std, configuration="HART", note=note),
        "AI|RTD": CompatEntry(charm_type="AI CHARM", terminal_block=std, configuration="RTD", note=note),
        "AO": CompatEntry(charm_type="AO CHARM", terminal_block=std, note=note),
        "AO|HART": CompatEntry(charm_type="AO CHARM", terminal_block=std, configuration="HART", note=note),
        "DI": CompatEntry(charm_type="DI CHARM", terminal_block=std, note=note),
        "DO": CompatEntry(charm_type="DO HIGH-SIDE CHARM", terminal_block=std, note=note),
        "DO|HIGH_CURRENT": CompatEntry(
            charm_type="DO ISOLATED CHARM",
            terminal_block="FUSED INJECTED POWER TERMINAL BLOCK", note=note),
        "DO|RELAY": CompatEntry(
            charm_type="DO HIGH-SIDE CHARM",
            terminal_block="RELAY OUTPUT TERMINAL BLOCK", note=note),
    }


def _default_aliases() -> dict[str, str]:
    a: dict[str, str] = {}
    for k, names in {
        "AI": ["AI", "ANALOG INPUT", "ANALOGUE INPUT", "4-20MA INPUT"],
        "AI|HART": ["AI-HART", "AI HART", "AI_HART"],
        "AI|RTD": ["RTD", "AI-RTD", "AI RTD", "AI_RTD"],
        "AO": ["AO", "ANALOG OUTPUT", "ANALOGUE OUTPUT"],
        "AO|HART": ["AO-HART", "AO HART", "AO_HART"],
        "DI": ["DI", "DIGITAL INPUT"],
        "DO": ["DO", "DIGITAL OUTPUT"],
        "DO|HIGH_CURRENT": ["DO-HC", "DO HC", "DO_HC", "DO HIGH CURRENT", "HIGH CURRENT DO",
                            "DO-ISO", "DO ISOLATED", "DO-ISOLATED"],
        "DO|RELAY": ["DO-RELAY", "DO RELAY", "DO_RELAY", "RELAY", "RELAY OUTPUT"],
    }.items():
        for n in names:
            a[n] = k
    return a


class Weights(BaseModel):
    """Objective weights (minimised). Order of magnitude encodes priority."""
    unplaced_per_slot: int = 1_000_000
    movement_per_slot: int = 100
    cabinet: int = 1000
    cioc: int = 50
    group_cabinet_span: int = 200
    group_cioc_span: int = 10


class SolverConfig(BaseModel):
    time_limit_s: float = Field(30.0, gt=0)
    workers: int = Field(8, ge=1, le=64)
    seed: int = 0
    use_greedy_hint: bool = True
    max_model_vars: int = Field(1_500_000, description="Above this the solver uses greedy placement only.")


class RuleConfig(BaseModel):
    profile_name: str = "default"
    description: str = ""
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    grouping: GroupingRule = Field(default_factory=GroupingRule)
    segregation: SegregationRule = Field(default_factory=SegregationRule)
    cable: CableRule = Field(default_factory=CableRule)
    spare: SpareRule = Field(default_factory=SpareRule)
    compat: dict[str, CompatEntry] = Field(default_factory=_default_compat)
    io_type_aliases: dict[str, str] = Field(default_factory=_default_aliases)
    weights: Weights = Field(default_factory=Weights)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    duplicate_policy: Literal["exclude_all", "keep_first"] = "exclude_all"
    max_upload_mb: int = 50

    @model_validator(mode="after")
    def _check(self):
        for alias, key in self.io_type_aliases.items():
            if alias != alias.upper():
                raise ValueError(f"alias {alias!r} must be upper-case")
        return self

    @property
    def config_id(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def reserved_baseplates(self) -> int:
        if not self.spare.enabled:
            return 0
        if self.spare.empty_baseplates_per_cabinet is not None:
            n = self.spare.empty_baseplates_per_cabinet
        else:
            import math
            n = math.ceil(self.spare.fraction * self.hardware.baseplates_per_cabinet - 1e-9)
        return min(n, self.hardware.baseplates_per_cabinet)


def load_config(path: str) -> RuleConfig:
    with open(path, "r", encoding="utf-8") as fh:
        return RuleConfig.model_validate(json.load(fh))


def save_config(cfg: RuleConfig, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg.model_dump(mode="json"), fh, indent=2)


# --------------------------------------------------------------------------
# Runtime records
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """A problem discovered by the reader / allocator; becomes a TQ or validation issue."""
    code: str
    severity: str  # ERROR | WARNING | INFO
    message: str
    sids: list[int] = field(default_factory=list)
    cable: Optional[str] = None
    group: Optional[str] = None
    hardware: Optional[str] = None
    needed: str = ""
    rules: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    sid: int
    source_row: int
    tag: Optional[str]
    io_raw: Optional[str] = None
    subtype_raw: Optional[str] = None
    io_key: Optional[str] = None
    cable_id: Optional[str] = None
    pair: Optional[int] = None
    cable_pairs: Optional[int] = None
    group: Optional[str] = None
    seg: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    source_file: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return any(f.severity == "ERROR" for f in self.findings)

    @property
    def tag_key(self) -> str:
        return (self.tag or "").strip().upper()

    @property
    def cable_key(self) -> Optional[str]:
        return self.cable_id.strip().upper() if self.cable_id else None


@dataclass
class Placement:
    cab: int      # 0-based
    cioc: int     # 0-based within cabinet
    bp: int       # 0-based within CIOC
    slot: int     # 0-based within baseplate
    charm_type: str = ""
    terminal_block: str = ""
    configuration: str = ""
    compat_status: str = ""
    block_id: Optional[int] = None
    reason: str = ""

    @property
    def loc(self) -> tuple[int, int, int, int]:
        return (self.cab, self.cioc, self.bp, self.slot)


@dataclass
class TQ:
    tq_id: str
    category: str
    severity: str
    subject: str                # signal tag or affected group
    cable: str
    hardware: str
    input_data: str
    conflicting_rules: str
    explanation: str
    needed: str
    status: str
    revision: str
    sids: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.category, self.subject, self.cable)


@dataclass
class AllocationResult:
    revision: str
    config_id: str
    timestamp: str
    signals: list[Signal]
    placements: dict[int, Placement]
    unallocated: dict[int, str]          # sid -> reason
    findings: list[Finding]
    status: str                          # OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN | INVALID_INPUT
    complete: bool
    solver: dict[str, Any]
    cable_blocks: list[dict[str, Any]]
    spare: dict[str, Any]
    tqs: list[TQ] = field(default_factory=list)
    config: Optional[RuleConfig] = None

    def signal(self, sid: int) -> Signal:
        return self.signals[sid]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
