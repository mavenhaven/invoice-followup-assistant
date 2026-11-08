"""Tests for core/charts.py: the dataviz method's rules, checked on the figures themselves."""
from __future__ import annotations

import datetime as dt
import json
import re

import pandas as pd
import pytest

from core import charts
from core.charts import PALETTES, category_color, channel_color

MODES = ["light", "dark"]
DAY = dt.date(2026, 8, 20)


def per_day() -> pd.DataFrame:
    return pd.DataFrame({"run_date": [DAY, DAY, DAY + dt.timedelta(days=2)], "channel": ["email", "phone", "email"], "attempts": [3, 1, 2]})


def mix() -> pd.DataFrame:
    return pd.DataFrame({"category": ["No contact", "Contact made", "Contact made", "Channel error", "System error"],
                         "outcome": ["NO_RESPONSE", "PROMISE_TO_PAY", "PAID", "EMAIL_BOUNCE_HARD", "SYSTEM_ERROR"], "events": [9, 4, 2, 2, 1]})


def channels() -> pd.DataFrame:
    return pd.DataFrame({"channel": ["email", "phone"], "attempts": [10, 5], "responses": [4, 3], "successes": [2, 3], "failures": [5, 1],
                         "response_rate": [0.4, 0.6], "success_rate": [0.2, 0.6], "failure_rate": [0.5, 0.2]})


def countries() -> pd.DataFrame:
    return pd.DataFrame({"country": ["AT", "DE", "CZ"], "attempts": [4, 8, 3], "responses": [3, 4, 1], "successes": [3, 2, 0], "failures": [1, 3, 2],
                         "response_rate": [0.75, 0.5, 0.33], "success_rate": [0.75, 0.25, 0.0], "failure_rate": [0.25, 0.375, 0.67]})


def themes() -> pd.DataFrame:
    return pd.DataFrame({"theme": ["Will pay on a date", "Disputes the invoice"], "replies": [3, 1]})


ALL = [("attempts", lambda m: charts.attempts_per_day_figure(per_day(), m)), ("outcomes", lambda m: charts.outcomes_figure(mix(), m)),
       ("channels", lambda m: charts.channel_figure(channels(), m)), ("country", lambda m: charts.country_figure(countries(), m)),
       ("themes", lambda m: charts.themes_figure(themes(), m))]


# ---------------------------------------------------------------- palette


def test_palettes_match_the_validated_reference_values():
    assert PALETTES["light"]["series"][:4] == ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
    assert PALETTES["dark"]["series"][:4] == ["#3987e5", "#d95926", "#199e70", "#c98500"]
    assert [PALETTES[m]["surface"] for m in MODES] == ["#fcfcfb", "#1a1a19"]
    for m in MODES:
        assert len(PALETTES[m]["series"]) == 8 and len(set(PALETTES[m]["series"])) == 8
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in PALETTES[m]["series"])


@pytest.mark.parametrize("mode", MODES)
def test_colors_follow_the_entity_not_its_rank(mode):
    series = PALETTES[mode]["series"]
    assert channel_color("email", mode) == series[0] and channel_color("phone", mode) == series[1]
    assert [category_color(c, mode) for c in ("Contact made", "No contact", "Channel error", "System error")] == series[:4]
    assert category_color("Something else", mode) == PALETTES[mode]["muted"]  # never a generated hue


@pytest.mark.parametrize("mode", MODES)
def test_a_filter_does_not_repaint_the_survivors(mode):
    full = charts.outcomes_figure(mix(), mode)
    only_contact = charts.outcomes_figure(mix()[mix()["category"] == "Contact made"], mode)
    assert {t.name: t.marker.color for t in only_contact.data} == {"Contact made": category_color("Contact made", mode)}
    assert {t.name: t.marker.color for t in full.data}["Contact made"] == category_color("Contact made", mode)
    few = charts.attempts_per_day_figure(per_day()[per_day()["channel"] == "phone"], mode)
    assert few.data[0].name == "Phone" and few.data[0].marker.color == channel_color("phone", mode)  # still orange, not "first series blue"


# ---------------------------------------------------------------- the rules, on every chart


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name, build", ALL, ids=[n for n, _ in ALL])
def test_every_chart_follows_the_method(name, build, mode):
    fig = build(mode)
    p = PALETTES[mode]
    assert fig.layout.paper_bgcolor == p["surface"] == fig.layout.plot_bgcolor
    assert fig.layout.xaxis.gridcolor == p["grid"] and fig.layout.yaxis.gridcolor == p["grid"]  # hairline, solid
    assert "yaxis2" not in fig.layout and "xaxis2" not in fig.layout  # one axis, never dual
    assert all(t.type == "bar" for t in fig.data)
    if name != "channels":  # (the channel chart keeps its legend even with one series; see its own test)
        assert fig.layout.showlegend is (len(fig.data) >= 2)  # a legend for two or more series, none for a single series
    else:
        assert fig.layout.showlegend is True
    for t in fig.data:
        assert t.marker.line.width == 2 and t.marker.line.color == p["surface"]  # a 2px surface gap, not a border
        if t.text is not None:  # labels wear text colors, never the series color
            assert t.textfont.color == p["text"] and t.textfont.color not in p["series"]
        assert t.hovertemplate and "<extra></extra>" in t.hovertemplate
    assert fig.layout.font.color == p["secondary"] and "system-ui" in fig.layout.font.family
    assert fig.layout.bargap >= 0.35  # thin marks with room around them
    json.loads(fig.to_json())  # serialises, so Streamlit can send it


# ---------------------------------------------------------------- each chart's own structure


def test_attempts_per_day_is_stacked_email_then_phone():
    fig = charts.attempts_per_day_figure(per_day(), "light")
    assert fig.layout.barmode == "stack" and [t.name for t in fig.data] == ["Email", "Phone"]
    assert [t.marker.color for t in fig.data] == ["#2a78d6", "#eb6834"] and list(fig.data[0].y) == [3, 2] and list(fig.data[1].y) == [1]
    assert fig.layout.yaxis.rangemode == "tozero" and fig.layout.yaxis.title.text == "Attempts"
    assert fig.layout.legend.traceorder == "normal"  # Email, then Phone, matching the stack


def test_outcomes_are_ranked_biggest_first_with_values_at_the_bar_ends():
    fig = charts.outcomes_figure(mix(), "light")
    order = list(fig.layout.yaxis.categoryarray)
    assert order[0] == "NO_RESPONSE" and order.index("PROMISE_TO_PAY") < order.index("PAID") and fig.layout.yaxis.autorange == "reversed"
    assert [t.name for t in fig.data] == ["Contact made", "No contact", "Channel error", "System error"]  # fixed category order
    assert all(t.orientation == "h" and t.textposition == "outside" for t in fig.data)
    assert sum(sum(t.x) for t in fig.data) == 18 and fig.layout.barmode == "overlay"


def test_channel_chart_compares_rates_on_one_percent_axis():
    fig = charts.channel_figure(channels(), "light")
    assert [t.name for t in fig.data] == ["Email (10 attempts)", "Phone (5 attempts)"] and fig.layout.barmode == "group"
    assert list(fig.data[0].y) == ["Response rate", "Success rate", "Failure rate"] and list(fig.data[0].x) == [40.0, 20.0, 50.0]
    assert list(fig.data[1].text) == ["60%", "60%", "20%"] and list(fig.layout.xaxis.ticktext)[-1] == "100%"
    one = charts.channel_figure(channels().assign(attempts=[10, 0]), "light")
    # with one channel left the heading ("Email and phone compared") no longer says which one it is, so the legend stays
    assert len(one.data) == 1 and one.layout.showlegend is True and one.data[0].name == "Email (10 attempts)"


def test_country_chart_ranks_by_success_rate_and_prints_the_volume():
    fig = charts.country_figure(countries(), "light")
    assert list(fig.layout.yaxis.categoryarray) == ["AT", "DE", "CZ"] and len(fig.data) == 1
    assert list(fig.data[0].text) == ["75% of 4", "25% of 8", "0% of 3"] and fig.data[0].marker.color == "#2a78d6"  # one series, one hue


def test_themes_chart_is_a_single_hue_ranked_bar():
    fig = charts.themes_figure(themes(), "dark")
    assert list(fig.layout.yaxis.categoryarray) == ["Will pay on a date", "Disputes the invoice"] and fig.data[0].marker.color == "#3987e5"
    assert fig.layout.xaxis.range[1] > 3  # headroom so the value label is not squeezed against the edge


def test_percent_axes_never_label_values_above_100():
    for fig in (charts.channel_figure(channels(), "light"), charts.country_figure(countries(), "light")):
        assert list(fig.layout.xaxis.tickvals) == [0, 20, 40, 60, 80, 100] and fig.layout.xaxis.range[1] > 100
        assert list(fig.layout.xaxis.ticktext) == ["0%", "20%", "40%", "60%", "80%", "100%"]


@pytest.mark.parametrize("builder", [charts.attempts_per_day_figure, charts.outcomes_figure, charts.channel_figure, charts.country_figure,
                                     charts.themes_figure])
def test_empty_data_gives_a_message_not_a_blank_or_broken_chart(builder):
    fig = builder(pd.DataFrame(columns=["x"]), "light")
    assert not fig.data and fig.layout.annotations and fig.layout.annotations[0].text
    assert fig.layout.xaxis.visible is False and fig.layout.yaxis.visible is False


def test_unknown_mode_falls_back_to_light():
    assert charts.attempts_per_day_figure(per_day(), "sepia").layout.paper_bgcolor == PALETTES["light"]["surface"]
