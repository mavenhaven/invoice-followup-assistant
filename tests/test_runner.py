"""Tests for core/runner.py: the daily run against a fake in-memory Google Sheet."""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
from pathlib import Path

import pandas as pd
import pytest
from test_sheets import FakeAPIError, FakeClient, FakeSpreadsheet, FakeWorksheet

from core.mock_responses import generate_response
from core.rules import RowState, approve_contact_extension
from core.runner import RunInProgressError, run_daily
from core.schema import (
    Channel, Col, INVOICES_EXISTING_COLUMNS, RunLogEntry, RunState,
)
from core.sheets import SheetStore
from core.taxonomy import Outcome, info

FIXTURE = Path(__file__).parent / "fixtures" / "overdue_invoices.csv"
DAY1 = dt.date(2026, 8, 20)
NOW = dt.datetime(2026, 8, 20, 9, 0)
E, P = Channel.EMAIL, Channel.PHONE


def d(n: int) -> dt.date:
    return DAY1 + dt.timedelta(days=n)


def make_store(rows: list[list[str]] | None = None, **kw) -> tuple[SheetStore, FakeWorksheet]:
    if rows is None:
        rows = list(csv.reader(FIXTURE.open(encoding="utf-8")))
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    return SheetStore(FakeClient(FakeSpreadsheet([ws])), "sheet", **kw), ws


def run(store: SheetStore, day: dt.date = DAY1, run_id: str = "run-1", seed: int = 7, hour: int = 9, **kw):
    return run_daily(store, day, run_id, random.Random(seed), now=dt.datetime.combine(day, dt.time(hour)), **kw)


def forced(email: Outcome, phone: Outcome, days: int | None = None):
    """A responder that always returns the same outcome per channel."""
    def responder(channel, contact, invoice, rng, *, today, **kw):
        outcome = email if channel is E else phone
        return generate_response(channel, contact, invoice, rng, today=today, forced_outcome=outcome,
                                 promised_days=days if info(outcome).date_kind else None)
    return responder


def table(store: SheetStore) -> dict[str, dict[str, str]]:
    return {r[Col.ROW_KEY]: r for r in store.read_invoices().to_dict("records")}


def audit(store: SheetStore):
    return store.read_audit_log()


def sheet_rows(count: int, **common: str) -> list[list[str]]:
    """A tiny sheet of single-invoice contacts, header = the 18 existing columns."""
    header = list(INVOICES_EXISTING_COLUMNS)
    out = [header]
    for n in range(count):
        row = {Col.INVOICE_NUMBER: f"INV-{n + 1}", Col.CUSTOMER: f"Customer {n + 1} GmbH", Col.COUNTRY: "DE",
               Col.CONTACT_NAME: f"Person {n + 1}", Col.EMAIL: f"p{n + 1}@c{n + 1}.de", Col.PHONE: f"+49 30 5540 11{n + 1:02d}",
               Col.INVOICE_DATE: "2026-06-01", Col.DUE_DATE: "2026-07-01", Col.INVOICE_AMOUNT: "1000",
               Col.AMOUNT_OUTSTANDING: "1000", Col.CURRENCY: "EUR", Col.CONTACT_ATTEMPTS: "0", Col.READY_FOR_CONTACT: "Yes"}
        row.update(common)
        out.append([row.get(c, "") for c in header])
    return out


def set_cell(ws: FakeWorksheet, store: SheetStore, row_key: str, column: str, value: str) -> None:
    df = store.read_invoices()
    sheet_row = int(df.loc[df[Col.ROW_KEY] == row_key, "_sheet_row"].iloc[0])
    col = ws.row_values(1).index(column) + 1
    while len(ws.grid[sheet_row - 1]) < col:
        ws.grid[sheet_row - 1].append("")
    ws.grid[sheet_row - 1][col - 1] = value


# ---------------------------------------------------------------- first run on the real file


def test_first_run_on_the_real_file_reconciles_and_stamps_every_row():
    store, _ = make_store()
    res = run(store)
    assert res.state is RunState.DONE and res.reconciled and res.rows_total == 35
    assert sum(res.buckets.values()) == 35 and res.buckets["dq_failed"] == 3
    assert res.rows_initialized == 35 and res.rows_processed == 35 and res.rows_skipped_already_processed == 0
    rows = table(store)
    assert all(r[Col.LAST_PROCESSED_DATE] == "2026-08-20" for r in rows.values())
    final = {name: sum(1 for r in rows.values() if r[Col.STATUS] in statuses)
             for name, statuses in [("dq_failed", {"DQ_FAILED"}), ("needs_human", {"NEEDS_HUMAN"}),
                                    ("in_progress", {"IN_PROGRESS", "NEW"}), ("handled", {"HANDLED"})]}
    assert final == res.buckets  # what the run reports equals what is in the sheet
    assert store.read_config()["legacy_migration_done"] == "true"


def test_failed_and_human_rows_are_never_contacted_on_day_one():
    store, _ = make_store()
    run(store)
    contacted = set(audit(store).query("counted_as_attempt == 'Yes'")["row_key"])
    for key in ("INV-2026-4471#2", "CN-2026-0118#1", "INV-2026-4144#1", "INV-2026-4265#1", "INV-2026-3980#1",
                "INV-2026-4415#1", "INV-2026-3902#1", "INV-2026-4177#1", "INV-2026-4029#1", "INV-2026-3955#1",
                "INV-2026-4512#1"):
        assert key not in contacted, key


def test_notes_are_never_changed():
    store, ws = make_store()
    before = [r.get(Col.NOTES, "") for r in table(store).values()]
    for n in range(3):
        run(store, d(n), f"run-{n}", seed=n)
    assert [r.get(Col.NOTES, "") for r in table(store).values()] == before


def test_a_combined_contact_is_one_attempt_over_all_of_the_contacts_invoices():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    log = audit(store)
    veltner = log[log["row_key"].isin(["INV-2026-4288#1", "INV-2026-4351#1", "INV-2026-4390#1"])]
    assert len(veltner) == 3 and veltner["contact_event_id"].nunique() == 1
    assert (veltner["counted_as_attempt"] == "Yes").sum() == 1  # one attempt, not three
    assert set(veltner["channel"]) == {"email"} and set(veltner["outcome"]) == {"NO_RESPONSE"}
    rows = table(store)
    for key in veltner["row_key"]:
        assert rows[key][Col.EMAIL_ATTEMPTS] == "1" and rows[key][Col.CONTACT_ATTEMPTS] == "1"
        assert rows[key][Col.LAST_CONTACT_DATE] == "2026-08-20" and rows[key][Col.NEXT_FOLLOWUP_DATE] == "2026-08-22"


def test_contacts_without_a_usable_email_are_called():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    log = audit(store).set_index("row_key")
    for key in ("INV-2026-4301#1", "INV-2026-4276#1", "INV-2026-4066#1"):
        assert log.loc[key, "channel"] == "phone"
    assert log.loc["INV-2026-4488#1", "channel"] == "email"


def test_run_log_matches_the_audit_log():
    store, _ = make_store()
    res = run(store)
    counted = audit(store).query("counted_as_attempt == 'Yes'")
    assert (counted["channel"] == "email").sum() == res.attempts_email
    assert (counted["channel"] == "phone").sum() == res.attempts_phone
    log = store.read_run_log().iloc[0]
    assert (log["run_id"], log["state"], log["rows_total"]) == ("run-1", "DONE", "35")
    assert int(log["attempts_email"]) == res.attempts_email and int(log["rows_escalated"]) == res.rows_escalated
    assert log["finished_at"] == "2026-08-20 09:00:00" and json.loads(log["errors"]) == []


# ---------------------------------------------------------------- idempotency and determinism


def test_a_second_run_on_the_same_day_changes_nothing():
    store, ws = make_store()
    run(store)
    grid = [list(r) for r in ws.grid]
    n_audit = len(audit(store))
    res2 = run(store, run_id="run-2", hour=15)
    assert [list(r) for r in ws.grid] == grid  # the invoices sheet is untouched
    assert len(audit(store)) == n_audit and res2.cells_written == 0
    assert res2.rows_skipped_already_processed == 35 and res2.rows_processed == 0 and res2.contact_events == 0
    assert res2.reconciled and len(store.read_run_log()) == 2  # but the run itself is logged


def test_the_same_seed_gives_the_same_sheet():
    (s1, w1), (s2, w2) = make_store(), make_store()
    run(s1, seed=11)
    run(s2, seed=11)
    assert w1.grid == w2.grid and audit(s1).equals(audit(s2))
    (s3, w3) = make_store()
    run(s3, seed=12)
    assert w3.grid != w1.grid


def test_writes_are_batched_and_batch_size_does_not_change_the_result():
    (s1, w1), (s2, w2) = make_store(), make_store()
    run(s1, flush_every=1)
    run(s2, flush_every=1000)
    assert w1.grid == w2.grid
    calls = [c for c in w2.calls if c[0] == "batch_update"]
    assert 1 <= len(calls) <= 12  # a handful of requests for ~1,000 cells, not one per cell


# ---------------------------------------------------------------- failures


def failing_for(invoice_number: str):
    def responder(channel, contact, invoice, rng, *, today, **kw):
        if invoice.invoice_number == invoice_number:
            raise RuntimeError("mail gateway exploded")
        return generate_response(channel, contact, invoice, rng, today=today, **kw)
    return responder


def test_an_error_in_one_contact_is_logged_and_the_run_carries_on():
    store, _ = make_store()
    res = run(store, responder=failing_for("INV-2026-4488"))
    assert res.state is RunState.DONE and res.reconciled and res.contact_events > 5
    assert [e["row_keys"] for e in res.errors] == [["INV-2026-4488#1"]] and "exploded" in res.errors[0]["message"]
    bad = table(store)["INV-2026-4488#1"]
    assert bad[Col.OUTCOME] == "SYSTEM_ERROR" and bad[Col.CONSECUTIVE_SYSTEM_ERRORS] == "1"
    assert bad[Col.EMAIL_ATTEMPTS] == "0" and bad[Col.LAST_PROCESSED_DATE] == "2026-08-20"  # no attempt used, not retried today
    assert bad[Col.NEXT_FOLLOWUP_DATE] == "2026-08-21" and bad[Col.STATUS] == "IN_PROGRESS"
    others = audit(store).query("counted_as_attempt == 'Yes'")
    assert len(others) == res.attempts_email + res.attempts_phone > 5
    assert "exploded" in store.read_run_log().iloc[0]["errors"]


def test_a_failed_contact_is_retried_the_next_day_and_the_error_streak_resets():
    store, _ = make_store()
    run(store, responder=failing_for("INV-2026-4488"))
    run(store, d(1), "run-2", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    row = table(store)["INV-2026-4488#1"]
    assert row[Col.CONSECUTIVE_SYSTEM_ERRORS] == "0" and row[Col.EMAIL_ATTEMPTS] == "1" and row[Col.OUTCOME] == "NO_RESPONSE"


def test_three_failures_in_a_row_send_the_contact_to_a_person():
    store, _ = make_store()
    for n in range(3):
        run(store, d(n), f"run-{n}", responder=failing_for("INV-2026-4488"))
    row = table(store)["INV-2026-4488#1"]
    assert row[Col.STATUS] == "NEEDS_HUMAN" and row[Col.HUMAN_REASON] == "SYSTEM_ERRORS_REPEATED"
    assert row[Col.EMAIL_ATTEMPTS] == "0"  # never burned an attempt


def test_a_fatal_sheet_error_marks_the_run_failed_and_raises():
    class Failing(FakeWorksheet):
        fail_after = 10 ** 9
        batches = 0

        def batch_update(self, data, value_input_option=None):
            type(self).batches += 1
            if type(self).batches > type(self).fail_after:
                raise FakeAPIError(403)
            super().batch_update(data, value_input_option)

    rows = list(csv.reader(FIXTURE.open(encoding="utf-8")))
    ws = Failing("Invoices", rows, col_count=len(rows[0]))
    store = SheetStore(FakeClient(FakeSpreadsheet([ws])), "sheet", sleeper=lambda s: None)
    store.ensure_schema()
    Failing.fail_after = Failing.batches  # the next write to the invoices sheet fails
    with pytest.raises(FakeAPIError):
        run(store)
    log = store.read_run_log().iloc[0]
    assert log["state"] == "FAILED" and "fatal" in log["errors"]


# ---------------------------------------------------------------- the run lock (BR-24)


def test_a_run_already_in_progress_blocks_a_second_one():
    store, _ = make_store()
    store.ensure_schema()
    store.append_run_log(RunLogEntry(run_id="other", run_date=DAY1, started_at=NOW - dt.timedelta(minutes=5)))
    with pytest.raises(RunInProgressError, match="other"):
        run(store)
    assert len(store.read_run_log()) == 1  # nothing was logged for the refused run
    assert not store.has_running_run("2026-08-21")


def test_a_stale_running_row_is_marked_failed_and_does_not_block():
    store, _ = make_store()
    store.ensure_schema()
    store.append_run_log(RunLogEntry(run_id="crashed", run_date=DAY1, started_at=NOW - dt.timedelta(hours=3)))
    res = run(store)
    log = store.read_run_log().set_index("run_id")
    assert res.state is RunState.DONE and log.loc["crashed", "state"] == "FAILED" and log.loc["run-1", "state"] == "DONE"


# ---------------------------------------------------------------- over several days


def test_a_contact_that_never_answers_gets_five_emails_five_calls_then_a_person():
    store, _ = make_store(sheet_rows(1))
    for n in range(40):
        run(store, d(n), f"run-{n}", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    log = audit(store).query("counted_as_attempt == 'Yes'")
    assert list(log["channel"]) == ["email"] * 5 + ["phone"] * 5
    dates = [dt.date.fromisoformat(x) for x in log["run_date"]]
    assert all((b - a).days >= 2 for a, b in zip(dates, dates[1:])) and len(set(dates)) == 10
    row = table(store)["INV-1#1"]
    assert row[Col.STATUS] == "NEEDS_HUMAN" and row[Col.HUMAN_REASON] == "ATTEMPT_CAP"
    assert (row[Col.EMAIL_ATTEMPTS], row[Col.PHONE_ATTEMPTS], row[Col.CONTACT_ATTEMPTS]) == ("5", "5", "10")


def test_a_not_yet_due_invoice_is_contacted_once_it_is_due():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    assert "INV-2026-4512#1" not in set(audit(store)["row_key"])  # due 2026-08-29
    run(store, d(10), "run-2", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    log = audit(store).query("row_key == 'INV-2026-4512#1'")
    assert len(log) == 1 and log.iloc[0]["run_date"] == "2026-08-30"


def test_fixing_the_data_brings_a_failed_row_back():
    store, ws = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    assert table(store)["INV-2026-4144#1"][Col.STATUS] == "DQ_FAILED"
    set_cell(ws, store, "INV-2026-4144#1", Col.EMAIL, "z.nagy@puszta-trade.hu")
    run(store, d(1), "run-2", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    row = table(store)["INV-2026-4144#1"]
    assert row[Col.STATUS] == "IN_PROGRESS" and row[Col.DQ_STATUS] == "WARN" and row[Col.OUTCOME] == "NO_RESPONSE"
    assert row[Col.EMAIL_ATTEMPTS] == "1" and row[Col.HUMAN_FLAG] == ""


def test_a_long_promise_waits_for_approval_and_a_sheet_edit_approves_it():
    store, ws = make_store(sheet_rows(2, **{Col.CONTACT_NAME: "Same Person", Col.EMAIL: "same@x.de", Col.PHONE: "+49 30 5540 1100"}))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=14))
    rows = table(store)
    assert {r[Col.STATUS] for r in rows.values()} == {"NEEDS_HUMAN"} and {r[Col.APPROVAL_STATUS] for r in rows.values()} == {"PENDING"}
    assert len(audit(store).query("counted_as_attempt == 'Yes'")) == 1  # one promise, one combined contact

    run(store, d(1), "run-2", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))  # nobody has decided yet
    assert len(audit(store).query("counted_as_attempt == 'Yes'")) == 1

    set_cell(ws, store, "INV-2#1", Col.APPROVAL_STATUS, "approved")  # a person types in the sheet
    res = run(store, d(2), "run-3", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    rows = table(store)
    assert res.sheet_edits_applied == 2
    assert {r[Col.STATUS] for r in rows.values()} == {"IN_PROGRESS"} and {r[Col.EXTENSION_APPROVALS_COUNT] for r in rows.values()} == {"1"}
    assert {r[Col.NEXT_FOLLOWUP_DATE] for r in rows.values()} == {"2026-09-03"}
    edits = audit(store).query("actor == 'sheet edit'")
    assert len(edits) == 2 and edits["status_after"].eq("IN_PROGRESS").all()
    run(store, d(14), "run-4", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))  # promised date reached: back to work
    assert len(audit(store).query("counted_as_attempt == 'Yes'")) == 2


def test_an_app_approval_is_picked_up_by_the_next_run():
    store, _ = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=10))
    row = RowState.from_row(table(store)["INV-1#1"])
    decision = approve_contact_extension([row], d(1), now=dt.datetime.combine(d(1), dt.time(8)))
    store.update_rows(decision.updates)
    run(store, d(1), "run-2", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    assert table(store)["INV-1#1"][Col.STATUS] == "IN_PROGRESS" and len(audit(store).query("counted_as_attempt == 'Yes'")) == 1
    run(store, d(10), "run-3", responder=forced(Outcome.PAID, Outcome.PAID))
    assert table(store)["INV-1#1"][Col.STATUS] == "HANDLED" and table(store)["INV-1#1"][Col.AMOUNT_OUTSTANDING] in ("0", "0.0")


# ---------------------------------------------------------------- invariants over 30 random days


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariants_hold_over_thirty_random_days(seed):
    store, _ = make_store()
    rng = random.Random(seed)
    for n in range(30):
        res = run_daily(store, d(n), f"run-{n}", rng, now=dt.datetime.combine(d(n), dt.time(9)))
        assert res.state is RunState.DONE and res.reconciled and not [e for e in res.errors if e["type"] == "error"]
    rows = table(store)
    log = audit(store)
    attempts = log.query("counted_as_attempt == 'Yes'")

    # never more than 5 per channel, and the sheet counters agree with the audit trail per contact
    for key, group in attempts.groupby("contact_key"):
        assert (group["channel"] == "email").sum() <= 5 and (group["channel"] == "phone").sum() <= 5, key
        assert group["run_date"].is_unique, f"{key} was contacted twice in one day"  # BR-06
    for r in rows.values():
        assert int(r[Col.EMAIL_ATTEMPTS]) <= 5 and int(r[Col.PHONE_ATTEMPTS]) <= 5 and int(r[Col.EXTENSION_APPROVALS_COUNT]) <= 3
        assert int(r[Col.CONTACT_ATTEMPTS]) == int(r[Col.EMAIL_ATTEMPTS]) + int(r[Col.PHONE_ATTEMPTS]) + int(r[Col.LEGACY_ATTEMPTS])
    # for contacts with no history before the first run, the highest counter on any of their rows equals the
    # attempts in the audit trail (a paid invoice stops receiving the contact's counters, so compare the maximum)
    original = pd.read_csv(FIXTURE, dtype=str, keep_default_na=False)
    original[Col.ROW_KEY] = original[Col.INVOICE_NUMBER] + "#" + (original.groupby(Col.INVOICE_NUMBER).cumcount() + 1).astype(str)
    had_history = {rows[k][Col.CONTACT_KEY] for k in original[Col.ROW_KEY][original[Col.CONTACT_ATTEMPTS] != "0"]}
    counts = attempts.groupby("contact_key")["channel"].value_counts().unstack(fill_value=0)
    checked = 0
    for contact, group in pd.DataFrame(rows.values()).groupby(Col.CONTACT_KEY):
        if contact in had_history or contact not in counts.index:
            continue
        for column, channel in ((Col.EMAIL_ATTEMPTS, "email"), (Col.PHONE_ATTEMPTS, "phone")):
            assert group[column].astype(int).max() == counts.loc[contact].get(channel, 0), (contact, channel)
        checked += 1
    assert checked >= 10

    # rows that failed data quality or are marked not ready are never contacted
    blocked = {k for k, r in rows.items() if r[Col.DQ_STATUS] == "FAIL" or r[Col.READY_FOR_CONTACT] == "No"}
    assert not blocked & set(attempts["row_key"])
    # Notes are exactly as imported
    assert {k: r[Col.NOTES] for k, r in rows.items()} == dict(zip(original[Col.ROW_KEY], original[Col.NOTES]))
