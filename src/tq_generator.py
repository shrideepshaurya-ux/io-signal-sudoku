"""Technical Query (TQ) generation from signal- and allocation-level findings."""
from __future__ import annotations

import json
from typing import Optional

import pandas as pd

from .models import AllocationResult, Finding, Signal, TQ

CATEGORY = {
    "MISSING_FIELD": "Missing mandatory data", "INVALID_PAIR": "Invalid pair number",
    "UNKNOWN_IO_TYPE": "Unknown IO type", "UNKNOWN_COMPAT": "Unknown CHARM compatibility",
    "DUPLICATE_TAG": "Duplicate tag", "DUPLICATE_PAIR": "Duplicate cable pair",
    "PAIR_OUT_OF_RANGE": "Pair outside cable size", "CABLE_SIZE_CONFLICT": "Conflicting cable size",
    "SEG_CABLE_CONFLICT": "Conflicting A/B segregation", "GROUP_SEGREGATION_CONFLICT": "Conflicting A/B segregation",
    "BLOCK_TOO_LARGE": "Impossible cable continuity", "GROUP_TOO_LARGE": "Insufficient hardware capacity",
    "INSUFFICIENT_CAPACITY": "Insufficient hardware capacity", "SPARE_SHORTFALL": "Spare-capacity shortfall",
    "SOLVER_LIMIT": "Unresolved within solver limits",
}


def _signal_tqs(s: Signal) -> list[Finding]:
    return [f for f in s.findings if f.severity == "ERROR"]


def generate_tqs(result: AllocationResult, revision: str = "") -> list[TQ]:
    """Create TQs for every ERROR finding plus allocation-level findings.
    The list is also stored on ``result.tqs``."""
    revision = revision or result.revision
    by_sid = {s.sid: s for s in result.signals}
    tqs: list[TQ] = []

    def add(f: Finding, subject: str, sids: list[int]):
        s0 = by_sid[sids[0]] if sids else None
        data = {}
        if s0:
            data = {"tag": s0.tag, "io_type": s0.io_raw, "cable": s0.cable_id, "pair": s0.pair,
                    "group": s0.group, "segregation": s0.seg, "row": s0.source_row}
        if len(sids) > 1:
            data["signals"] = [by_sid[x].tag for x in sids][:25]
            if len(sids) > 25:
                data["signals_more"] = len(sids) - 25
        hardware = f.hardware or ""
        tqs.append(TQ(
            tq_id=f"TQ-{len(tqs) + 1:04d}", category=CATEGORY.get(f.code, f.code.replace("_", " ").title()),
            severity=f.severity, subject=subject, cable=f.cable or "", hardware=hardware,
            input_data=json.dumps({k: v for k, v in data.items() if v not in (None, "")}, default=str),
            conflicting_rules=", ".join(f.rules), explanation=f.message,
            needed=f.needed or "Engineering decision required.", status="Open",
            revision=revision, sids=list(sids)))

    seen_group_findings: set[int] = set()
    for f in result.findings:
        if f.severity != "ERROR" and f.code != "SPARE_SHORTFALL":
            continue
        seen_group_findings.add(id(f))
        if f.sids:
            subj = (by_sid[f.sids[0]].tag if len(f.sids) == 1 else
                    (f"Cable {f.cable}" if f.cable and ", " not in f.cable else f"Group {f.group or '?'}"))
            if len(f.sids) > 1 and f.group and f.code.startswith("GROUP"):
                subj = f"Group {f.group}"
            add(f, subj, f.sids)
        else:
            add(f, "All cabinets", [])
    allocator_sids = {sid for f in result.findings if f.severity == "ERROR" for sid in f.sids}
    for s in result.signals:
        for f in _signal_tqs(s):
            if s.sid in allocator_sids and f.code in ("UNKNOWN_COMPAT",):
                continue
            add(f, s.tag or f"(row {s.source_row})", [s.sid])
    result.tqs = tqs
    return tqs


def tq_dataframe(tqs: list[TQ]) -> pd.DataFrame:
    cols = ["TQ ID", "Subject", "Cable", "Hardware", "Category", "Severity", "Input data",
            "Conflicting rules", "Explanation", "Information needed", "Status", "Revision"]
    return pd.DataFrame([[t.tq_id, t.subject, t.cable, t.hardware, t.category, t.severity,
                          t.input_data, t.conflicting_rules, t.explanation, t.needed, t.status,
                          t.revision] for t in tqs], columns=cols)
