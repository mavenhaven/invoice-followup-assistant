"""Plotly figures for the dashboard. No Streamlit here; each function takes a metrics table and returns a Figure.

Design rules (dataviz method):
  - Colors follow the entity, never its rank: email is always blue, phone always orange, and the four outcome
    categories keep the same slot whatever the filter. Slots come in fixed order from the validated palette.
  - One axis per chart, no dual axes. Thin bars, a 2px surface gap between fills, solid hairline grid.
  - A legend whenever there are two or more series; none for a single series (the heading names it).
  - Text wears text colors, never the series color. Values are printed at the bar end (the light-mode aqua and
    yellow are below 3:1 contrast, so visible labels and the table view are required).
Palette validated with the skill's validate_palette.js: light and dark both pass for the first four slots.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go

PALETTES: dict[str, dict[str, Any]] = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "secondary": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]},
}
CHANNEL_SLOT = {"email": 0, "phone": 1}
CATEGORY_SLOT = {"Contact made": 0, "No contact": 1, "Channel error": 2, "System error": 3}
CATEGORY_ORDER = list(CATEGORY_SLOT)
PERCENT_TICKS = [0, 20, 40, 60, 80, 100]
PERCENT_LABELS = [f"{t}%" for t in PERCENT_TICKS]  # written out: explicit tick positions drop the axis suffix
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _p(mode: str) -> dict[str, Any]:
    return PALETTES.get(mode, PALETTES["light"])


def channel_color(channel: str, mode: str = "light") -> str:
    return _p(mode)["series"][CHANNEL_SLOT.get(channel, 7)]


def category_color(category: str, mode: str = "light") -> str:
    slot = CATEGORY_SLOT.get(category)
    return _p(mode)["series"][slot] if slot is not None else _p(mode)["muted"]


def _base(fig: go.Figure, mode: str, height: int, *, legend: bool) -> go.Figure:
    p = _p(mode)
    fig.update_layout(
        height=height, paper_bgcolor=p["surface"], plot_bgcolor=p["surface"], showlegend=legend,
        font={"family": FONT, "size": 13, "color": p["secondary"]}, margin={"l": 8, "r": 24, "t": 36 if legend else 12, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.0, "xanchor": "left", "x": 0, "title": None,
                "font": {"color": p["secondary"]}},
        hoverlabel={"font": {"family": FONT, "size": 13}}, bargap=0.45, uirevision="keep")
    axis = {"gridcolor": p["grid"], "gridwidth": 1, "linecolor": p["axis"], "zeroline": False, "tickfont": {"color": p["muted"]},
            "automargin": True}
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


def _bar(**kw: Any) -> dict[str, Any]:
    return kw


def _surface_gap(mode: str) -> dict[str, Any]:
    return {"line": {"color": _p(mode)["surface"], "width": 2}}  # a 2px surface gap between fills, not a border


def empty_figure(message: str = "Nothing to show for this selection", mode: str = "light") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 14, "color": _p(mode)["muted"]})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _base(fig, mode, 160, legend=False)


def _height(n_bars: int, extra: int = 0) -> int:
    return max(200, 80 + 34 * n_bars + extra)


# ---------------------------------------------------------------- the four charts


def attempts_per_day_figure(table: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Stacked columns: attempts per run date, split by channel."""
    if table.empty:
        return empty_figure("No attempts yet", mode)
    fig = go.Figure()
    for channel in ("email", "phone"):
        part = table[table["channel"] == channel]
        if part.empty:
            continue
        fig.add_bar(x=part["run_date"], y=part["attempts"], name=channel.capitalize(),
                    marker={"color": channel_color(channel, mode), **_surface_gap(mode)},
                    hovertemplate="%{x|%d %b %Y}<br>" + channel.capitalize() + ": %{y} attempts<extra></extra>")
    fig.update_layout(barmode="stack", legend_traceorder="normal")  # legend reads Email, Phone like the stack reads bottom to top
    fig.update_yaxes(title_text="Attempts", rangemode="tozero", dtick=1 if table["attempts"].max() < 8 else None)
    fig.update_xaxes(type="date", tickformat="%d %b")
    return _base(fig, mode, 320, legend=True)


def outcomes_figure(table: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Ranked horizontal bars: contact events per outcome, colored by outcome category."""
    if table.empty:
        return empty_figure("No contact events yet", mode)
    order = list(table.sort_values(["events", "outcome"], ascending=[False, True])["outcome"])  # biggest first, top to bottom
    fig = go.Figure()
    for category in [c for c in CATEGORY_ORDER if c in set(table["category"])] + [c for c in set(table["category"]) if c not in CATEGORY_SLOT]:
        part = table[table["category"] == category]
        fig.add_bar(y=part["outcome"], x=part["events"], orientation="h", name=category, text=part["events"], textposition="outside",
                    cliponaxis=False, textfont={"color": _p(mode)["text"]},
                    marker={"color": category_color(category, mode), **_surface_gap(mode)},
                    hovertemplate="%{y}<br>" + category + ": %{x} events<extra></extra>")
    fig.update_layout(barmode="overlay")  # each outcome belongs to exactly one category, so bars never overlap
    fig.update_yaxes(categoryorder="array", categoryarray=order, autorange="reversed", showgrid=False)
    fig.update_xaxes(rangemode="tozero", title_text="Contact events")
    return _base(fig, mode, _height(len(order), 30), legend=True)


def channel_figure(table: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Grouped horizontal bars: response, success and failure rate for email and phone (one axis, percent)."""
    if table.empty or table["attempts"].sum() == 0:
        return empty_figure("No attempts yet", mode)
    measures = [("response_rate", "Response rate"), ("success_rate", "Success rate"), ("failure_rate", "Failure rate")]
    fig = go.Figure()
    for channel in ("email", "phone"):
        row = table[table["channel"] == channel]
        if row.empty or int(row.iloc[0]["attempts"]) == 0:
            continue
        r = row.iloc[0]
        values = [float(r[m]) * 100 for m, _ in measures]
        fig.add_bar(y=[label for _, label in measures], x=values, orientation="h", name=f"{channel.capitalize()} ({int(r['attempts'])} attempts)",
                    text=[f"{v:.0f}%" for v in values], textposition="outside", cliponaxis=False, textfont={"color": _p(mode)["text"]},
                    marker={"color": channel_color(channel, mode), **_surface_gap(mode)},
                    hovertemplate="%{y}<br>" + channel.capitalize() + ": %{x:.0f}% of " + str(int(r["attempts"])) + " attempts<extra></extra>")
    fig.update_layout(barmode="group", bargap=0.35, bargroupgap=0.08)
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(range=[0, 112], tickvals=PERCENT_TICKS, ticktext=PERCENT_LABELS, title_text="Share of attempts")  # room for labels, ticks stop at 100%
    return _base(fig, mode, 300, legend=True)


def country_figure(table: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Ranked horizontal bars: success rate by country, with the number of attempts printed beside each."""
    if table.empty:
        return empty_figure("No attempts yet", mode)
    t = table.sort_values(["success_rate", "attempts", "country"], ascending=[False, False, True])
    fig = go.Figure(go.Bar(
        y=t["country"], x=t["success_rate"] * 100, orientation="h", text=[f"{r * 100:.0f}% of {n}" for r, n in zip(t["success_rate"], t["attempts"])],
        textposition="outside", cliponaxis=False, textfont={"color": _p(mode)["text"]},
        marker={"color": _p(mode)["series"][0], **_surface_gap(mode)},
        customdata=t[["attempts", "successes", "responses"]].to_numpy(),
        hovertemplate="%{y}<br>%{x:.0f}% success: %{customdata[1]} of %{customdata[0]} attempts<br>%{customdata[2]} responses<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False, categoryorder="array", categoryarray=list(t["country"]))
    fig.update_xaxes(range=[0, 125], tickvals=PERCENT_TICKS, ticktext=PERCENT_LABELS, title_text="Success rate")
    return _base(fig, mode, _height(len(t)), legend=False)


def themes_figure(table: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Ranked horizontal bars: how many of a contact's replies touched each theme."""
    if table.empty:
        return empty_figure("No themes found in their replies", mode)
    t = table.sort_values(["replies", "theme"], ascending=[False, True])
    fig = go.Figure(go.Bar(y=t["theme"], x=t["replies"], orientation="h", text=t["replies"], textposition="outside", cliponaxis=False,
                           textfont={"color": _p(mode)["text"]}, marker={"color": _p(mode)["series"][0], **_surface_gap(mode)},
                           hovertemplate="%{y}: %{x} replies<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False, categoryorder="array", categoryarray=list(t["theme"]))
    fig.update_xaxes(range=[0, float(t["replies"].max()) + 0.6], dtick=1, title_text="Replies")  # headroom for the value label
    return _base(fig, mode, _height(len(t)), legend=False)
