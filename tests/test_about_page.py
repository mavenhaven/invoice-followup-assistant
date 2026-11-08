"""The About page, the drift checks' own sanity, and the time-zone clock."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_rules_catalog import defined_tests, resolve
from test_runner import make_store, run

from core import rules_catalog as cat
from core.clock import DEFAULT_TIMEZONE, today_in
from core.schema import Config

ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc


@pytest.fixture(autouse=True)
def fresh_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def open_about(**state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "pages" / "4_About.py"), default_timeout=60)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# ---------------------------------------------------------------- the checker would notice a mistake


def test_the_drift_checks_can_actually_fail():
    assert "test_nothing_like_this_exists" not in defined_tests("tests/test_rules.py")
    with pytest.raises(AttributeError):
        resolve("core.rules.no_such_function")
    with pytest.raises(AssertionError):
        resolve("no_such_package.thing")
    bad = cat.BusinessRule("BR-99", "x", "text {attempt_cap}", "e", ("core.rules.decide_action",), ("tests/test_rules.py::test_gone",))
    assert bad.tests[0].partition("::")[2] not in defined_tests("tests/test_rules.py")
    with pytest.raises(KeyError):
        cat.BusinessRule("BR-98", "x", "text {not_a_setting}", "e", (), ()).rendered(Config())


# ---------------------------------------------------------------- the page


def test_the_page_opens_before_the_app_is_connected_to_a_sheet():
    at = open_about()
    assert at.title[0].value == "About the Invoice Follow-Up Assistant"
    assert [h.value for h in at.header] == ["What it is for", "How it works", "The Google Sheet", "Row status", "Outcome taxonomy",
                                             "Data quality rules", "Business rules implemented", "Settings", "Assumptions", "Limitations"]
    assert not at.error and any("Showing the defaults" in m.value for m in at.markdown)


def test_it_has_an_explicit_business_rules_section_with_every_rule():
    at = open_about()
    text = " ".join(m.value for m in at.markdown)
    for r in cat.RULES:
        assert f"**{r.id} · " in text, r.id
    assert f"**{len(cat.RULES)} rules**" in text
    assert text.count("**Code:**") == len(cat.RULES) and text.count("**Tests:**") == len(cat.RULES)


def test_the_rule_search_narrows_the_list():
    at = open_about(about_find="approval")
    text = " ".join(m.value for m in at.markdown)
    shown = [r.id for r in cat.RULES if f"**{r.id} · " in text]
    assert {"BR-10", "BR-11", "BR-25"} <= set(shown) and "BR-02" not in shown and len(shown) < len(cat.RULES)
    nothing = open_about(about_find="zebra crossing")
    assert any("No rule matches that" in i.value for i in nothing.info)


def test_diagrams_and_tables_are_present():
    at = open_about()
    assert len(at.get("graphviz_chart")) == 2
    tables = [d.value for d in at.dataframe]
    assert any("Outcome" in t.columns and len(t) == 26 for t in tables)  # the full taxonomy
    assert any("Rule" in t.columns and len(t) == 18 for t in tables)  # the data quality rules
    columns = next(t for t in tables if list(t.columns) == ["Column", "How the app uses it"])
    assert len(columns) == 18 and columns.iloc[-1]["Column"] == "Notes" and "never writes" in columns.iloc[-1]["How the app uses it"]
    assert any(list(t.columns) == ["#", "Status", "Assumption"] and len(t) == len(cat.ASSUMPTIONS) for t in tables)


def test_the_taxonomy_filter_works():
    at = open_about()
    at.multiselect(key="about_categories").set_value(["System error"]).run()
    assert not at.exception
    shown = next(d.value for d in at.dataframe if "Outcome" in d.value.columns)
    assert set(shown["Category"]) == {"System error"} and len(shown) == 3


def test_it_offers_the_design_document_for_download():
    at = open_about()
    assert len(at.get("download_button")) == 1


def test_live_settings_from_the_sheet_flow_into_the_rules():
    store, _ = make_store()
    run(store)
    store.set_config("attempt_cap", 7)
    store.set_config("max_approvals", 2)
    at = open_about(store_override=store)
    text = " ".join(m.value for m in at.markdown)
    assert "A contact gets at most 7 counted attempts on email and 7 on phone" in text
    assert "at most 2 extensions" in text and "Showing the defaults" not in text
    settings = next(d.value for d in at.dataframe if list(d.value.columns) == ["Setting", "What it controls", "Value"])
    assert dict(zip(settings["Setting"], settings["Value"]))["attempt_cap"] == "7"
    assert dict(zip(settings["Setting"], settings["Value"]))["min_amount_EUR"] == "10"


# ---------------------------------------------------------------- the clock


def test_today_follows_the_configured_time_zone_not_the_servers():
    late_utc = dt.datetime(2026, 8, 20, 23, 30, tzinfo=UTC)  # 20 Aug 23:30 in UTC is already 21 Aug in Berlin
    assert today_in("UTC", late_utc) == dt.date(2026, 8, 20)
    assert today_in("Europe/Berlin", late_utc) == dt.date(2026, 8, 21)
    assert today_in("America/New_York", late_utc) == dt.date(2026, 8, 20)
    assert today_in("Pacific/Kiritimati", dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)) == dt.date(2026, 8, 21)  # UTC+14


def test_winter_and_summer_time_are_handled():
    assert today_in("Europe/Berlin", dt.datetime(2026, 1, 15, 23, 30, tzinfo=UTC)) == dt.date(2026, 1, 16)  # UTC+1
    assert today_in("Europe/Berlin", dt.datetime(2026, 7, 15, 22, 30, tzinfo=UTC)) == dt.date(2026, 7, 16)  # UTC+2


def test_an_unknown_zone_falls_back_instead_of_crashing():
    assert today_in("Mars/Olympus", dt.datetime(2026, 8, 20, 10, 0)) == dt.date(2026, 8, 20)
    assert isinstance(today_in("Mars/Olympus"), dt.date) and isinstance(today_in(), dt.date) and DEFAULT_TIMEZONE == "Europe/Berlin"


def test_the_run_page_defaults_to_today_in_the_configured_zone():
    store, _ = make_store()
    store.ensure_schema()
    store.set_config("timezone", "Pacific/Kiritimati")
    at = AppTest.from_file(str(ROOT / "pages" / "1_Run.py"), default_timeout=60)
    at.session_state["store_override"] = store
    before = today_in("Pacific/Kiritimati")
    at.run()
    after = today_in("Pacific/Kiritimati")
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert any(l in (f"Already processed on {before.isoformat()}", f"Already processed on {after.isoformat()}") for l in labels)
    assert any("Pacific/Kiritimati" in (d.help or "") for d in at.date_input)
