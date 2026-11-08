"""Tests for core/queue.py (what the Human Queue shows) and core/actions.py (what its buttons do)."""
from __future__ import annotations

import datetime as dt

import pytest
from test_runner import DAY1, d, forced, make_store, run, set_cell, sheet_rows, table

from core.actions import ActionError, decide_extension, resolve_row_action, running_run, sync_sheet_edits
from core.queue import build_queue, fmt_money, reason_label
from core.schema import ApprovalStatus, Col, HumanReason, RunLogEntry, Status
from core.taxonomy import Outcome

SAME = {Col.CONTACT_NAME: "Same Person", Col.EMAIL: "same@x.de", Col.PHONE: "+49 30 5540 1100"}
ACTOR = "alex@example.com"
LATER = dt.datetime(2026, 8, 20, 11, 0)


def pending_store(days: int = 14):
    """Two invoices of one contact, one promise to pay in `days` days: both wait for a decision."""
    store, ws = make_store(sheet_rows(2, **SAME))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=days))
    return store, ws


def queue(store, today=DAY1):
    return build_queue(store.read_invoices(), store.read_audit_log(), today, store.load_config())


# ---------------------------------------------------------------- the queue view


def test_formatting_helpers():
    assert fmt_money(1234.5, "EUR") == "1,234.50 EUR" and fmt_money("x", "EUR") == "x EUR"
    assert reason_label("DISPUTE") == "Invoice disputed"
    assert reason_label("EXTENSION_APPROVAL_PENDING", "REJECTED").startswith("Payment date rejected")
    assert reason_label("SOMETHING_NEW") == "Something new" and reason_label("") == ""


def test_one_card_per_contact_and_date_with_the_last_reply():
    store, _ = pending_store()
    view = queue(store)
    assert len(view.pending) == 1 and view.counts["pending_contacts"] == 1 and view.counts["pending_rows"] == 2
    card = view.pending[0]
    assert (card.customer, card.contact_name, card.contact_key) == ("Customer 1 GmbH", "Same Person", "same@x.de")
    assert card.pending_date == d(14) and card.days_out == 14 and card.approvals_used == 0 and card.can_approve
    assert [i["invoice_number"] for i in card.invoices] == ["INV-1", "INV-2"] and card.invoices[0]["outstanding"] == "1,000.00 EUR"
    assert "Subject:" in card.last_reply and card.asked_on == "2026-08-20"  # the contact's own words, not the "covered by" note
    assert view.human.empty and view.dq.empty  # pending decisions are not repeated in the other lists


def test_a_card_cannot_be_approved_once_the_contact_used_three_approvals():
    store, ws = pending_store()
    for key in ("INV-1#1", "INV-2#1"):
        set_cell(ws, store, key, Col.EXTENSION_APPROVALS_COUNT, "3")
    card = queue(store).pending[0]
    assert card.approvals_used == 3 and not card.can_approve


def test_the_real_file_queue_after_a_run():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    view = queue(store)
    assert view.counts["dq"] == 3 and set(view.dq["row_key"]) == {"INV-2026-4471#2", "CN-2026-0118#1", "INV-2026-4144#1"}
    problems = dict(zip(view.dq["row_key"], view.dq["problem"]))
    assert "Exact duplicate of INV-2026-4471#1" in problems["INV-2026-4471#2"]
    assert "not a collectible invoice" in problems["CN-2026-0118#1"] and "Neither a valid email nor a valid phone" in problems["INV-2026-4144#1"]
    assert set(view.human["human_reason"]) == {"READY_NO", "ATTEMPT_CAP_LEGACY", "DISPUTE", "WRONG_CONTACT", "SETTLEMENT"}
    assert view.counts["human"] == 6 and not view.pending
    labels = dict(zip(view.human["invoice_number"], view.human["reason"]))
    assert labels["INV-2026-4177"] == "Invoice disputed" and labels["INV-2026-3980"] == "Marked not ready for contact"
    assert set(view.human.columns) >= {"outstanding", "last_summary", "notes", "email_attempts"}
    assert view.human.loc[view.human["invoice_number"] == "INV-2026-3980", "notes"].iloc[0].startswith("Insolvency")  # notes shown, untouched


def test_an_empty_sheet_gives_an_empty_queue():
    store, _ = make_store(sheet_rows(0))
    store.ensure_schema()
    view = queue(store)
    assert view.counts == {"pending_contacts": 0, "pending_rows": 0, "human": 0, "dq": 0} and view.human.empty and view.dq.empty


# ---------------------------------------------------------------- approving and rejecting


def test_approving_resumes_every_invoice_of_the_promise_and_records_who_did_it():
    store, _ = pending_store()
    msg = decide_extension(store, "same@x.de", approve=True, actor=ACTOR, today=DAY1, now=LATER)
    assert "approved (1 of 3)" in msg and "2 invoices updated" in msg
    rows = table(store)
    assert {r[Col.STATUS] for r in rows.values()} == {"IN_PROGRESS"} and {r[Col.EXTENSION_APPROVALS_COUNT] for r in rows.values()} == {"1"}
    assert {r[Col.NEXT_FOLLOWUP_DATE] for r in rows.values()} == {"2026-09-03"}
    mine = store.read_audit_log().query("actor == @ACTOR")
    assert len(mine) == 2 and set(mine["status_after"]) == {"IN_PROGRESS"} and set(mine["status_before"]) == {"NEEDS_HUMAN"}
    assert not queue(store).pending


def test_rejecting_keeps_the_rows_with_a_person_and_uses_no_approval():
    store, _ = pending_store()
    decide_extension(store, "same@x.de", approve=False, actor=ACTOR, today=DAY1, now=LATER)
    rows = table(store)
    assert {r[Col.APPROVAL_STATUS] for r in rows.values()} == {"REJECTED"} and {r[Col.STATUS] for r in rows.values()} == {"NEEDS_HUMAN"}
    assert {r[Col.EXTENSION_APPROVALS_COUNT] for r in rows.values()} == {"0"}
    view = queue(store)
    assert not view.pending and set(view.human["reason"]) == {"Payment date rejected: decide what happens next"}


def test_the_same_decision_cannot_be_made_twice():
    store, _ = pending_store()
    decide_extension(store, "same@x.de", approve=True, actor=ACTOR, today=DAY1, now=LATER)
    with pytest.raises(ActionError, match="may already have been decided"):
        decide_extension(store, "same@x.de", approve=True, actor="someone.else", today=DAY1, now=LATER)
    assert table(store)["INV-1#1"][Col.EXTENSION_APPROVALS_COUNT] == "1"  # still one approval


def test_actions_need_a_name_and_refuse_during_a_run():
    store, _ = pending_store()
    with pytest.raises(ActionError, match="Enter your name"):
        decide_extension(store, "same@x.de", approve=True, actor="  ", today=DAY1, now=LATER)
    store.append_run_log(RunLogEntry(run_id="busy", run_date=DAY1, started_at=LATER - dt.timedelta(minutes=2)))
    assert running_run(store, LATER) == "busy"
    with pytest.raises(ActionError, match="run is in progress"):
        decide_extension(store, "same@x.de", approve=True, actor=ACTOR, today=DAY1, now=LATER)
    assert running_run(store, LATER + dt.timedelta(hours=1)) is None  # a stale RUNNING row does not block
    assert table(store)["INV-1#1"][Col.STATUS] == "NEEDS_HUMAN"


def test_an_unknown_contact_is_reported():
    store, _ = pending_store()
    with pytest.raises(ActionError, match="no longer in the sheet"):
        decide_extension(store, "nobody@x.de", approve=True, actor=ACTOR, today=DAY1, now=LATER)


def test_approving_past_the_cap_is_refused():
    store, ws = pending_store()
    for key in ("INV-1#1", "INV-2#1"):
        set_cell(ws, store, key, Col.EXTENSION_APPROVALS_COUNT, "3")
    with pytest.raises(ActionError, match="already used 3"):
        decide_extension(store, "same@x.de", approve=True, actor=ACTOR, today=DAY1, now=LATER)
    decide_extension(store, "same@x.de", approve=False, actor=ACTOR, today=DAY1, now=LATER)  # rejecting is always allowed


# ---------------------------------------------------------------- resolving a row


def human_store():
    store, ws = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.DISPUTE, Outcome.DISPUTE))
    return store, ws


def test_closing_a_row_marks_it_handled_with_a_note_and_leaves_notes_alone():
    store, _ = human_store()
    assert table(store)["INV-1#1"][Col.HUMAN_REASON] == "DISPUTE"
    msg = resolve_row_action(store, "INV-1#1", actor=ACTOR, today=DAY1, now=LATER, close=True, note="Credit note issued")
    row = table(store)["INV-1#1"]
    assert msg.endswith("closed.") and row[Col.STATUS] == "HANDLED" and row[Col.HUMAN_RESOLUTION_NOTE] == "Credit note issued"
    assert row[Col.HUMAN_FLAG] == "" and row[Col.NOTES] == ""
    assert store.read_audit_log().query("actor == @ACTOR").iloc[0]["response_text"] == "Credit note issued"


def test_returning_a_row_to_follow_up_can_reset_attempts_and_reenable_channels():
    store, ws = make_store(sheet_rows(1))
    for n in range(40):
        run(store, d(n), f"run-{n}", responder=forced(Outcome.EMAIL_BOUNCE_HARD if n == 0 else Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    row = table(store)["INV-1#1"]
    assert row[Col.STATUS] == "NEEDS_HUMAN" and row[Col.EMAIL_DISABLED] == "Yes" and row[Col.PHONE_ATTEMPTS] == "5"
    resolve_row_action(store, "INV-1#1", actor=ACTOR, today=d(40), now=LATER, reset_attempts=True, reenable_channels=True,
                       next_followup_date=d(42), note="New address confirmed")
    row = table(store)["INV-1#1"]
    assert row[Col.STATUS] == "IN_PROGRESS" and row[Col.NEXT_FOLLOWUP_DATE] == str(d(42))
    assert (row[Col.EMAIL_ATTEMPTS], row[Col.PHONE_ATTEMPTS], row[Col.EMAIL_DISABLED], row[Col.PHONE_DISABLED]) == ("0", "0", "", "")


def test_resolving_refuses_rows_that_are_not_with_a_person():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    with pytest.raises(ActionError, match="not NEEDS_HUMAN"):
        resolve_row_action(store, "INV-2026-4488#1", actor=ACTOR, today=DAY1, now=LATER)
    with pytest.raises(ActionError, match="fix the data"):
        resolve_row_action(store, "INV-2026-4144#1", actor=ACTOR, today=DAY1, now=LATER)
    with pytest.raises(ActionError, match="no longer in the sheet"):
        resolve_row_action(store, "NOPE#1", actor=ACTOR, today=DAY1, now=LATER)


def test_resetting_attempts_applies_to_every_open_invoice_of_the_contact():
    store, _ = make_store(sheet_rows(2, **SAME))
    for n in range(40):
        run(store, d(n), f"run-{n}", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    assert {r[Col.STATUS] for r in table(store).values()} == {"NEEDS_HUMAN"}
    resolve_row_action(store, "INV-1#1", actor=ACTOR, today=d(40), now=LATER, reset_attempts=True)
    rows = table(store)
    assert rows["INV-1#1"][Col.STATUS] == "IN_PROGRESS" and rows["INV-2#1"][Col.STATUS] == "NEEDS_HUMAN"  # only the chosen invoice moves
    assert {r[Col.EMAIL_ATTEMPTS] for r in rows.values()} == {"0"}  # but the contact's counters are reset everywhere


# ---------------------------------------------------------------- decisions typed into the sheet


def test_sync_applies_a_typed_approval_and_logs_it_as_a_sheet_edit():
    store, ws = pending_store()
    set_cell(ws, store, "INV-2#1", Col.APPROVAL_STATUS, "APPROVED")
    result = sync_sheet_edits(store, DAY1, LATER)
    assert result.changed and {r[Col.STATUS] for r in table(store).values()} == {"IN_PROGRESS"}
    log = store.read_audit_log().query("actor == 'sheet edit'")
    assert len(log) == 2 and "edited in the sheet" in log.iloc[0]["response_text"]
    assert not sync_sheet_edits(store, DAY1, LATER).changed  # nothing left to do


def test_sync_reports_conflicts_without_changing_anything():
    store, ws = pending_store()
    set_cell(ws, store, "INV-1#1", Col.APPROVAL_STATUS, "APPROVED")
    set_cell(ws, store, "INV-2#1", Col.APPROVAL_STATUS, "REJECTED")
    result = sync_sheet_edits(store, DAY1, LATER)
    assert not result.changed and [i.code for i in result.issues] == ["CONFLICT"]
    assert {r[Col.STATUS] for r in table(store).values()} == {"NEEDS_HUMAN"}


def test_sync_refuses_during_a_run():
    store, _ = pending_store()
    store.append_run_log(RunLogEntry(run_id="busy", run_date=DAY1, started_at=LATER - dt.timedelta(minutes=1)))
    with pytest.raises(ActionError, match="run is in progress"):
        sync_sheet_edits(store, DAY1, LATER)


def test_status_enums_round_trip_through_the_sheet():
    store, _ = pending_store()
    row = table(store)["INV-1#1"]
    assert Status(row[Col.STATUS]) is Status.NEEDS_HUMAN and ApprovalStatus(row[Col.APPROVAL_STATUS]) is ApprovalStatus.PENDING
    assert HumanReason(row[Col.HUMAN_REASON]) is HumanReason.EXTENSION_APPROVAL_PENDING
