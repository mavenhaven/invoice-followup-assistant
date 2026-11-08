"""Contact-level human approval and the fan-out of updates across a contact's rows (core/rules.py)."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from core.rules import (
    ContactState, RowState, apply_outcome, approve_contact_extension, build_event_updates, fan_out_updates,
    is_open, plan_contact_event, reject_contact_extension,
)
from core.schema import (
    ApprovalStatus, Col, Config, DqStatus, HumanReason, INVOICES_COLUMNS, PROTECTED_COLUMNS, Status, to_cell,
)
from core.taxonomy import Outcome

TODAY = dt.date(2026, 8, 20)
NOW = dt.datetime(2026, 8, 20, 9, 30)
CFG = Config()


def d(days: int) -> dt.date:
    return TODAY + dt.timedelta(days=days)


def row(**kw) -> RowState:
    base = dict(row_key="A", contact_key="t@x.at", due_date=dt.date(2026, 7, 1), amount_outstanding=100.0, ready="yes")
    base.update(kw)
    return RowState(**base)


class Book:
    """Several rows of one contact stored as sheet strings, updated through to_cell like in production."""

    def __init__(self, due_dates: dict[str, str]) -> None:
        self.rows = {k: {Col.ROW_KEY: k, Col.DUE_DATE: v, Col.AMOUNT_OUTSTANDING: "100", Col.READY_FOR_CONTACT: "Yes",
                         Col.CONTACT_KEY: "t@x.at"} for k, v in due_dates.items()}
        self.dq = {"dq_status": "PASS", "can_contact_email": True, "can_contact_phone": True, "contact_key": "t@x.at"}

    def write(self, updates: dict[str, dict]) -> None:
        for key, u in updates.items():
            for col, v in u.items():
                assert col in INVOICES_COLUMNS and col not in PROTECTED_COLUMNS, col
                self.rows[key][col] = str(to_cell(v))

    def states(self) -> list[RowState]:
        return [RowState.from_row(r, self.dq) for r in self.rows.values()]

    def contact_event(self, today: dt.date, outcome: Outcome, **kw):
        states = self.states()
        ev = plan_contact_event(states, today, CFG, NOW)
        assert ev is not None
        primary = next(s for s in states if s.row_key == ev.primary_row_key)
        res = apply_outcome(primary, ContactState.from_rows(states), ev.channel, outcome, today, CFG, now=NOW, **kw)
        self.write(build_event_updates(ev, res, states, today, NOW))
        return ev, res


def three_invoices() -> Book:
    return Book({"A": "2026-06-23", "B": "2026-07-10", "C": "2026-07-29"})


def test_one_promise_over_three_invoices_makes_three_rows_pending_but_needs_one_decision():
    book = three_invoices()
    book.contact_event(TODAY, Outcome.PROMISE_TO_PAY, promised_days=14)
    states = book.states()
    assert [s.approval_status for s in states] == [ApprovalStatus.PENDING] * 3
    assert {s.pending_approval_date for s in states} == {d(14)}
    assert all(s.status is Status.NEEDS_HUMAN and s.human_reason is HumanReason.EXTENSION_APPROVAL_PENDING for s in states)

    decision = approve_contact_extension(states, TODAY, CFG, NOW)
    assert sorted(decision.row_keys) == ["A", "B", "C"] and decision.approvals_used == 1
    book.write(decision.updates)
    after = book.states()
    assert all(s.status is Status.IN_PROGRESS and s.approval_status is ApprovalStatus.APPROVED for s in after)
    assert all(s.next_followup_date == d(14) and s.approvals == 1 for s in after)  # one approval, on every row
    assert "1 of 3" in decision.audit_note


def test_approving_the_same_promise_twice_is_refused():
    book = three_invoices()
    book.contact_event(TODAY, Outcome.PROMISE_TO_PAY, promised_days=14)
    book.write(approve_contact_extension(book.states(), TODAY, CFG, NOW).updates)
    with pytest.raises(ValueError, match="no pending extension"):
        approve_contact_extension(book.states(), TODAY, CFG, NOW)  # a second person clicking after the first


def test_contact_rejection_marks_every_waiting_row_and_uses_no_approval():
    book = three_invoices()
    book.contact_event(TODAY, Outcome.PROMISE_TO_PAY, promised_days=14)
    decision = reject_contact_extension(book.states(), TODAY, NOW)
    book.write(decision.updates)
    after = book.states()
    assert all(s.approval_status is ApprovalStatus.REJECTED and s.status is Status.NEEDS_HUMAN for s in after)
    assert all(s.approvals == 0 for s in after) and decision.approvals_used == 0
    with pytest.raises(ValueError, match="no pending extension"):
        reject_contact_extension(after, TODAY, NOW)


def test_the_cap_counts_promises_not_rows():
    book = three_invoices()
    day = TODAY
    for _ in range(3):
        book.contact_event(day, Outcome.PROMISE_TO_PAY, promised_days=10)
        book.write(approve_contact_extension(book.states(), day, CFG, NOW).updates)
        day = day + dt.timedelta(days=10)
    assert {s.approvals for s in book.states()} == {3}
    _, res = book.contact_event(day, Outcome.PROMISE_TO_PAY, promised_days=10)
    assert res.human_reason is HumanReason.EXTENSION_LIMIT_REACHED
    assert all(s.approval_status is ApprovalStatus.NONE and s.human_reason is HumanReason.EXTENSION_LIMIT_REACHED
               for s in book.states())


def test_approval_is_refused_when_the_contact_is_already_at_the_cap():
    states = [row(approval_status=ApprovalStatus.PENDING, pending_approval_date=d(9), approvals=3)]
    with pytest.raises(ValueError, match="already used 3"):
        approve_contact_extension(states, TODAY, CFG)


def test_two_pending_dates_need_the_date_to_be_named():
    states = [row(row_key="A", approval_status=ApprovalStatus.PENDING, pending_approval_date=d(9), status=Status.NEEDS_HUMAN),
              row(row_key="B", approval_status=ApprovalStatus.PENDING, pending_approval_date=d(20), status=Status.NEEDS_HUMAN)]
    with pytest.raises(ValueError, match="several dates"):
        approve_contact_extension(states, TODAY, CFG)
    decision = approve_contact_extension(states, TODAY, CFG, pending_date=d(20))
    assert decision.row_keys == ["B"] and decision.updates["B"][Col.NEXT_FOLLOWUP_DATE] == d(20)
    assert decision.updates["A"] == {Col.EXTENSION_APPROVALS_COUNT: 1}  # A only gets the new count
    with pytest.raises(ValueError, match="no pending request"):
        approve_contact_extension(states, TODAY, CFG, pending_date=d(30))


def test_a_decision_with_nothing_pending_is_refused():
    with pytest.raises(ValueError, match="no pending extension"):
        approve_contact_extension([row()], TODAY, CFG)
    with pytest.raises(ValueError, match="no pending extension"):
        reject_contact_extension([row()], TODAY)


def test_fan_out_skips_paid_and_failed_rows_unless_they_have_their_own_update():
    rows = [row(row_key="open"), row(row_key="paid", status=Status.HANDLED), row(row_key="bad", status=Status.DQ_FAILED),
            row(row_key="bad2", dq_status=DqStatus.FAIL)]
    assert [is_open(r) for r in rows] == [True, False, False, False]
    assert fan_out_updates(rows, {Col.EMAIL_ATTEMPTS: 2}) == {"open": {Col.EMAIL_ATTEMPTS: 2}}
    out = fan_out_updates(rows, {Col.EMAIL_ATTEMPTS: 2, Col.STATUS: "x"}, {"paid": {Col.STATUS: "y"}, "open": {Col.STATUS: "z"}})
    assert out["paid"] == {Col.EMAIL_ATTEMPTS: 2, Col.STATUS: "y"} and out["open"][Col.STATUS] == "z" and "bad" not in out


def test_an_invoice_level_outcome_changes_only_the_primary_row_and_covers_the_rest():
    book = three_invoices()
    book.contact_event(TODAY, Outcome.DISPUTE)
    rows = {s.row_key: s for s in book.states()}
    assert rows["A"].status is Status.NEEDS_HUMAN and rows["A"].human_reason is HumanReason.DISPUTE
    for key in ("B", "C"):
        assert rows[key].status is Status.NEW and rows[key].next_followup_date == d(1)  # covered, due again tomorrow
        assert rows[key].email_attempts == 1 and rows[key].last_contact_date == TODAY  # but the attempt is counted
        assert rows[key].last_processed_date == TODAY


def test_payment_of_one_invoice_does_not_close_the_others():
    book = three_invoices()
    book.contact_event(TODAY, Outcome.PAID, amount_paid=100.0)
    rows = {s.row_key: s for s in book.states()}
    assert rows["A"].status is Status.HANDLED and rows["A"].amount_outstanding == 0
    assert rows["B"].amount_outstanding == 100 and rows["C"].amount_outstanding == 100
    ev = plan_contact_event(book.states(), d(1), CFG)  # next day the remaining two go out together
    assert ev is not None and ev.row_keys == ["B", "C"]


def test_build_event_updates_includes_release_updates_and_rejects_unknown_rows():
    states = [row(row_key="A", status=Status.DQ_FAILED), row(row_key="B")]
    ev = plan_contact_event(states, TODAY, CFG, NOW)
    assert ev is not None
    res = apply_outcome(states[1], ContactState.from_rows(states), ev.channel, Outcome.NO_RESPONSE, TODAY, CFG, now=NOW)
    out = build_event_updates(ev, res, states, TODAY, NOW)
    assert out["A"][Col.STATUS] is Status.IN_PROGRESS and out["B"][Col.EMAIL_ATTEMPTS] == 1
    with pytest.raises(KeyError, match="ZZ"):
        build_event_updates(dataclasses.replace(ev, row_keys=["A", "ZZ"]), res, states, TODAY, NOW)
