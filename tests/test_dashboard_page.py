"""Drive pages/3_Dashboard.py headlessly against a fake sheet that has had a few days of runs."""
from __future__ import annotations

import random
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_runner import d, forced, make_store, run, set_cell, sheet_rows

from core.schema import Col
from core.taxonomy import Outcome

ROOT = Path(__file__).resolve().parent.parent
VELTNER = "t.berger@veltner-gastro.at"


@pytest.fixture(autouse=True)
def fresh_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def open_dashboard(store, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "pages" / "3_Dashboard.py"), default_timeout=90)
    at.session_state["store_override"] = store
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def charts(at: AppTest) -> int:
    return len(at.get("plotly_chart"))


@pytest.fixture(scope="module")
def worked_store():
    """The real file after six days of random mock replies."""
    store, ws = make_store()
    rng = random.Random(5)
    import datetime as dt
    for n in range(6):
        from core.runner import run_daily
        run_daily(store, d(n), f"run-{n}", rng, now=dt.datetime.combine(d(n), dt.time(9)))
    return store


def counted_attempts(store, countries=None) -> int:
    audit, inv = store.read_audit_log(), store.read_invoices().set_index(Col.ROW_KEY)
    yes = audit[audit["counted_as_attempt"] == "Yes"]
    if countries:
        yes = yes[yes["row_key"].map(inv[Col.COUNTRY]).isin(countries)]
    return len(yes)


# ---------------------------------------------------------------- before and after runs


def test_before_any_run_it_shows_row_counts_and_waits_for_data():
    store, _ = make_store()
    at = open_dashboard(store)
    m = metrics(at)
    assert m["Total rows"] == "35" and m["Attempts"] == "0" and m["Response rate"] == "–"
    assert any("No follow-ups have been run yet" in i.value for i in at.info)
    assert charts(at) == 0


def test_after_runs_the_kpis_match_the_audit_log(worked_store):
    st.cache_data.clear()
    at = open_dashboard(worked_store)
    m = metrics(at)
    assert m["Attempts"] == str(counted_attempts(worked_store)) and int(m["Attempts"]) > 20
    assert m["Total rows"] == "35" and "adds up" in [mm.delta for mm in at.metric if mm.label == "Total rows"][0]
    assert sum(int(m[k]) for k in ("In progress", "Needing human follow-up", "Data quality failed", "Handled")) == 35
    for label in ("Response rate", "Success rate", "Failure rate"):
        assert m[label].endswith("%")
    assert m["Data quality failed"] == "3"
    assert [h.value for h in at.header] == ["Drill down to one contact"]
    assert charts(at) >= 4  # attempts per day, outcomes, channels, country (plus themes once a contact is picked)


def test_every_chart_has_a_table_view(worked_store):
    at = open_dashboard(worked_store)
    assert charts(at) >= 4
    assert len([e for e in at.expander if e.label.startswith("Table view")]) >= 4
    assert any(e.label.startswith("Outstanding amounts by currency") for e in at.expander)


# ---------------------------------------------------------------- filters


def test_country_filter_narrows_rows_and_attempts(worked_store):
    at = open_dashboard(worked_store, dash_countries=["AT"])
    at_rows = int((worked_store.read_invoices()[Col.COUNTRY] == "AT").sum())
    m = metrics(at)
    assert m["Total rows"] == str(at_rows) and m["Attempts"] == str(counted_attempts(worked_store, ["AT"]))
    assert any(f"Showing {at_rows} of 35 rows" in c.value for c in at.caption)


def test_two_countries_add_up(worked_store):
    at = open_dashboard(worked_store, dash_countries=["AT", "HU"])
    inv = worked_store.read_invoices()
    assert metrics(at)["Total rows"] == str(int(inv[Col.COUNTRY].isin(["AT", "HU"]).sum()))
    assert metrics(at)["Attempts"] == str(counted_attempts(worked_store, ["AT", "HU"]))


def test_search_by_customer_email_or_invoice_number(worked_store):
    assert metrics(open_dashboard(worked_store, dash_search="veltner"))["Total rows"] == "3"
    assert metrics(open_dashboard(worked_store, dash_search="T.BERGER@"))["Total rows"] == "3"
    assert metrics(open_dashboard(worked_store, dash_search="INV-2026-4488"))["Total rows"] == "1"
    both = open_dashboard(worked_store, dash_search="gmbh", dash_countries=["AT"])
    inv = worked_store.read_invoices()
    assert metrics(both)["Total rows"] == str(int(((inv[Col.COUNTRY] == "AT") & inv[Col.CUSTOMER].str.lower().str.contains("gmbh")).sum()))


def test_a_filter_that_matches_nothing_says_so(worked_store):
    at = open_dashboard(worked_store, dash_search="no such customer")
    assert any("No rows match these filters" in w.value for w in at.warning) and not at.metric


# ---------------------------------------------------------------- drill-down


def test_drilling_down_to_one_contact(worked_store):
    at = open_dashboard(worked_store, dash_contact=VELTNER)
    m = metrics(at)
    audit = worked_store.read_audit_log()
    mine = audit[(audit["contact_key"] == VELTNER) & (audit["contact_event_id"] != "")]
    dates = mine["run_date"].nunique()
    assert m["Invoices"] == "3" and m["Followed up on"] == f"{dates} dates" and dates >= 1
    assert any(s.value == "Every follow-up" for s in at.subheader)
    assert any("Themes in their replies" == s.value for s in at.subheader)
    assert "/" in m["Attempts (email / phone)"] and m["Approvals used"].endswith("of 3")
    assert charts(at) >= 5  # the four overview charts plus the themes chart


def test_a_contact_never_followed_up_says_so():
    rows = sheet_rows(2)
    rows[2][rows[0].index(Col.READY_FOR_CONTACT)] = "No"  # the second contact is on hold, so it is never contacted
    store, _ = make_store(rows)
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    at = open_dashboard(store, dash_contact="p2@c2.de")
    assert metrics(at)["Followed up on"] == "never"
    assert any("has not been followed up yet" in i.value for i in at.info)
    assert not any(s.value == "Themes in their replies" for s in at.subheader)


def test_the_drill_down_shows_the_replies_for_a_contact_with_a_promise():
    store, _ = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=3))
    at = open_dashboard(store)
    frames = [df.value for df in at.dataframe]
    history = next(f for f in frames if "What came back" in f.columns)
    assert len(history) == 1 and history.iloc[0]["Outcome"] == "PROMISE_TO_PAY" and history.iloc[0]["Channel"] == "email"
    assert "payment" in history.iloc[0]["What came back"].lower() or "pay" in history.iloc[0]["What came back"].lower()
    assert any("Most frequent words" in c.value for c in at.caption)


# ---------------------------------------------------------------- reconciliation and system errors


def test_an_unrecognised_status_makes_the_page_warn_that_the_groups_do_not_add_up():
    store, ws = make_store(sheet_rows(2))
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    set_cell(ws, store, "INV-2#1", Col.STATUS, "WHATEVER")
    at = open_dashboard(store)
    assert any("unrecognised status" in e.value for e in at.error)
    assert [mm.delta for mm in at.metric if mm.label == "Total rows"] == ["does not add up"]


def test_system_errors_are_reported_separately_from_attempts():
    store, _ = make_store(sheet_rows(2))

    def broken(*a, **k):
        raise RuntimeError("gateway down")
    run(store, responder=broken)
    at = open_dashboard(store)
    assert metrics(at)["Attempts"] == "0"
    assert any("2 system errors" in c.value and "never use an attempt" in c.value for c in at.caption)


def test_currency_table_never_adds_across_currencies(worked_store):
    at = open_dashboard(worked_store)
    pivot = next(df.value for df in at.dataframe if "All rows" in df.value.columns)
    assert set(pivot.index) == {"CZK", "EUR", "HUF", "RON"}
    inv = worked_store.read_invoices()
    eur = sum(float(v) for v, c in zip(inv[Col.AMOUNT_OUTSTANDING], inv[Col.CURRENCY]) if c == "EUR")
    assert float(pivot.loc["EUR", "All rows"].replace(",", "")) == pytest.approx(eur, abs=0.05)
