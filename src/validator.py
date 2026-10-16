"""Independent validation of an allocation.

Works only from the signals, placements and configuration; it ignores the
solver's status. The rule checks live in ``rule_engine`` so enabling /
disabling a rule changes validation without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .hardware import Topology
from .models import AllocationResult, RuleConfig
from .rule_engine import Context, Issue, validate_all


@dataclass
class ValidationSummary:
    total_signals: int
    eligible_signals: int
    allocated: int
    unallocated: int
    violations: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    infos: list[Issue] = field(default_factory=list)
    tqs_generated: int = 0
    rules_checked: dict[str, bool] = field(default_factory=dict)
    fully_valid: bool = False
    complete: bool = False

    def headline(self) -> str:
        if self.violations:
            return "INVALID - hard-constraint violations present"
        if not self.complete:
            return "VALID BUT INCOMPLETE - some signals are unallocated (see TQs)"
        return "FULLY VALID under the enabled rules"


def validate(result: AllocationResult, cfg: Optional[RuleConfig] = None,
             tq_count: Optional[int] = None) -> ValidationSummary:
    cfg = cfg or result.config
    topo = Topology(cfg.hardware, cfg.naming)
    sigs = result.signals
    by_sid = {s.sid: s for s in sigs}
    issues: list[Issue] = []

    # structural checks (cannot be disabled) --------------------------------------
    for sid in result.placements:
        if sid not in by_sid:
            issues.append(Issue("STRUCT", "ERROR", f"Placement for unknown signal id {sid}.", [sid]))
    for sid in result.placements:
        if sid in result.unallocated:
            issues.append(Issue("STRUCT", "ERROR",
                                f"{by_sid[sid].tag}: both allocated and listed as unallocated.", [sid]))
    eligible = [s for s in sigs if not s.has_error]
    for s in eligible:
        if s.sid not in result.placements and s.sid not in result.unallocated:
            issues.append(Issue("STRUCT", "ERROR",
                                f"Eligible signal {s.tag} is neither allocated nor explained.", [s.sid]))
    for sid, why in result.unallocated.items():
        if not why:
            issues.append(Issue("STRUCT", "ERROR", f"Unallocated signal {by_sid[sid].tag} has no reason.", [sid]))
    for s in sigs:
        if s.has_error and s.sid in result.placements:
            issues.append(Issue("STRUCT", "ERROR",
                                f"Signal {s.tag} has input errors but was assigned a location "
                                "(invented values used).", [s.sid]))
    tags: dict[str, list[int]] = {}
    for sid in result.placements:
        tags.setdefault(by_sid[sid].tag_key, []).append(sid)
    for k, v in tags.items():
        if len(v) > 1:
            issues.append(Issue("STRUCT", "ERROR", f"Tag {k} allocated {len(v)} times.", v))

    ctx = Context(signals=sigs, placements=result.placements, cfg=cfg, topo=topo)
    rule_issues, ran = validate_all(ctx)
    issues += rule_issues

    viol = [i for i in issues if i.severity == "ERROR"]
    warn = [i for i in issues if i.severity == "WARNING"]
    info = [i for i in issues if i.severity == "INFO"]
    n_tq = tq_count if tq_count is not None else len(result.tqs)
    complete = len(result.placements) == len(sigs) and len(sigs) > 0
    return ValidationSummary(
        total_signals=len(sigs), eligible_signals=len(eligible), allocated=len(result.placements),
        unallocated=len(sigs) - len(result.placements), violations=viol, warnings=warn, infos=info,
        tqs_generated=n_tq, rules_checked=ran, fully_valid=(not viol and complete), complete=complete)
