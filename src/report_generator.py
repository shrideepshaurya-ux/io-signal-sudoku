"""Excel export. Fills a user-supplied output template when given (keeping its
formatting, sheet names, column order and merged cells) and appends report
sheets. Without a template a clearly-labelled PROVISIONAL layout is produced.

Formulas are never evaluated; string cells that start with '=' are written as
text so spreadsheet content cannot become executable formulas.
"""
from __future__ import annotations

import copy
import io
import json
import logging
import re
from typing import Optional

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .hardware import Topology
from .models import AllocationResult, RuleConfig
from .revision_diff import RevisionReport
from .tq_generator import tq_dataframe
from .utilization import compute_utilization
from .validator import ValidationSummary

log = logging.getLogger(__name__)

ALLOC_COLUMNS = ["Signal Tag", "IO Type", "Cable ID", "Pair", "Logical Group", "Segregation",
                 "Cabinet", "Marshalling Cabinet", "Controller", "CIOC", "Baseplate", "CHARM Slot",
                 "CHARM Type", "CHARM Configuration", "Terminal Block Type", "Allocation Status",
                 "Warnings", "Reason", "Source Row", "Revision"]

# template header -> field (case/space-insensitive lookup)
TEMPLATE_ALIASES: dict[str, list[str]] = {
    "Signal Tag": ["signal tag", "tag", "tag no", "tagname", "signal"],
    "IO Type": ["io type", "signal type", "type"],
    "Cable ID": ["cable id", "cable", "cable no", "cable number"],
    "Pair": ["pair", "pair no", "pair number", "core"],
    "Logical Group": ["logical group", "group", "io group"],
    "Segregation": ["segregation", "seg"],
    "Cabinet": ["cabinet", "system cabinet", "cabinet name", "cabinet id"],
    "Marshalling Cabinet": ["marshalling cabinet", "marshalling"],
    "Controller": ["controller", "controller name", "controller/cioc"],
    "CIOC": ["cioc", "cioc name", "cioc id"],
    "Baseplate": ["baseplate", "base plate", "baseplate name", "baseplate id"],
    "CHARM Slot": ["charm slot", "slot", "charm position", "charm no", "slot no", "charm"],
    "CHARM Type": ["charm type", "charm model", "charm kind"],
    "CHARM Configuration": ["charm configuration", "configuration"],
    "Terminal Block Type": ["terminal block type", "terminal block", "tb type", "terminal block (tb)"],
    "Allocation Status": ["allocation status", "status"],
    "Warnings": ["warnings", "remarks", "comments", "notes"],
    "Reason": ["reason", "allocation reason"],
    "Source Row": ["source row"],
    "Revision": ["revision", "rev"],
}

_HDR_FILL = PatternFill("solid", fgColor="DDE6F0")


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9/]+", " ", str(s).lower()).strip()


def allocation_rows(result: AllocationResult, cfg: RuleConfig) -> pd.DataFrame:
    topo = Topology(cfg.hardware, cfg.naming)
    rows = []
    for s in result.signals:
        p = result.placements.get(s.sid)
        warn = "; ".join(f.message for f in s.findings if f.severity in ("WARNING", "INFO"))
        row = {"Signal Tag": s.tag, "IO Type": s.io_raw, "Cable ID": s.cable_id, "Pair": s.pair,
               "Logical Group": s.group, "Segregation": s.seg, "Warnings": warn,
               "Source Row": s.source_row, "Revision": result.revision}
        if p:
            row.update(topo.describe(p))
            row.update({"CHARM Type": p.charm_type, "CHARM Configuration": p.configuration,
                        "Terminal Block Type": p.terminal_block, "Allocation Status": "ALLOCATED",
                        "Reason": p.reason})
            if p.compat_status == "provisional":
                row["Warnings"] = (row["Warnings"] + "; " if row["Warnings"] else "") + \
                    "CHARM mapping provisional"
        else:
            row.update({"Allocation Status": "UNALLOCATED",
                        "Reason": result.unallocated.get(s.sid, "")})
        rows.append(row)
    return pd.DataFrame(rows, columns=ALLOC_COLUMNS)


def _write_cell(ws, r, c, v):
    if isinstance(v, float) and pd.isna(v):
        v = None
    cell = ws.cell(row=r, column=c)
    cell.value = v
    if isinstance(v, str) and v.startswith("="):
        cell.data_type = "s"
    return cell


def _write_df(ws, df: pd.DataFrame, start_row: int = 1, note: Optional[str] = None) -> None:
    r = start_row
    if note:
        ws.cell(row=r, column=1, value=note).font = Font(italic=True, color="9C0006")
        r += 1
    for j, col in enumerate(df.columns, 1):
        c = ws.cell(row=r, column=j, value=str(col))
        c.font = Font(bold=True)
        c.fill = _HDR_FILL
    for i, row in enumerate(df.itertuples(index=False), r + 1):
        for j, v in enumerate(row, 1):
            if isinstance(v, (list, dict, set, tuple)):
                v = json.dumps(v, default=str)
            _write_cell(ws, i, j, v)
    if len(df.columns):
        ws.auto_filter.ref = f"A{r}:{get_column_letter(len(df.columns))}{max(r, r + len(df))}"
    ws.freeze_panes = ws.cell(row=r + 1, column=1)
    for j, col in enumerate(df.columns, 1):
        sample = [len(str(col))] + [len(str(x)) for x in df.iloc[:200, j - 1].tolist()]
        ws.column_dimensions[get_column_letter(j)].width = min(60, max(10, max(sample) + 2))


def _unique_sheet(wb, name: str) -> str:
    name = re.sub(r"[\[\]\*\?/\\:]", "-", name)[:31]
    base, n = name, 2
    while name in wb.sheetnames:
        suffix = f" ({n})"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    return name


def _find_template_target(wb, sheet: Optional[str]):
    """Return (ws, header_row, {field: column_index}) or None."""
    candidates = [wb[sheet]] if sheet and sheet in wb.sheetnames else list(wb.worksheets)
    best = None
    for ws in candidates:
        for r in range(1, min(ws.max_row, 40) + 1):
            m: dict[str, int] = {}
            for c in range(1, min(ws.max_column, 80) + 1):
                v = ws.cell(row=r, column=c).value
                if v is None:
                    continue
                n = _norm(v)
                for fld, al in TEMPLATE_ALIASES.items():
                    if fld not in m and (n in al or n == _norm(fld)):
                        m[fld] = c
                        break
            if best is None or len(m) > len(best[2]):
                best = (ws, r, m)
    if best and len(best[2]) >= 3:
        return best
    return None


def export_workbook(result: AllocationResult, cfg: RuleConfig, validation: Optional[ValidationSummary] = None,
                    input_report=None, revision: Optional[RevisionReport] = None,
                    template: Optional[bytes] = None, template_sheet: Optional[str] = None,
                    source_files: Optional[list[str]] = None) -> tuple[bytes, dict]:
    """Return (xlsx bytes, info). ``info`` says whether a template was used and
    which template columns were populated / left empty."""
    info: dict = {"template_used": False, "provisional": True, "populated": [], "unpopulated": [],
                  "template_warnings": []}
    alloc = allocation_rows(result, cfg)
    wb: Workbook
    target = None
    if template:
        try:
            wb = load_workbook(io.BytesIO(template), keep_vba=False)   # formulas kept as text
            target = _find_template_target(wb, template_sheet)
            if target is None:
                info["template_warnings"].append(
                    "Template headers could not be matched; a provisional Allocation sheet was added.")
        except Exception:
            log.exception("template could not be read")
            info["template_warnings"].append("Template could not be opened; used provisional format.")
            wb = Workbook()
            wb.remove(wb.active)
    else:
        wb = Workbook()
        wb.remove(wb.active)

    if target is not None:
        ws, hdr, colmap = target
        info.update(template_used=True, provisional=False, template_sheet=ws.title)
        first = hdr + 1
        style_row = {c: copy.copy(ws.cell(row=first, column=c)._style) for c in range(1, ws.max_column + 1)}
        # clear any pre-filled example rows under the header (values only)
        if ws.max_row >= first:
            merged = {(rng.min_row, rng.min_col) for rng in ws.merged_cells.ranges}
            for r in range(first, ws.max_row + 1):
                for c in colmap.values():
                    if (r, c) not in merged:
                        ws.cell(row=r, column=c).value = None
        for i, row in enumerate(alloc.itertuples(index=False, name=None), first):
            vals = dict(zip(ALLOC_COLUMNS, row))
            for fld, c in colmap.items():
                cell = _write_cell(ws, i, c, vals[fld])
                cell._style = copy.copy(style_row.get(c, cell._style))
        info["populated"] = sorted(colmap)
        info["unpopulated"] = [f for f in ALLOC_COLUMNS if f not in colmap]
        ws2 = wb.create_sheet(_unique_sheet(wb, "Allocation Detail"))
        _write_df(ws2, alloc)
    else:
        ws = wb.create_sheet(_unique_sheet(wb, "Allocation"))
        _write_df(ws, alloc, note="PROVISIONAL FORMAT - the official IO allocation template was not supplied. "
                                  "Do not treat column order as the project standard.")
        # keep header on row 2 -> readers find it via 'Signal Tag'
    util = compute_utilization(result, cfg)
    val_rows = []
    if validation:
        val_rows = [{"Metric": "Total signals", "Value": validation.total_signals},
                    {"Metric": "Eligible signals", "Value": validation.eligible_signals},
                    {"Metric": "Allocated", "Value": validation.allocated},
                    {"Metric": "Unallocated", "Value": validation.unallocated},
                    {"Metric": "Violations", "Value": len(validation.violations)},
                    {"Metric": "Warnings", "Value": len(validation.warnings)},
                    {"Metric": "TQs generated", "Value": validation.tqs_generated},
                    {"Metric": "Fully valid under enabled rules", "Value": validation.fully_valid},
                    {"Metric": "Verdict", "Value": validation.headline()}]
    sheets: list[tuple[str, pd.DataFrame]] = []
    if input_report is not None:
        sheets.append(("Input Validation", pd.concat(
            [pd.DataFrame(input_report.summary_rows(), columns=["Metric", "Count"]),
             pd.DataFrame([{}])], ignore_index=True).dropna(how="all")))
        if len(input_report.issues):
            sheets.append(("Input Issues", input_report.issues))
    rc = []
    if validation:
        sheets.append(("Validation Summary", pd.DataFrame(val_rows)))
        for lst in (validation.violations, validation.warnings, validation.infos):
            for i in lst:
                rc.append({"Rule": i.rule_id, "Severity": i.severity, "Message": i.message,
                           "Signals": ", ".join(result.signals[s].tag or "" for s in i.sids[:10]),
                           "Hardware": i.hardware or "", "Explanation": i.explanation})
        ran = pd.DataFrame([{"Rule": k, "Checked": v} for k, v in validation.rules_checked.items()])
        sheets.append(("Rule Checks", pd.concat([ran, pd.DataFrame(rc)], ignore_index=True)
                       if rc else ran))
    sheets.append(("TQs", tq_dataframe(result.tqs)))
    sheets.append(("Cabinet Utilization", util.cabinets))
    sheets.append(("CIOC Utilization", util.ciocs))
    sheets.append(("Baseplate Utilization", util.baseplates))
    sheets.append(("Cable Spares", util.cable_spares))
    solver_rows = [{"Item": k, "Value": str(v)} for k, v in {
        "Result status": result.status, "Complete allocation": result.complete,
        "Revision": result.revision, "Timestamp": result.timestamp, "Configuration ID": result.config_id,
        "Profile": cfg.profile_name, "Source files": "; ".join(source_files or []),
        **result.solver, **{f"Spare: {k}": v for k, v in result.spare.items()},
        **{f"Total: {k}": v for k, v in util.totals.items()}}.items()]
    sheets.append(("Solver Summary", pd.DataFrame(solver_rows)))
    if revision is not None:
        sheets.append(("Revision Summary", pd.DataFrame(
            [{"Item": k, "Value": str(v)} for k, v in revision.summary.items()])))
        sheets.append(("Revision Changes", revision.changes))
        sheets.append(("Revision Moves", revision.moves))
        sheets.append(("Revision TQs", revision.tq_changes))
    for name, df in sheets:
        w = wb.create_sheet(_unique_sheet(wb, name))
        _write_df(w, df)
    audit = wb.create_sheet(_unique_sheet(wb, "Audit"))
    _write_df(audit, pd.DataFrame([
        {"Item": "Generated", "Value": result.timestamp},
        {"Item": "Source revision", "Value": result.revision},
        {"Item": "Configuration ID", "Value": result.config_id},
        {"Item": "Format", "Value": "Official template" if info["template_used"] else "PROVISIONAL"},
        {"Item": "Source files", "Value": "; ".join(source_files or [])},
        {"Item": "Configuration (JSON)", "Value": json.dumps(cfg.model_dump(mode="json"))[:30000]},
    ]))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), info
