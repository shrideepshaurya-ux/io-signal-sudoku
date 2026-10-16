"""Headless demo: allocate SYNTHETIC Rev A, compare with SYNTHETIC Rev B, write workbooks.

    python -m src.demo
"""
from __future__ import annotations

from pathlib import Path

from . import allocator, excel_reader as er, report_generator as rg, revision_diff as rd
from . import sample_data as sd, tq_generator, validator
from .models import RuleConfig, load_config


def main(out_dir: str = "outputs") -> None:
    cfg_path = Path("config/default_rules.json")
    cfg = load_config(str(cfg_path)) if cfg_path.exists() else RuleConfig()
    out = Path(out_dir)
    out.mkdir(exist_ok=True)

    a = er.read_master(sd.to_workbook_bytes(sd.synthetic_with_problems()), "SYNTHETIC_rev_A.xlsx", cfg,
                       revision_label="Rev A")
    res_a = allocator.allocate(a.signals, cfg, revision="Rev A")
    tq_generator.generate_tqs(res_a)
    val_a = validator.validate(res_a, cfg)
    data, info = rg.export_workbook(res_a, cfg, val_a, a.report, source_files=["SYNTHETIC_rev_A.xlsx"])
    (out / "demo_allocation_rev_A.xlsx").write_bytes(data)
    print(f"Rev A: status={res_a.status} allocated={len(res_a.placements)}/{len(res_a.signals)} "
          f"TQs={len(res_a.tqs)} verdict={val_a.headline()}")

    b_df = sd.synthetic_revision_b(sd.synthetic_with_problems())
    b = er.read_master(sd.to_workbook_bytes(b_df), "SYNTHETIC_rev_B.xlsx", cfg, revision_label="Rev B")
    rep = rd.compare_revisions(res_a, b.signals, cfg, "Rev B")
    val_b = validator.validate(rep.result_b, cfg)
    data, _ = rg.export_workbook(rep.result_b, cfg, val_b, b.report, revision=rep,
                                 source_files=["SYNTHETIC_rev_A.xlsx", "SYNTHETIC_rev_B.xlsx"])
    (out / "demo_allocation_rev_B_with_revision_report.xlsx").write_bytes(data)
    print("Revision summary:", rep.summary)
    print(f"Rev B: verdict={val_b.headline()}")


if __name__ == "__main__":
    main()
