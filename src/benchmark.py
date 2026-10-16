"""Live scaling measurements on synthetic data (real solver runs, nothing simulated)."""
from __future__ import annotations

import time
from typing import Callable, Optional

import pandas as pd

from . import allocator, sample_data as sd, validator
from .models import RuleConfig
from .utilization import compute_utilization


def run_scaling(sizes: list[int], cfg: RuleConfig, time_limit: float = 10.0,
                progress: Optional[Callable[[int, int, str], None]] = None) -> pd.DataFrame:
    from . import excel_reader as er
    c = cfg.model_copy(deep=True)
    c.solver.time_limit_s = time_limit
    rows = []
    for i, n in enumerate(sizes):
        if progress:
            progress(i, len(sizes), f"{n} signals")
        df = sd.synthetic_large(n, seed=7)
        rr = er.read_master(sd.to_workbook_bytes(df), "bench.xlsx", c)
        t = time.time()
        res = allocator.allocate(rr.signals, c, revision=f"bench-{n}", time_limit=time_limit)
        dt = time.time() - t
        v = validator.validate(res, c)
        u = compute_utilization(res, c)
        rows.append({"Signals": n, "Allocation time (s)": round(dt, 2), "Status": res.status,
                     "Allocated": len(res.placements), "Cabinets": u.totals["Cabinets required"],
                     "Violations": len(v.violations), "Fully valid": v.fully_valid})
    if progress:
        progress(len(sizes), len(sizes), "done")
    return pd.DataFrame(rows)
