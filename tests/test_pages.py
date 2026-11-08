"""Drive the Streamlit pages headlessly with streamlit.testing.v1.AppTest against a fake sheet."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_runner import DAY1, forced, make_store, run, set_cell, sheet_rows, table

from core.schema import Col, RunLogEntry
from core.taxonomy import Outcome

ROOT = Path(__file__).resolve().parent.parent
SAME = {Col.CONTACT_NAME: "Same Person", Col.EMAIL: "same@x.de", Col.PHONE: "+49 30 5540 1100"}
APPROVE = "approve-same@x.de-2026-09-03"
REJECT = "reject-same@x.de-2026-09-03"


@pytest.fixture(autouse=True)
def fresh_caches():
    st.cache_data.clear()  # read_all is cached per version; every test uses its own fake sheet
    yield
    st.cache_data.clear()


def open_page(page: str, store, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    at.session_state["store_override"] = store
    at.session_state["run_date"] = DAY1
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def pending_store():
    store, ws = make_store(sheet_rows(2, **SAME))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=14))
    return store, ws


# ---------------------------------------------------------------- not connected yet


@pytest.mark.parametrize("page", ["app.py", "pages/1_Run.py", "pages/2_Human_Queue.py"])
def test_without_secrets_every_page_shows_setup_help_instead_of_crashing(page):
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    at.run()  # no store_override and no secrets
    assert not at.exception
    assert any("Could not connect to the Google Sheet" in e.value for e in at.error)
    assert any("sheet_id" in m.value for m in at.markdown)
    assert not at.metric and not at.button  # nothing else is rendered


# ---------------------------------------------------------------- landing page


def test_home_page_shows_the_status_counts():
    store, _ = make_store()
    run(store)
    at = open_page("app.py", store)
    assert at.title[0].value == "Invoice Follow-Up Assistant"
    m = metrics(at)
    assert m["Total rows"] == "35" and m["Data quality failed"] == "3"
    assert sum(int(m[k]) for k in ("In progress", "Needing human follow-up", "Data quality failed", "Handled")) == 35


def test_home_page_before_any_run_says_so():
    store, _ = make_store()
    at = open_page("app.py", store)
    assert any("No follow-ups have been run yet" in i.value for i in at.info)


# ---------------------------------------------------------------- run page


def test_run_page_runs_and_shows_a_reconciled_result():
    store, _ = make_store()
    at = open_page("pages/1_Run.py", store)
    assert at.title[0].value == "Run follow-ups"
    assert metrics(at)["Already processed on 2026-08-20"] == "0"
    at.checkbox(key="use_seed").check()
    at.run()
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    m = metrics(at)
    assert m["Total rows"] == "35" and m["Data quality failed"] == "3" and m["Rows decided"] == "35"
    assert any("Every row is accounted for" in s.value for s in at.success)
    assert int(m["Emails sent"]) > 0
    assert int(m["In progress"]) + int(m["Needing human follow-up"]) + int(m["Data quality failed"]) + int(m["Handled"]) == 35
    assert at.dataframe  # the data quality issue table and the recent-runs table
    assert len(store.read_run_log()) == 1 and table(store)["INV-2026-4144#1"][Col.STATUS] == "DQ_FAILED"


def test_running_twice_on_one_date_is_a_no_op_that_the_page_explains():
    store, _ = make_store()
    at = open_page("pages/1_Run.py", store)
    at.button[0].click().run()
    at.button[0].click().run()
    assert not at.exception
    m = metrics(at)
    assert m["Skipped: done today"] == "35" and m["Rows decided"] == "0" and m["Emails sent"] == "0" and m["Calls made"] == "0"
    assert len(store.read_run_log()) == 2
    st.cache_data.clear()  # a new browser session after the 30-second cache has expired
    fresh = open_page("pages/1_Run.py", store)
    assert any("A run now would change nothing" in s.value for s in fresh.success)


def test_run_page_warns_when_another_run_is_in_progress():
    store, _ = make_store()
    store.ensure_schema()
    store.append_run_log(RunLogEntry(run_id="busy", run_date=DAY1, started_at=dt.datetime.now().replace(microsecond=0)))
    at = open_page("pages/1_Run.py", store)
    at.button[0].click().run()
    assert any("Another run is in progress" in w.value for w in at.warning)
    assert len(store.read_run_log()) == 1


# ---------------------------------------------------------------- human queue page


def test_queue_page_shows_three_separate_sections_on_the_real_file():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    at = open_page("pages/2_Human_Queue.py", store)
    assert [h.value for h in at.header] == ["Decisions waiting", "A. Data quality failures", "B. Needing human follow-up"]
    m = metrics(at)
    assert m["Data quality failures"] == "3" and m["Needing human follow-up"] == "6" and m["Decisions waiting"] == "0"
    assert any("Nothing is waiting for a decision" in s.value for s in at.success)
    subheads = [s.value for s in at.subheader]
    assert any(s.startswith("Invoice disputed") for s in subheads) and "Resolve a row" in subheads
    assert len(at.dataframe) >= 4  # the data quality table plus one table per reason


def test_approve_button_needs_a_name_then_approves_for_the_whole_contact():
    store, _ = pending_store()
    at = open_page("pages/2_Human_Queue.py", store)
    assert metrics(at)["Decisions waiting"] == "1" and metrics(at)["Invoices waiting on a decision"] == "2"
    assert at.button(key=APPROVE).disabled  # nobody has said who they are
    assert any("Enter your name" in i.value for i in at.info)

    at.session_state["actor_name"] = "Alex"
    at.run()
    at.button(key=APPROVE).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("approved (1 of 3)" in s.value for s in at.success)
    rows = table(store)
    assert {r[Col.STATUS] for r in rows.values()} == {"IN_PROGRESS"}
    assert {r[Col.EXTENSION_APPROVALS_COUNT] for r in rows.values()} == {"1"}
    assert store.read_audit_log().query("actor == 'Alex'").shape[0] == 2
    assert metrics(at)["Decisions waiting"] == "0"


def test_reject_button_keeps_the_rows_with_a_person():
    store, _ = pending_store()
    at = open_page("pages/2_Human_Queue.py", store, actor_name="Sam")
    at.button(key=REJECT).click().run()
    assert not at.exception
    assert {r[Col.APPROVAL_STATUS] for r in table(store).values()} == {"REJECTED"}
    assert metrics(at)["Needing human follow-up"] == "2" and metrics(at)["Decisions waiting"] == "0"


def test_a_second_person_gets_a_clear_message_when_the_decision_was_already_made():
    store, _ = pending_store()
    first = open_page("pages/2_Human_Queue.py", store, actor_name="Alex")
    second = open_page("pages/2_Human_Queue.py", store, actor_name="Sam")  # still showing the card from before Alex decides
    first.button(key=APPROVE).click().run()
    second.button(key=APPROVE).click().run()  # Sam's page is served from the 30-second cache, so the button is still there
    assert any("may already have been decided" in e.value for e in second.error)
    assert table(store)["INV-1#1"][Col.EXTENSION_APPROVALS_COUNT] == "1"  # not approved twice


def test_resolve_form_closes_a_row():
    store, _ = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.DISPUTE, Outcome.DISPUTE))
    at = open_page("pages/2_Human_Queue.py", store, actor_name="Alex")
    at.radio(key="resolve_what").set_value("Close (mark as handled)")
    at.text_area(key="resolve_note").set_value("Credit note issued")
    next(b for b in at.button if b.label == "Save").click().run()  # form values are sent when Save is pressed
    assert not at.exception, [e.value for e in at.exception]
    row = table(store)["INV-1#1"]
    assert row[Col.STATUS] == "HANDLED" and row[Col.HUMAN_RESOLUTION_NOTE] == "Credit note issued"


def test_sync_button_applies_a_decision_typed_into_the_sheet_and_shows_conflicts():
    store, ws = pending_store()
    set_cell(ws, store, "INV-1#1", Col.APPROVAL_STATUS, "APPROVED")
    set_cell(ws, store, "INV-2#1", Col.APPROVAL_STATUS, "REJECTED")
    at = open_page("pages/2_Human_Queue.py", store)
    next(b for b in at.button if b.label == "Sync sheet edits").click().run()
    assert not at.exception
    assert any("CONFLICT" in w.value for w in at.warning)
    set_cell(ws, store, "INV-2#1", Col.APPROVAL_STATUS, "APPROVED")
    next(b for b in at.button if b.label == "Sync sheet edits").click().run()
    assert {r[Col.STATUS] for r in table(store).values()} == {"IN_PROGRESS"}
    assert any("Applied decisions from the sheet to 2 row(s)" in s.value for s in at.success)


def test_queue_page_with_nothing_to_do_is_calm():
    store, _ = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    at = open_page("pages/2_Human_Queue.py", store)
    text = " ".join(s.value for s in at.success)
    assert "Nothing is waiting for a decision" in text and "No rows need human follow-up" in text and "No rows failed" in text
