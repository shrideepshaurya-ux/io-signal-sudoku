"""Plotly figure builders for the Streamlit front-end (no Streamlit import here)."""
from __future__ import annotations

from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .hardware import Topology
from .models import AllocationResult, RuleConfig

PALETTE = ["#2a6fdb", "#e07b00", "#1a9e6f", "#c23b8a", "#7a52cc", "#c9a100", "#d6402f", "#0d8aa8",
           "#6b7a2a", "#8c564b", "#4c4c9d", "#b03a2e"]
FIXED = {"empty (free)": "#e6e9ee", "spare baseplate (reserved)": "#bfc5ce",
         "Unchanged": "#9db7e0", "Added": "#1a9e6f", "Moved": "#d6402f", "Modified": "#e07b00",
         "Deleted": "#7a52cc", "Moved (old position)": "#d6402f"}


def _category(result: AllocationResult, sid: int, color_by: str) -> str:
    s = result.signals[sid]
    p = result.placements[sid]
    if color_by == "Segregation (A/B)":
        return s.seg or "no designation"
    if color_by == "Logical group":
        return s.group or "(no group)"
    if color_by == "CHARM type":
        return p.charm_type or "?"
    return s.io_key or s.io_raw or "?"


def cabinet_map(result: AllocationResult, cfg: RuleConfig, cab: int, color_by: str = "IO type",
                highlight: Optional[dict[int, str]] = None, title: str = ""):
    """Return (figure, lookup) where lookup maps (slot_x, row_y) -> sid or None."""
    topo = Topology(cfg.hardware, cfg.naming)
    hw = cfg.hardware
    reserved = cfg.reserved_baseplates()
    occupied = {p.loc: sid for sid, p in result.placements.items() if p.cab == cab}
    cats: dict[str, dict[str, list]] = {}
    lookup: dict[tuple[int, int], Optional[int]] = {}
    yticks, ylabels, cioc_labels = [], [], []
    row = 0
    for ci in range(hw.ciocs_per_cabinet):
        usable = topo.usable_baseplates(ci, reserved)
        top = row
        for bp in range(hw.baseplates_per_cioc):
            y = -row
            yticks.append(y)
            ylabels.append(topo.baseplate_name(cab, ci, bp))
            for slot in range(hw.charms_per_baseplate):
                sid = occupied.get((cab, ci, bp, slot))
                x = slot + 1
                if sid is not None:
                    s = result.signals[sid]
                    p = result.placements[sid]
                    if highlight is not None:
                        cat = highlight.get(sid, "Unchanged")
                    else:
                        cat = _category(result, sid, color_by)
                    hover = (f"<b>{s.tag}</b><br>{s.io_raw} | cable {s.cable_id} pair {s.pair}"
                             f"<br>{p.charm_type} / {p.terminal_block}"
                             f"<br>group {s.group or '-'} | seg {s.seg or '-'}"
                             f"<br>{topo.location_str(p)}")
                else:
                    cat = "spare baseplate (reserved)" if bp >= usable else "empty (free)"
                    hover = f"{topo.baseplate_name(cab, ci, bp)} slot {x}: {cat}"
                d = cats.setdefault(cat, {"x": [], "y": [], "t": []})
                d["x"].append(x)
                d["y"].append(y)
                d["t"].append(hover)
                lookup[(x, y)] = sid
            row += 1
        cioc_labels.append((-(top + row - 1) / 2 * -1 if False else -(top + (row - 1 - top) / 2),
                            topo.cioc_name(cab, ci)))
        row += 1  # gap between CIOCs
    fig = go.Figure()
    colors, i = {}, 0
    for cat in sorted(cats, key=lambda c: (c in FIXED, c)):
        if cat in FIXED:
            colors[cat] = FIXED[cat]
        else:
            colors[cat] = PALETTE[i % len(PALETTE)]
            i += 1
        d = cats[cat]
        fig.add_trace(go.Scatter(x=d["x"], y=d["y"], mode="markers", name=cat, hovertext=d["t"],
                                 hoverinfo="text",
                                 marker=dict(symbol="square", size=15, color=colors[cat],
                                             line=dict(width=1, color="#ffffff"))))
    for y, label in cioc_labels:
        fig.add_annotation(x=hw.charms_per_baseplate + 0.9, y=y, text=f"<b>{label}</b>", showarrow=False,
                           xanchor="left", font=dict(size=11))
    fig.update_layout(
        title=title or f"{topo.system_cabinet(cab)} - {color_by}",
        height=max(420, 24 * row + 120), margin=dict(l=10, r=130, t=50, b=10),
        xaxis=dict(title="CHARM slot", tickmode="linear", dtick=1, range=[0.4, hw.charms_per_baseplate + 0.6],
                   fixedrange=True),
        yaxis=dict(tickmode="array", tickvals=yticks, ticktext=ylabels, fixedrange=True, showgrid=False),
        legend=dict(orientation="h", y=-0.08))
    return fig, lookup


def tq_chart(tq_df: pd.DataFrame):
    if tq_df.empty:
        return None
    d = tq_df.groupby(["Category", "Severity"]).size().reset_index(name="TQs")
    fig = px.bar(d, x="TQs", y="Category", color="Severity", orientation="h",
                 color_discrete_map={"ERROR": "#d6402f", "WARNING": "#e07b00", "INFO": "#2a6fdb"})
    fig.update_layout(height=max(260, 60 * d["Category"].nunique() + 120), yaxis_title=None,
                      margin=dict(l=10, r=10, t=30, b=10))
    return fig


def scaling_chart(df: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["Signals"], y=df["Allocation time (s)"], mode="lines+markers",
                             name="Allocation time (s)"))
    fig.add_trace(go.Scatter(x=df["Signals"], y=df["Cabinets"], mode="lines+markers", name="Cabinets",
                             yaxis="y2"))
    fig.update_layout(xaxis_title="Signals", yaxis_title="Seconds",
                      yaxis2=dict(title="Cabinets", overlaying="y", side="right"),
                      height=360, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h"))
    return fig
