"""Capacity / utilisation summaries computed from placements."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .hardware import Topology
from .models import AllocationResult, RuleConfig


@dataclass
class Utilization:
    totals: dict
    cabinets: pd.DataFrame
    ciocs: pd.DataFrame
    baseplates: pd.DataFrame
    cable_spares: pd.DataFrame


def compute_utilization(result: AllocationResult, cfg: RuleConfig | None = None) -> Utilization:
    cfg = cfg or result.config
    topo = Topology(cfg.hardware, cfg.naming)
    hw = cfg.hardware
    target = cfg.reserved_baseplates()
    bp_occ: dict[tuple[int, int, int], int] = {}
    for p in result.placements.values():
        k = (p.cab, p.cioc, p.bp)
        bp_occ[k] = bp_occ.get(k, 0) + 1
    cabs = sorted({k[0] for k in bp_occ})
    bp_rows = [{"Cabinet": topo.system_cabinet(c), "CIOC": topo.cioc_name(c, i),
                "Baseplate": topo.baseplate_name(c, i, b), "Occupied": n,
                "Capacity": hw.charms_per_baseplate,
                "Utilization %": round(100 * n / hw.charms_per_baseplate, 1)}
               for (c, i, b), n in sorted(bp_occ.items())]
    cioc_rows, cab_rows = [], []
    for c in cabs:
        used_ciocs = sorted({i for (cc, i, _) in bp_occ if cc == c})
        occ_cab = 0
        used_bp_cab = 0
        for i in used_ciocs:
            bps = [b for (cc, ii, b) in bp_occ if cc == c and ii == i]
            occ = sum(bp_occ[(c, i, b)] for b in bps)
            occ_cab += occ
            used_bp_cab += len(bps)
            cioc_rows.append({"Cabinet": topo.system_cabinet(c), "CIOC": topo.cioc_name(c, i),
                              "Controller": topo.controller(c, i), "Baseplates used": len(bps),
                              "Baseplates available": hw.baseplates_per_cioc, "Occupied": occ,
                              "Capacity": hw.slots_per_cioc,
                              "Utilization %": round(100 * occ / hw.slots_per_cioc, 1)})
        empty_bp = hw.baseplates_per_cabinet - used_bp_cab
        cab_rows.append({"Cabinet": topo.system_cabinet(c), "Marshalling cabinet": topo.marshalling_cabinet(c),
                         "CIOCs used": len(used_ciocs), "Baseplates used": used_bp_cab,
                         "Empty baseplates": empty_bp, "Spare target (baseplates)": target,
                         "Spare met": empty_bp >= target, "Occupied positions": occ_cab,
                         "Capacity": hw.slots_per_cabinet,
                         "Utilization %": round(100 * occ_cab / hw.slots_per_cabinet, 1)})
    cable_df = pd.DataFrame(result.cable_blocks)
    spare_pairs = int(cable_df["Spare pairs"].fillna(0).sum()) if not cable_df.empty else 0
    totals = {
        "Cabinets required": len(cabs),
        "CIOCs used": len(cioc_rows),
        "Baseplates used": len(bp_rows),
        "Occupied CHARM positions": len(result.placements),
        "Available positions (used cabinets)": len(cabs) * hw.slots_per_cabinet,
        "Empty baseplates (used cabinets)": sum(r["Empty baseplates"] for r in cab_rows),
        "Spare target per cabinet (baseplates)": target,
        "Cable spare pairs": spare_pairs,
        "Allocated signals": len(result.placements),
        "Unallocated signals": len(result.signals) - len(result.placements),
        "Overall utilization %": round(100 * len(result.placements) / (len(cabs) * hw.slots_per_cabinet), 1)
        if cabs else 0.0,
    }
    return Utilization(totals, pd.DataFrame(cab_rows), pd.DataFrame(cioc_rows),
                       pd.DataFrame(bp_rows), cable_df)
