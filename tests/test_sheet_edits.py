"""Decisions made by typing in the Google Sheet (reconcile_sheet_edits), compared with the app buttons."""
from __future__ import annotations

import pytest
from test_approvals import CFG, NOW, TODAY, Book, d, row, three_invoices

from core.rules import RowState, approve_contact_extension, reconcile_sheet_edits, reject_contact_extension
from core.schema import ApprovalStatus, Col, HumanReason, INVOICES_COLUMNS, PROTECTED_COLUMNS, Status
from core.taxonomy import Outcome

DECISION_COLUMNS = [Col.STATUS, Col.APPROVAL_STATUS, Col.PENDING_APPROVAL_DATE, Col.NEXT_FOLLOWUP_DATE,
                    Col.LAST_PROMISED_DATE, Col.EXTENSION_APPROVALS_COUNT, Col.HUMAN_FLAG, Col.HUMAN_REASON]


def pending_book() -> Book:
    book = three_invoices()
    book.contact_event(TODAY, Outcome.PROMISE_TO_PAY, promised_days=14)
    return book


def sync(book: Book):
    result = reconcile_sheet_edits(book.states(), TODAY, CFG, NOW)
    book.write(result.updates)
    return result


def snapshot(book: Book) -> dict[str, dict[str, str]]:
    return {k: {c: r.get(c, "") for c in DECISION_COLUMNS} for k, r in book.rows.items()}


def test_nothing_to_reconcile_when_nobody_edited_anything():
    book = pending_book()
    assert not sync(book).changed
    assert not sync(three_invoices()).changed


def test_typing_approved_on_one_row_approves_the_whole_promise_once():
    book = pending_book()
    book.rows["B"][Col.APPROVAL_STATUS] = "APPROVED"  # a person edits one of the three rows
    result = sync(book)
    after = book.states()
    assert all(s.status is Status.IN_PROGRESS and s.approval_status is ApprovalStatus.APPROVED for s in after)
    assert all(s.next_followup_date == d(14) and s.approvals == 1 for s in after)  # one approval, not three
    assert [a.row_key for a in result.audits] == ["A", "B", "C"]
    assert all(a.actor == "sheet edit" and "edited in the sheet" in a.note and a.status_after is Status.IN_PROGRESS for a in result.audits)
    assert not result.issues


def test_a_second_sync_changes_nothing():
    book = pending_book()
    book.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    sync(book)
    snap = snapshot(book)
    again = sync(book)
    assert not again.changed and not again.audits and snapshot(book) == snap


def test_a_sheet_approval_ends_in_the_same_state_as_the_app_button():
    by_sheet, by_app = pending_book(), pending_book()
    by_sheet.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    sync(by_sheet)
    by_app.write(approve_contact_extension(by_app.states(), TODAY, CFG, NOW).updates)
    assert snapshot(by_sheet) == snapshot(by_app)


def test_a_sheet_rejection_ends_in_the_same_state_as_the_app_button():
    by_sheet, by_app = pending_book(), pending_book()
    by_sheet.rows["C"][Col.APPROVAL_STATUS] = "REJECTED"
    result = sync(by_sheet)
    by_app.write(reject_contact_extension(by_app.states(), TODAY, NOW).updates)
    assert snapshot(by_sheet) == snapshot(by_app)
    assert {a.row_key for a in result.audits} == {"A", "B"}  # the two rows the person did not touch
    assert all(s.approvals == 0 and s.status is Status.NEEDS_HUMAN for s in by_sheet.states())
    assert not sync(by_sheet).changed


def test_editing_the_date_before_approving_approves_that_date():
    book = pending_book()
    for key in ("A", "B", "C"):
        book.rows[key][Col.PENDING_APPROVAL_DATE] = d(20).isoformat()
    book.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    sync(book)
    assert {s.next_followup_date for s in book.states()} == {d(20)}


def test_approved_and_rejected_in_the_same_promise_is_a_conflict_and_nothing_changes():
    book = pending_book()
    book.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    book.rows["B"][Col.APPROVAL_STATUS] = "REJECTED"
    before = snapshot(book)
    result = sync(book)
    assert not result.changed and snapshot(book) == before
    assert [i.code for i in result.issues] == ["CONFLICT"] and result.issues[0].row_keys == ["A", "B", "C"]


def test_a_sheet_approval_past_the_cap_is_refused_and_not_counted():
    states = [row(row_key="A", status=Status.NEEDS_HUMAN, human_reason=HumanReason.EXTENSION_APPROVAL_PENDING,
                  approval_status=ApprovalStatus.APPROVED, pending_approval_date=d(9), approvals=3)]
    result = reconcile_sheet_edits(states, TODAY, CFG, NOW)
    u = result.updates["A"]
    assert u[Col.APPROVAL_STATUS] is ApprovalStatus.REJECTED and "already used 3" in u[Col.HUMAN_RESOLUTION_NOTE]
    assert Col.EXTENSION_APPROVALS_COUNT not in u and [i.code for i in result.issues] == ["CAP_REACHED"]


def test_approved_without_a_date_goes_back_to_pending():
    states = [row(row_key="A", status=Status.NEEDS_HUMAN, human_reason=HumanReason.EXTENSION_APPROVAL_PENDING,
                  approval_status=ApprovalStatus.APPROVED, pending_approval_date=None)]
    result = reconcile_sheet_edits(states, TODAY, CFG, NOW)
    assert result.updates["A"] == {Col.APPROVAL_STATUS: ApprovalStatus.PENDING}
    assert [i.code for i in result.issues] == ["MISSING_DATE"]


def test_approved_on_a_row_that_is_not_waiting_for_approval_is_ignored():
    states = [row(row_key="A", status=Status.NEEDS_HUMAN, human_reason=HumanReason.DISPUTE, approval_status=ApprovalStatus.APPROVED)]
    result = reconcile_sheet_edits(states, TODAY, CFG, NOW)
    assert result.updates["A"] == {Col.APPROVAL_STATUS: ApprovalStatus.NONE}
    assert [i.code for i in result.issues] == ["IGNORED_APPROVAL"]


def test_pending_request_is_cleared_when_a_person_moves_the_row_on_by_hand():
    book = pending_book()
    book.rows["A"][Col.STATUS] = "IN_PROGRESS"  # a person closes the question by editing the status
    result = sync(book)
    a = next(s for s in book.states() if s.row_key == "A")
    assert a.approval_status is ApprovalStatus.NONE and a.pending_approval_date is None and a.human_reason is None
    assert [i.code for i in result.issues] == ["STALE_PENDING"]
    # the other two rows are still waiting, undisturbed
    assert [s.approval_status for s in book.states() if s.row_key != "A"] == [ApprovalStatus.PENDING] * 2
    assert not sync(book).changed


def test_human_reason_left_on_a_row_that_is_no_longer_with_a_person_is_cleared():
    for status in (Status.IN_PROGRESS, Status.HANDLED, Status.NEW):
        result = reconcile_sheet_edits([row(row_key="A", status=status, human_reason=HumanReason.DISPUTE)], TODAY, CFG, NOW)
        assert result.updates["A"] == {Col.HUMAN_FLAG: "", Col.HUMAN_REASON: ""}
    assert not reconcile_sheet_edits([row(status=Status.NEEDS_HUMAN, human_reason=HumanReason.DISPUTE)], TODAY, CFG).changed
    assert not reconcile_sheet_edits([row(status=Status.DQ_FAILED, human_reason=HumanReason.DQ)], TODAY, CFG).changed


def test_other_contacts_are_not_affected():
    book = pending_book()
    other = RowState(row_key="Z", contact_key="other@x.de", status=Status.NEEDS_HUMAN,
                     human_reason=HumanReason.EXTENSION_APPROVAL_PENDING, approval_status=ApprovalStatus.PENDING,
                     pending_approval_date=d(14))
    book.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    result = reconcile_sheet_edits([*book.states(), other], TODAY, CFG, NOW)
    assert "Z" not in result.updates and set(result.updates) == {"A", "B", "C"}


def test_two_promises_for_different_dates_are_decided_separately():
    early = [row(row_key=k, status=Status.NEEDS_HUMAN, human_reason=HumanReason.EXTENSION_APPROVAL_PENDING,
                 approval_status=ApprovalStatus.PENDING, pending_approval_date=d(9)) for k in ("A", "B")]
    late = row(row_key="C", status=Status.NEEDS_HUMAN, human_reason=HumanReason.EXTENSION_APPROVAL_PENDING,
               approval_status=ApprovalStatus.APPROVED, pending_approval_date=d(20))
    result = reconcile_sheet_edits([*early, late], TODAY, CFG, NOW)
    assert result.updates["C"][Col.NEXT_FOLLOWUP_DATE] == d(20)
    assert result.updates["A"] == {Col.EXTENSION_APPROVALS_COUNT: 1}  # still pending, only the new count


def test_reconcile_only_writes_real_columns_and_never_notes():
    book = pending_book()
    book.rows["A"][Col.APPROVAL_STATUS] = "APPROVED"
    result = sync(book)
    for u in result.updates.values():
        assert set(u) <= set(INVOICES_COLUMNS) and not set(u) & PROTECTED_COLUMNS


@pytest.mark.parametrize("typed", ["approved", " Approved ", "APPROVED"])
def test_typing_is_forgiving_about_case_and_spaces(typed):
    book = pending_book()
    book.rows["A"][Col.APPROVAL_STATUS] = typed
    sync(book)
    assert all(s.approval_status is ApprovalStatus.APPROVED for s in book.states())
