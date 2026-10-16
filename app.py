"""IO Signal Sudoku - Streamlit front-end. Run:  streamlit run app.py"""
from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from src import (allocator, excel_reader as er, report_generator as rg, revision_diff as rd,
                 sample_data as sd, tq_generator as tqg, validator)
from src.excel_reader import InputError
from src.models import RuleConfig, load_config
from src.rule_engine import rule_catalog
from src.utilization import compute_utilization
from src import viz, benchmark

logging.basicConfig(level=logging.INFO, filename="app.log", format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")
ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"

st.set_page_config(page_title="IO Signal Sudoku", layout="wide")


# --------------------------------------------------------------------------- state
def S(key, default=None):
    return st.session_state.get(key, default)


if "cfg" not in st.session_state:
    p = CONFIG_DIR / "default_rules.json"
    st.session_state.cfg = load_config(str(p)) if p.exists() else RuleConfig()
    st.session_state.files = {}      # name -> dict(bytes, sheet, header_row, mapping)
    st.session_state.runs = []


def cfg() -> RuleConfig:
    return st.session_state.cfg


def df_view(df: pd.DataFrame, key: str, height: int = 380):
    """Searchable table."""
    if df is None or df.empty:
        st.info("Nothing to show.")
        return
    if "Severity" in df.columns:
        levels = [x for x in ["ERROR", "WARNING", "INFO"] if x in set(df["Severity"])]
        sel = st.multiselect("Severity", levels, default=[x for x in levels if x != "INFO"] or levels,
                             key=f"sev_{key}")
        df = df[df["Severity"].isin(sel)]
    q = st.text_input("Filter (text contained in any column)", key=f"q_{key}")
    if q:
        mask = df.astype(str).apply(lambda c: c.str.contains(q, case=False, regex=False)).any(axis=1)
        df = df[mask]
    st.caption(f"{len(df)} row(s)")
    st.dataframe(df, use_container_width=True, height=height)


def to_xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for n, d in sheets.items():
            d.to_excel(xw, sheet_name=n[:31], index=False)
    return buf.getvalue()


# --------------------------------------------------------------------------- pages
def page_overview():
    st.title("IO Signal Sudoku - DeltaV IO allocation")
    st.write("Constraint-based allocation of field signals to **Cabinet -> CIOC -> Baseplate -> CHARM slot** "
             "with independent validation, Technical Queries and revision comparison. "
             "All processing is local; no data leaves this machine.")
    res = S("result")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Signals loaded", len(S("signals", [])))
    if res:
        u = compute_utilization(res, cfg())
        v = S("validation")
        c2.metric("Allocated", f"{len(res.placements)} / {len(res.signals)}")
        c3.metric("Cabinets", u.totals["Cabinets required"])
        c4.metric("TQs", len(res.tqs))
        st.subheader(f"Current allocation: {res.status}")
        if v:
            (st.success if v.fully_valid else st.warning if not v.violations else st.error)(v.headline())
    else:
        st.info("No allocation yet. Go to **Upload & Validate**, then **Allocation**. "
                "A synthetic demo dataset can be loaded from the Upload page.")
    if st.button("Run full SYNTHETIC demo (load, validate, allocate)", type="primary"):
        try:
            with st.spinner("Running demo..."):
                name = "SYNTHETIC_demo_problems.xlsx"
                rr = er.read_master(sd.to_workbook_bytes(sd.synthetic_with_problems()), name, cfg())
                st.session_state.signals, st.session_state.input_report = rr.signals, rr.report
                st.session_state.source_files = [name]
                run_allocation("Demo Rev A")
            st.rerun()
        except Exception:
            log.exception("demo failed")
            st.error("Demo failed. See app.log.")
    st.subheader("Recent runs")
    if S("runs"):
        st.dataframe(pd.DataFrame(S("runs")), use_container_width=True)
    else:
        st.caption("No runs in this session.")
    st.caption("Provisional items: CHARM / terminal-block mappings and default naming patterns are "
               "placeholders until validated against project specifications.")


def page_upload():
    st.header("Upload and input validation")
    up = st.file_uploader("IO MDB workbook(s) (.xlsx / .xlsm)", type=["xlsx", "xlsm"], accept_multiple_files=True)
    c1, c2 = st.columns([1, 3])
    if c1.button("Load SYNTHETIC demo MDB"):
        name = "SYNTHETIC_demo_basic.xlsx"
        st.session_state.files[name] = {"bytes": sd.to_workbook_bytes(sd.synthetic_with_problems()),
                                        "sheet": None, "header": None, "mapping": None}
    c2.caption("Synthetic data only - not real project data.")
    for f in up or []:
        if f.name not in st.session_state.files:
            st.session_state.files[f.name] = {"bytes": f.getvalue(), "sheet": None, "header": None, "mapping": None}
    if not S("files"):
        st.info("Upload a workbook to begin.")
        return
    frames = []
    for name, ent in list(S("files").items()):
        with st.expander(name, expanded=len(S("files")) == 1):
            try:
                sheets = er.list_sheets(ent["bytes"], name, cfg().max_upload_mb)
            except InputError as e:
                st.error(str(e))
                continue
            sheet = st.selectbox("Worksheet", sheets, key=f"sh_{name}",
                                 index=sheets.index(ent["sheet"]) if ent["sheet"] in sheets else 0)
            try:
                prev = er.preview(ent["bytes"], name, sheet, 12, cfg().max_upload_mb)
                rows = er._sheet_rows(ent["bytes"], name, sheet, cfg().max_upload_mb)
            except InputError as e:
                st.error(str(e))
                continue
            auto_hr = er.detect_header_row(rows)
            hr = st.number_input("Header row (1-based)", 1, len(rows), auto_hr + 1, key=f"hr_{name}") - 1
            st.dataframe(prev, use_container_width=True, height=220)
            try:
                df, hr = er.load_table(ent["bytes"], name, sheet, hr, cfg().max_upload_mb)
            except InputError as e:
                st.error(str(e))
                continue
            cols = [c for c in df.columns if c != "_source_row"]
            auto, conf = er.detect_mapping(cols)
            st.markdown("**Column mapping** (auto-detected; change if wrong)")
            mcols = st.columns(4)
            mapping = {}
            for i, fld in enumerate(er.FIELDS):
                opts = ["(none)"] + cols
                default = opts.index(auto[fld]) if auto.get(fld) in opts else 0
                lab = f"{fld} [{conf.get(fld)}]"
                sel = mcols[i % 4].selectbox(lab, opts, index=default, key=f"map_{name}_{fld}")
                mapping[fld] = None if sel == "(none)" else sel
            frames.append((df, mapping, name))
            if st.button("Remove file", key=f"rm_{name}"):
                del st.session_state.files[name]
                st.rerun()
    if not frames:
        return
    try:
        signals, report = er.build_signals_multi(frames, cfg())
    except InputError as e:
        st.error(str(e))
        return
    except Exception:
        log.exception("input build failed")
        st.error("The input could not be processed. See app.log for diagnostics.")
        return
    st.session_state.signals = signals
    st.session_state.input_report = report
    st.session_state.source_files = [n for _, _, n in frames]
    st.session_state.raw_frames = {n: d for d, _, n in frames}
    st.subheader("Input validation report")
    cols = st.columns(4)
    for i, (k, v) in enumerate(report.summary_rows()):
        cols[i % 4].metric(k, v)
    errs = report.issues[report.issues["Severity"] == "ERROR"] if len(report.issues) else report.issues
    if len(errs):
        st.error(f"{len(errs)} error(s) prevent safe allocation of the affected records "
                 "(these are excluded and will become TQs).")
    elif report.warnings:
        st.warning("Warnings need engineering review.")
    else:
        st.success("No input errors.")
    df_view(report.issues, "issues")
    iss = report.issues.copy()
    order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    if len(iss):
        iss = iss.sort_values("Severity", key=lambda c: c.map(order), kind="stable")
    st.download_button("Download input-validation report", to_xlsx(
        {"Summary": pd.DataFrame(report.summary_rows(), columns=["Metric", "Count"]),
         "Errors & Warnings": iss[iss["Severity"] != "INFO"] if len(iss) else iss,
         "Info (provisional mappings)": iss[iss["Severity"] == "INFO"] if len(iss) else iss}),
        "input_validation_report.xlsx")


def page_rules():
    st.header("Rule configuration")
    c = cfg()
    st.caption("Hard constraints are never relaxed by the solver; preferences are weighted objectives.")
    with st.form("rules"):
        a, b = st.columns(2)
        with a:
            st.subheader("Hardware (hard)")
            mc = st.number_input("Max cabinets (0 = unbounded)", 0, 10000, c.hardware.max_cabinets or 0)
            cpc = st.number_input("CIOCs per cabinet", 1, 64, c.hardware.ciocs_per_cabinet)
            bpc = st.number_input("Baseplates per CIOC", 1, 64, c.hardware.baseplates_per_cioc)
            chb = st.number_input("CHARM positions per baseplate", 1, 64, c.hardware.charms_per_baseplate)
            st.subheader("Spare capacity (empty baseplates)")
            sp_on = st.checkbox("Enable spare capacity", c.spare.enabled)
            sp_frac = st.slider("Target fraction of baseplates per cabinet", 0.0, 0.5, float(c.spare.fraction), 0.01)
            sp_abs = st.number_input("...or explicit empty baseplates per cabinet (-1 = use fraction)",
                                     -1, 1000, -1 if c.spare.empty_baseplates_per_cabinet is None
                                     else c.spare.empty_baseplates_per_cabinet)
            sp_mode = st.selectbox("If target cannot be met", ["soft", "hard"],
                                   index=["soft", "hard"].index(c.spare.mode),
                                   help="soft: use reserved baseplates when needed and report the shortfall; "
                                        "hard: leave signals unallocated instead.")
        with b:
            st.subheader("Logical grouping (A)")
            g_on = st.checkbox("Enable grouping", c.grouping.enabled)
            g_scope = st.selectbox("Group scope", ["cabinet", "cioc"], index=["cabinet", "cioc"].index(c.grouping.scope))
            g_mode = st.selectbox("Group mode", ["hard", "soft"], index=["hard", "soft"].index(c.grouping.mode))
            st.subheader("A/B segregation (B)")
            s_on = st.checkbox("Enable segregation", c.segregation.enabled)
            s_scope = st.selectbox("Segregation scope", ["cioc", "cabinet"],
                                   index=["cioc", "cabinet"].index(c.segregation.scope))
            s_pairs = st.text_input("Conflict pairs (comma-separated, e.g. A:B, C:D)",
                                    ", ".join(f"{p}:{q}" for p, q in c.segregation.conflict_pairs))
            st.subheader("Cable continuity (D)")
            cb_on = st.checkbox("Enable cable continuity", c.cable.enabled)
            cb_cont = st.selectbox("Continuity policy", ["linear", "baseplate_aligned"],
                                   index=["linear", "baseplate_aligned"].index(c.cable.continuity),
                                   help="linear: consecutive slots, may cross baseplates. baseplate_aligned: "
                                        "cables <= one baseplate stay within it; larger cables start on a baseplate.")
            cb_sp = st.selectbox("Spare-pair policy", ["compact", "reserve_positions"],
                                 index=["compact", "reserve_positions"].index(c.cable.spare_policy),
                                 help="compact: only used pairs occupy slots. reserve_positions: unused pairs "
                                      "keep their slot position empty.")
            cb_req = st.checkbox("Cable ID and pair are mandatory", c.cable.require_cable_and_pair)
            dup = st.selectbox("Duplicate tags", ["exclude_all", "keep_first"],
                               index=["exclude_all", "keep_first"].index(c.duplicate_policy))
        st.subheader("Naming patterns (1-based fields: cab, cioc, bp, slot)")
        n = st.columns(5)
        names = {k: n[i].text_input(k, getattr(c.naming, k)) for i, k in enumerate(
            ["system_cabinet", "controller", "marshalling_cabinet", "cioc", "baseplate"])}
        st.subheader("Signal -> CHARM / terminal block matrix (provisional)")
        comp_df = st.data_editor(pd.DataFrame(
            [{"key": k, **v.model_dump()} for k, v in c.compat.items()]), num_rows="dynamic",
            use_container_width=True, key="compat_ed")
        st.subheader("IO type aliases (raw text -> matrix key)")
        alias_df = st.data_editor(pd.DataFrame(
            [{"alias": k, "key": v} for k, v in c.io_type_aliases.items()]), num_rows="dynamic",
            use_container_width=True, key="alias_ed")
        st.subheader("Solver and objective weights (preferences)")
        w = st.columns(4)
        tl = w[0].number_input("Time limit (s)", 1.0, 3600.0, float(c.solver.time_limit_s))
        wk = w[1].number_input("Workers", 1, 64, c.solver.workers)
        w_mv = w[2].number_input("Movement penalty / slot", 0, 100000, c.weights.movement_per_slot)
        w_cab = w[3].number_input("Cabinet penalty", 0, 1000000, c.weights.cabinet)
        w2 = st.columns(3)
        w_ci = w2[0].number_input("CIOC penalty", 0, 100000, c.weights.cioc)
        w_gs = w2[1].number_input("Group cabinet-span penalty", 0, 100000, c.weights.group_cabinet_span)
        w_gc = w2[2].number_input("Group CIOC-span penalty", 0, 100000, c.weights.group_cioc_span)
        submitted = st.form_submit_button("Apply configuration")
    if submitted:
        try:
            d = c.model_dump(mode="json")
            d["hardware"].update(max_cabinets=int(mc) or None, ciocs_per_cabinet=int(cpc),
                                 baseplates_per_cioc=int(bpc), charms_per_baseplate=int(chb))
            d["spare"].update(enabled=sp_on, fraction=sp_frac, mode=sp_mode,
                              empty_baseplates_per_cabinet=None if sp_abs < 0 else int(sp_abs))
            d["grouping"].update(enabled=g_on, scope=g_scope, mode=g_mode)
            d["segregation"].update(enabled=s_on, scope=s_scope, conflict_pairs=[
                [x.strip() for x in pr.split(":")] for pr in s_pairs.split(",") if ":" in pr])
            d["cable"].update(enabled=cb_on, continuity=cb_cont, spare_policy=cb_sp, require_cable_and_pair=cb_req)
            d["duplicate_policy"] = dup
            d["naming"] = names
            d["compat"] = {r["key"]: {k: r[k] for k in ("charm_type", "terminal_block", "configuration",
                                                         "status", "note")}
                           for _, r in comp_df.dropna(subset=["key"]).iterrows()}
            d["io_type_aliases"] = {str(r["alias"]).upper().strip(): r["key"]
                                    for _, r in alias_df.dropna(subset=["alias"]).iterrows()}
            d["solver"].update(time_limit_s=tl, workers=int(wk))
            d["weights"].update(movement_per_slot=int(w_mv), cabinet=int(w_cab), cioc=int(w_ci),
                                group_cabinet_span=int(w_gs), group_cioc_span=int(w_gc))
            new = RuleConfig.model_validate(d)
            from src.hardware import Topology
            dups = Topology(new.hardware, new.naming).names_unique(3)
            if dups:
                raise ValueError(f"naming patterns are not unique: {dups[:3]}")
            st.session_state.cfg = new
            st.success(f"Configuration applied (id {new.config_id}).")
        except Exception as exc:  # pydantic / value errors are user-facing
            st.error(f"Invalid configuration: {exc}")
    st.subheader("Profiles")
    p1, p2 = st.columns(2)
    p1.download_button("Download current profile (JSON)", json.dumps(cfg().model_dump(mode="json"), indent=2),
                       f"{cfg().profile_name}.json")
    prof = p2.file_uploader("Load profile (JSON)", type=["json"], key="prof_up")
    if prof is not None and st.button("Load uploaded profile"):
        try:
            st.session_state.cfg = RuleConfig.model_validate_json(prof.getvalue())
            st.success("Profile loaded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid profile: {exc}")
    saved = sorted(p.name for p in CONFIG_DIR.glob("*.json"))
    pick = st.selectbox("Bundled profiles", saved) if saved else None
    if pick and st.button("Load bundled profile"):
        st.session_state.cfg = load_config(str(CONFIG_DIR / pick))
        st.rerun()
    st.subheader("Rule catalogue")
    cat = pd.DataFrame(rule_catalog(cfg()))
    cat["Configuration"] = cat["Configuration"].apply(lambda d: json.dumps(d, default=str)[:300])
    st.dataframe(cat, use_container_width=True)
    st.caption(f"Configuration ID: {cfg().config_id}")


def run_allocation(label: str):
    sig = S("signals")
    res = allocator.allocate(sig, cfg(), revision=label)
    tqg.generate_tqs(res)
    val = validator.validate(res, cfg())
    st.session_state.result, st.session_state.validation = res, val
    st.session_state.export = None
    st.session_state.runs.append({"Time": res.timestamp, "Revision": label, "Status": res.status,
                                  "Allocated": f"{len(res.placements)}/{len(res.signals)}",
                                  "TQs": len(res.tqs), "Valid": val.fully_valid,
                                  "Runtime s": res.solver.get("runtime_s"), "Config": res.config_id})


def page_alloc():
    st.header("Allocation")
    if not S("signals"):
        st.info("Load an IO MDB first.")
        return
    label = st.text_input("Revision label", "Rev A")
    tpl = st.file_uploader("Output template (.xlsx) - optional", type=["xlsx", "xlsm"], key="tpl")
    if tpl is not None:
        st.session_state.template = tpl.getvalue()
        st.session_state.template_name = tpl.name
    tl = st.number_input("Solver time limit (s)", 1.0, 3600.0, float(cfg().solver.time_limit_s))
    cfg().solver.time_limit_s = tl
    if st.button("Run allocation", type="primary"):
        try:
            with st.spinner("Solving (CP-SAT)..."):
                run_allocation(label)
            st.session_state.export = None
        except Exception:
            log.exception("allocation failed")
            st.error("Allocation failed unexpectedly. See app.log for diagnostics.")
    res, val = S("result"), S("validation")
    if not res:
        return
    s = res.solver
    c = st.columns(5)
    c[0].metric("Status", res.status)
    c[1].metric("Allocated", f"{len(res.placements)}/{len(res.signals)}")
    c[2].metric("Unallocated", len(res.signals) - len(res.placements))
    c[3].metric("TQs", len(res.tqs))
    c[4].metric("Runtime (s)", s.get("runtime_s"))
    st.caption("Status describes the solve over *eligible* signals; records excluded for input errors "
               "are reported separately as TQs.")
    if not res.complete:
        st.warning("PARTIAL allocation - unallocated signals are listed below with TQs.")
    if res.status == "FEASIBLE":
        st.info("A complete allocation was found but optimality was not proven within the limits.")
    (st.success if val.fully_valid else st.warning if not val.violations else st.error)(val.headline())
    with st.expander("Solver statistics"):
        st.json({k: v for k, v in s.items()})
    t1, t2, t3, t4 = st.tabs(["Allocation", "Unallocated", "TQs", "Validation"])
    with t1:
        df_view(rg.allocation_rows(res, cfg()), "alloc")
    with t2:
        un = rg.allocation_rows(res, cfg())
        df_view(un[un["Allocation Status"] == "UNALLOCATED"], "unalloc")
    with t3:
        df_view(tqg.tq_dataframe(res.tqs), "tq")
    with t4:
        if st.button("Re-run independent validation"):
            st.session_state.validation = validator.validate(res, cfg())
            val = S("validation")
        st.write(f"Violations: {len(val.violations)} | warnings: {len(val.warnings)} | "
                 f"rules checked: {', '.join(k for k, v in val.rules_checked.items() if v)}")
        rows = [{"Rule": i.rule_id, "Severity": i.severity, "Message": i.message} for i in
                val.violations + val.warnings + val.infos[:200]]
        df_view(pd.DataFrame(rows, columns=["Rule", "Severity", "Message"]), "val")
    if st.button("Prepare Excel workbook"):
        try:
            data, info = rg.export_workbook(res, cfg(), S("validation"), S("input_report"),
                                            template=S("template"), source_files=S("source_files"))
            st.session_state.export = (data, info)
        except Exception:
            log.exception("export failed")
            st.error("Export failed. See app.log for diagnostics.")
    if S("export"):
        data, info = S("export")
        if info["template_used"]:
            st.success(f"Template applied (sheet {info.get('template_sheet')}); "
                       f"columns not in template: {', '.join(info['unpopulated']) or 'none'}")
        else:
            st.warning("Provisional output format used (no usable template).")
        for w in info["template_warnings"]:
            st.warning(w)
        st.download_button("Download allocation workbook", data,
                           f"io_allocation_{res.revision.replace(' ', '_')}_{res.config_id}.xlsx")


def page_reports():
    st.header("Reports")
    res = S("result")
    if not res:
        st.info("Run an allocation first.")
        return
    u = compute_utilization(res, cfg())
    cols = st.columns(4)
    for i, (k, v) in enumerate(u.totals.items()):
        cols[i % 4].metric(k, v)
    sp = res.spare
    if sp.get("shortfall_baseplates_per_cabinet"):
        st.warning(f"Spare target {sp['target_baseplates_per_cabinet']} empty baseplate(s) per cabinet; "
                   f"achieved {sp['achieved_baseplates_per_cabinet']}.")
    if not u.cabinets.empty:
        fig = px.bar(u.cabinets, x="Cabinet", y="Utilization %", hover_data=["CIOCs used", "Baseplates used",
                                                                              "Empty baseplates"])
        fig.update_yaxes(range=[0, 100])
        st.plotly_chart(fig, use_container_width=True)
    tabs = st.tabs(["Cabinets", "CIOCs", "Baseplates", "Cable blocks", "TQs", "Violations", "Signal inspector",
                    "TQ dashboard"])
    with tabs[0]:
        df_view(u.cabinets, "cab")
    with tabs[1]:
        df_view(u.ciocs, "cioc")
    with tabs[2]:
        df_view(u.baseplates, "bp")
    with tabs[3]:
        df_view(u.cable_spares, "cable")
    with tabs[4]:
        df_view(tqg.tq_dataframe(res.tqs), "tq2")
    with tabs[5]:
        v = S("validation")
        df_view(pd.DataFrame([{"Rule": i.rule_id, "Severity": i.severity, "Message": i.message}
                              for i in v.violations + v.warnings], columns=["Rule", "Severity", "Message"]), "viol")
    with tabs[7]:
        tdf = tqg.tq_dataframe(res.tqs)
        fig = viz.tq_chart(tdf)
        if fig is None:
            st.info("No TQs in this run.")
        else:
            m = st.columns(3)
            m[0].metric("TQs", len(tdf))
            m[1].metric("Categories", tdf["Category"].nunique())
            m[2].metric("Errors", int((tdf["Severity"] == "ERROR").sum()))
            st.plotly_chart(fig, use_container_width=True)
            ex = tdf.iloc[0]
            a, b = st.columns(2)
            a.markdown("**A generic tool says**")
            a.code("Allocation failed.")
            b.markdown(f"**This tool says ({ex['Category']})**")
            b.write(ex["Explanation"])
            b.write("**Needed to resolve:** " + str(ex["Information needed"]))
    with tabs[6]:
        tags = sorted({s.tag for s in res.signals if s.tag})
        pick = st.selectbox("Signal tag", tags)
        sig = next(s for s in res.signals if s.tag == pick)
        p = res.placements.get(sig.sid)
        if p:
            from src.hardware import Topology
            st.json({**Topology(cfg().hardware, cfg().naming).describe(p), "CHARM type": p.charm_type,
                     "Terminal block": p.terminal_block, "Mapping status": p.compat_status})
            st.write("**Why here:** " + p.reason)
        else:
            st.error("Unallocated: " + res.unallocated.get(sig.sid, "no reason recorded"))
        for f in sig.findings:
            st.write(f"[{f.severity}] {f.code}: {f.message}")


def page_revision():
    st.header("Revision comparison")
    st.caption("Rev A is the baseline: the current allocation, or a new upload that is allocated first. "
               "Optionally restore Rev A locations from a previously exported workbook.")
    use_cur = False
    if S("result"):
        use_cur = st.checkbox(f"Use current allocation ({S('result').revision}) as Rev A", True)
    up_a = None if use_cur else st.file_uploader("Rev A IO MDB", type=["xlsx", "xlsm"], key="revA")
    up_b = st.file_uploader("Rev B IO MDB", type=["xlsx", "xlsm"], key="revB")
    prev_wb = st.file_uploader("Previous allocation workbook (optional, restores Rev A locations)",
                               type=["xlsx"], key="prevwb")
    if st.button("Load SYNTHETIC Rev A/B demo"):
        st.session_state.demo_rev = (sd.to_workbook_bytes(sd.synthetic_basic()),
                                     sd.to_workbook_bytes(sd.synthetic_revision_b()))
    demo = S("demo_rev")
    if demo:
        st.info("Using SYNTHETIC demo revisions.")
    if st.button("Compare revisions", type="primary"):
        try:
            with st.spinner("Allocating and comparing..."):
                if demo and not up_b:
                    a_bytes, b_bytes, a_name, b_name = demo[0], demo[1], "synthetic_A.xlsx", "synthetic_B.xlsx"
                    use_current = False
                else:
                    b_bytes, b_name = (up_b.getvalue(), up_b.name) if up_b else (None, None)
                    use_current = use_cur
                    a_bytes, a_name = (up_a.getvalue(), up_a.name) if up_a else (None, None)
                if b_bytes is None:
                    st.error("Upload Rev B first.")
                    return
                if use_current:
                    res_a = S("result")
                else:
                    if a_bytes is None:
                        st.error("Provide Rev A.")
                        return
                    ra = er.read_master(a_bytes, a_name, cfg(), revision_label="Rev A")
                    res_a = allocator.allocate(ra.signals, cfg(), revision="Rev A")
                    tqg.generate_tqs(res_a)
                prev = rd.previous_from_workbook(prev_wb.getvalue(), cfg()) if prev_wb else None
                rb = er.read_master(b_bytes, b_name, cfg(), revision_label="Rev B")
                rep = rd.compare_revisions(res_a, rb.signals, cfg(), "Rev B", previous=prev)
                st.session_state.rev_report = rep
                st.session_state.rev_a = res_a
                st.session_state.rev_input_report = rb.report
                st.session_state.rev_src = [a_name or "current allocation", b_name]
        except InputError as e:
            st.error(str(e))
        except Exception:
            log.exception("revision compare failed")
            st.error("Revision comparison failed. See app.log for diagnostics.")
    rep = S("rev_report")
    if not rep:
        return
    cols = st.columns(4)
    for i, (k, v) in enumerate(rep.summary.items()):
        cols[i % 4].metric(k, str(v))
    _revision_visual(rep, S("rev_a"))
    t = st.tabs(["Changes", "Moved signals", "Affected hardware", "TQ changes"])
    with t[0]:
        df_view(rep.changes, "rchg")
    with t[1]:
        df_view(rep.moves, "rmv")
    with t[2]:
        df_view(rep.affected, "raff")
    with t[3]:
        df_view(rep.tq_changes, "rtq")
    resb = rep.result_b
    vb = validator.validate(resb, cfg())
    (st.success if vb.fully_valid else st.warning if not vb.violations else st.error)(
        "Rev B: " + vb.headline())
    data, _ = rg.export_workbook(resb, cfg(), vb, S("rev_input_report"), revision=rep,
                                 template=S("template"), source_files=S("rev_src"))
    st.download_button("Download Rev B allocation + revision report", data, "revision_report.xlsx")


def _explain(res, sid):
    """Render the 'why here' panel for one signal."""
    from src.hardware import Topology
    sig = res.signals[sid]
    p = res.placements.get(sid)
    st.markdown(f"#### {sig.tag}")
    if p:
        st.json({**Topology(cfg().hardware, cfg().naming).describe(p), "IO type": sig.io_raw,
                 "Cable / pair": f"{sig.cable_id} / {sig.pair}", "Group": sig.group, "Segregation": sig.seg,
                 "CHARM type": p.charm_type, "Terminal block": p.terminal_block,
                 "Mapping status": p.compat_status})
        st.write("**Why here:** " + p.reason)
    else:
        st.error("Unallocated: " + res.unallocated.get(sid, "no reason recorded"))
    for f in sig.findings:
        if f.severity != "INFO":
            st.write(f"[{f.severity}] {f.code}: {f.message}")


def page_map():
    st.header("Cabinet map")
    res = S("result")
    if not res or not res.placements:
        st.info("Run an allocation first (Overview has a one-click demo).")
        return
    cabs = sorted({p.cab for p in res.placements.values()})
    from src.hardware import Topology
    topo = Topology(cfg().hardware, cfg().naming)
    c1, c2 = st.columns(2)
    cab = c1.selectbox("Cabinet", cabs, format_func=topo.system_cabinet)
    color_by = c2.selectbox("Colour by", ["IO type", "Segregation (A/B)", "Logical group", "CHARM type"])
    st.caption("Each row is a baseplate (12 CHARM slots). Light grey = free, darker grey = reserved spare "
               "baseplate. Click a square to see why that signal is there.")
    fig, lookup = viz.cabinet_map(res, cfg(), cab, color_by)
    ev = st.plotly_chart(fig, use_container_width=True, on_select="rerun", selection_mode="points",
                         key="cabmap")
    picked = None
    try:
        pts = ev.selection.points if ev and ev.selection else []
        if pts:
            picked = lookup.get((int(round(pts[0]["x"])), int(round(pts[0]["y"]))))
    except Exception:
        picked = None
    if picked is not None:
        _explain(res, picked)
    else:
        st.caption("No square selected - or pick a signal below.")
        tags = sorted(s.tag for s in res.signals if s.sid in res.placements and res.placements[s.sid].cab == cab)
        t = st.selectbox("Signal in this cabinet", tags)
        if t:
            _explain(res, next(s.sid for s in res.signals if s.tag == t and s.sid in res.placements))


def _revision_visual(rep, res_a):
    resb = rep.result_b
    st.subheader("Before / after map")
    cabs = sorted({p.cab for p in list(res_a.placements.values()) + list(resb.placements.values())})
    from src.hardware import Topology
    topo = Topology(cfg().hardware, cfg().naming)
    cab = st.selectbox("Cabinet", cabs, format_func=topo.system_cabinet, key="rev_cab")
    cls = dict(zip(rep.changes["Tag"].str.upper(), rep.changes["Change"]))

    def label(c):
        if "reallocated" in c:
            return "Moved"
        if c.startswith("Added"):
            return "Added"
        if c.startswith("Modified"):
            return "Modified"
        if c.startswith("Deleted"):
            return "Deleted"
        return "Unchanged"

    def a_label(c):
        k = label(c)
        return "Moved (old position)" if k == "Moved" else k

    hl_a = {s.sid: a_label(cls.get(s.tag_key, "")) for s in res_a.signals if s.tag}
    hl_b = {s.sid: label(cls.get(s.tag_key, "")) for s in resb.signals if s.tag}
    a, b = st.columns(2)
    fa, _ = viz.cabinet_map(res_a, cfg(), cab, highlight=hl_a, title=f"{res_a.revision}")
    fb, _ = viz.cabinet_map(resb, cfg(), cab, highlight=hl_b, title=f"{resb.revision}")
    a.plotly_chart(fa, use_container_width=True, key="vis_a")
    b.plotly_chart(fb, use_container_width=True, key="vis_b")
    moved = int(rep.changes["Change"].str.contains("eallocated").sum())
    st.success(f"{int((rep.changes['Change'] == 'Unchanged signal').sum())} signals kept their exact slot; "
               f"{moved} moved; {rep.summary['Added']} added; {rep.summary['Deleted']} deleted.")


def page_results():
    st.header("Results and benchmarks")
    st.subheader("Recorded 10,000-signal run")
    bp = ROOT / "docs" / "benchmark_10000.json"
    if bp.exists():
        d = json.loads(bp.read_text())
        s = d.get("solver", {})
        cols = st.columns(4)
        cols[0].metric("Signals", d["signals"])
        cols[1].metric("Allocation time (s)", d["allocate_s"])
        cols[2].metric("Cabinets", d["cabinets"])
        cols[3].metric("Rule violations", d["violations"])
        st.caption(f"Status {d['status']} (complete: {d['complete']}); solver {s.get('method')} limit 60 s, "
                   f"lower-bound cabinets {s.get('lower_bound_cabinets')}; validation {d['validate_s']} s; "
                   f"memory ~{d['rss_mb']} MB; {d['warnings']} group-sequence warnings. "
                   "Measured on the developer machine with synthetic data; optimality was not proven.")
    else:
        st.info("No recorded benchmark file found.")
    st.subheader("Live scaling test (runs the real solver now)")
    sizes = st.multiselect("Signal counts", [250, 500, 1000, 2000, 4000], default=[250, 500, 1000, 2000])
    tl = st.number_input("Time limit per run (s)", 1.0, 120.0, 10.0)
    if st.button("Run scaling test") and sizes:
        bar = st.progress(0.0, text="Starting")
        try:
            df = benchmark.run_scaling(sorted(sizes), cfg(), tl,
                                       lambda i, n, m: bar.progress(i / n, text=m))
            st.session_state.scaling = df
        except Exception:
            log.exception("scaling failed")
            st.error("Scaling test failed. See app.log.")
        bar.empty()
    if S("scaling") is not None:
        df = S("scaling")
        st.plotly_chart(viz.scaling_chart(df), use_container_width=True)
        st.dataframe(df, use_container_width=True)
    st.subheader("Manual vs automated (your estimate)")
    c1, c2 = st.columns(2)
    n = c1.number_input("Signals in the project", 1, 1_000_000, 10000)
    hrs = c2.number_input("Your team's estimated manual hours", 0.0, 100000.0, 0.0,
                          help="This number comes from you; the tool does not know it.")
    sc = S("scaling")
    if sc is not None and len(sc):
        r = sc.iloc[-1]
        per = r["Allocation time (s)"] / r["Signals"]
        auto_s = per * n
        st.write(f"Measured automated rate: {per * 1000:.1f} ms/signal -> about **{auto_s:.0f} s** for {n} signals "
                 "(extrapolated from the largest live run).")
        if hrs > 0:
            st.metric("Speed-up vs your manual estimate", f"{hrs * 3600 / max(auto_s, 1):.0f}x")
    else:
        st.caption("Run the live scaling test to get a measured rate.")


def page_whatif():
    st.header("Rule what-if")
    sig = S("signals")
    if not sig:
        st.info("Load an IO MDB first.")
        return
    st.caption("Switch rules off and re-solve on a copy of the configuration. The what-if result is then "
               "checked against the FULL rule set, so you can see exactly what breaks.")
    c = st.columns(4)
    seg = c[0].checkbox("A/B segregation", cfg().segregation.enabled)
    grp = c[1].checkbox("Logical grouping", cfg().grouping.enabled)
    cab = c[2].checkbox("Cable continuity", cfg().cable.enabled)
    spr = c[3].checkbox("Spare baseplates", cfg().spare.enabled)
    if st.button("Run what-if", type="primary"):
        try:
            with st.spinner("Solving both scenarios..."):
                base_cfg = cfg().model_copy(deep=True)
                what = base_cfg.model_copy(deep=True)
                what.segregation.enabled, what.grouping.enabled = seg, grp
                what.cable.enabled, what.spare.enabled = cab, spr
                rows = []
                for name, cf in (("Baseline (current rules)", base_cfg), ("What-if", what)):
                    r = allocator.allocate(S("signals"), cf, revision=name)
                    tqg.generate_tqs(r)
                    v_own = validator.validate(r, cf)
                    v_full = validator.validate(r, base_cfg)
                    u = compute_utilization(r, cf)
                    rows.append({"Scenario": name, "Allocated": f"{len(r.placements)}/{len(r.signals)}",
                                 "Cabinets": u.totals["Cabinets required"], "CIOCs": u.totals["CIOCs used"],
                                 "Baseplates": u.totals["Baseplates used"],
                                 "Violations (own rules)": len(v_own.violations),
                                 "Violations vs FULL rules": len(v_full.violations),
                                 "Rules broken": ", ".join(sorted({i.rule_id for i in v_full.violations})) or "-"})
                st.session_state.whatif = pd.DataFrame(rows)
        except Exception:
            log.exception("what-if failed")
            st.error("What-if failed. See app.log.")
    if S("whatif") is not None:
        st.dataframe(S("whatif"), use_container_width=True)
        w = S("whatif")
        if w.iloc[1]["Violations vs FULL rules"] > 0:
            st.error("With those rules off, the layout breaks the full rule set - this is why the rules exist.")
        else:
            st.success("No additional violations against the full rule set for this selection.")


PAGES = {"Overview": page_overview, "Upload & Validate": page_upload, "Rule Configuration": page_rules,
         "Allocation": page_alloc, "Cabinet Map": page_map, "Reports": page_reports,
         "Revision Comparison": page_revision, "Rule What-if": page_whatif,
         "Results & Benchmarks": page_results}
choice = st.sidebar.radio("Page", list(PAGES))
st.sidebar.caption(f"Profile: {cfg().profile_name} ({cfg().config_id})")
PAGES[choice]()
