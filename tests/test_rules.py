"""Tests for core/rules.py: table-driven scenarios per business rule, simulations, and the real file."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from core.quality import check_quality
from core.rules import (
    Action, ActionKind, ContactState, Reason, RowState, apply_outcome, approve_extension, choose_channel,
    covered_row_updates, decide_action, initialize_row, plan_contact_event, reject_extension, resolve_row,
)
from core.schema import (
    ApprovalStatus, Channel, Col, Config, DqStatus, HumanReason, INVOICES_COLUMNS, PROTECTED_COLUMNS, Status, to_cell,
)
from core.taxonomy import Outcome, outcomes_for_channel

TODAY = dt.date(2026, 8, 20)
NOW = dt.datetime(2026, 8, 20, 9, 30)
CFG = Config()
E, P = Channel.EMAIL, Channel.PHONE


def d(days: int) -> dt.date:
    return TODAY + dt.timedelta(days=days)


def row(**kw) -> RowState:
    base = dict(row_key="INV-1#1", contact_key="a@x.de", due_date=dt.date(2026, 7, 1), amount_outstanding=1000.0, ready="yes")
    base.update(kw)
    return RowState(**base)


def contact_of(r: RowState, **kw) -> ContactState:
    c = ContactState.from_rows([r])
    import dataclasses as _dc
    return _dc.replace(c, **kw) if kw else c


def decide(r: RowState, **contact_kw) -> Action:
    return decide_action(r, contact_of(r, **contact_kw), TODAY, CFG, NOW)


# ---------------------------------------------------------------- decide_action: one scenario per line

C, S, M = ActionKind.CONTACT, ActionKind.SKIP, ActionKind.MARK

DECISIONS = [
    # id, row kwargs, contact kwargs, kind, reason or channel, human reason
    ("fresh row starts with email (BR-02)", {}, {}, C, E, None),
    ("already processed today (BR-07)", {"last_processed_date": TODAY}, {}, S, Reason.ALREADY_PROCESSED, None),
    ("processed yesterday is fine", {"last_processed_date": d(-1)}, {}, C, E, None),
    ("blocking DQ issue (BR-18)", {"dq_status": DqStatus.FAIL}, {}, M, Reason.DQ_FAILED, HumanReason.DQ),
    ("credit note keeps its own reason", {"dq_status": DqStatus.FAIL, "dq_human_reason": "NOT_AN_INVOICE"}, {}, M, Reason.DQ_FAILED, HumanReason.NOT_AN_INVOICE),
    ("already DQ_FAILED just waits", {"dq_status": DqStatus.FAIL, "status": Status.DQ_FAILED}, {}, S, Reason.DQ_FAILED, None),
    ("DQ fixed: back in play", {"status": Status.DQ_FAILED}, {}, C, E, None),
    ("paid by DQ exclusion (BR-15)", {"dq_exclusion_reason": "SETTLED"}, {}, M, Reason.SETTLED, None),
    ("outcome PAID", {"outcome": Outcome.PAID}, {}, M, Reason.SETTLED, None),
    ("zero outstanding", {"amount_outstanding": 0.0}, {}, M, Reason.SETTLED, None),
    ("closed by a person", {"status": Status.HANDLED}, {}, S, Reason.ALREADY_HANDLED, None),
    ("already HANDLED and paid", {"status": Status.HANDLED, "outcome": Outcome.PAID}, {}, S, Reason.ALREADY_HANDLED, None),
    ("Ready = No (BR-17)", {"ready": "no"}, {}, M, Reason.READY_NO, HumanReason.READY_NO),
    ("Ready = No already escalated", {"ready": "no", "status": Status.NEEDS_HUMAN, "human_reason": HumanReason.READY_NO}, {}, S, Reason.WAITING_FOR_HUMAN, None),
    ("Ready flag changed back to Yes", {"status": Status.NEEDS_HUMAN, "human_reason": HumanReason.READY_NO}, {}, C, E, None),
    ("waiting for a human", {"status": Status.NEEDS_HUMAN, "human_reason": HumanReason.DISPUTE}, {}, S, Reason.WAITING_FOR_HUMAN, None),
    ("pending approval waits", {"status": Status.NEEDS_HUMAN, "human_reason": HumanReason.EXTENSION_APPROVAL_PENDING}, {}, S, Reason.WAITING_FOR_HUMAN, None),
    ("not yet due (BR-01)", {"due_date": d(9)}, {}, S, Reason.NOT_DUE, None),
    ("due today counts as due", {"due_date": TODAY}, {}, C, E, None),
    ("follow-up date in the future (BR-09)", {"next_followup_date": d(3)}, {}, S, Reason.WAITING_FOLLOWUP, None),
    ("follow-up date reached", {"next_followup_date": TODAY}, {}, C, E, None),
    ("contacted earlier today (BR-06)", {}, {"last_contact_date": TODAY}, S, Reason.CONTACTED_TODAY, None),
    ("contacted yesterday is fine", {}, {"last_contact_date": d(-1)}, C, E, None),
    ("below minimum, set to skip", {"dq_exclusion_reason": "BELOW_MINIMUM"}, {}, S, Reason.BELOW_MINIMUM, None),
    ("4 email attempts: still email (BR-03)", {}, {"email_attempts": 4}, C, E, None),
    ("5 email attempts: switch to phone (BR-04)", {}, {"email_attempts": 5}, C, P, None),
    ("invalid email: phone only", {"email_valid": False}, {}, C, P, None),
    ("email disabled by a bounce", {}, {"email_disabled": True}, C, P, None),
    ("invalid phone: email", {"phone_valid": False}, {}, C, E, None),
    ("phone at the cap, email still free", {}, {"phone_attempts": 5}, C, E, None),
    ("both channels at the cap (BR-05)", {}, {"email_attempts": 5, "phone_attempts": 5}, M, "ATTEMPT_CAP", HumanReason.ATTEMPT_CAP),
    ("email capped, phone invalid", {"phone_valid": False}, {"email_attempts": 5}, M, "ATTEMPT_CAP", HumanReason.ATTEMPT_CAP),
    ("both disabled", {}, {"email_disabled": True, "phone_disabled": True}, M, "NO_CHANNEL_LEFT", HumanReason.NO_CHANNEL_LEFT),
    ("no valid channel at all", {"email_valid": False, "phone_valid": False}, {}, M, "NO_CHANNEL_LEFT", HumanReason.NO_CHANNEL_LEFT),
    ("5 legacy attempts of unknown channel (BR-19)", {}, {"legacy_attempts": 5}, M, Reason.ATTEMPT_CAP_LEGACY, HumanReason.ATTEMPT_CAP_LEGACY),
    ("4 legacy attempts are fine", {}, {"legacy_attempts": 4}, C, E, None),
    ("callback request prefers phone", {"outcome": Outcome.CALLBACK_REQUESTED}, {}, C, P, None),
    ("callback request but no phone", {"outcome": Outcome.CALLBACK_REQUESTED, "phone_valid": False}, {}, C, E, None),
    ("callback request but phone at the cap", {"outcome": Outcome.CALLBACK_REQUESTED}, {"phone_attempts": 5}, C, E, None),
]


@pytest.mark.parametrize("name, row_kw, contact_kw, kind, expect, human", DECISIONS, ids=[x[0] for x in DECISIONS])
def test_decide_action(name, row_kw, contact_kw, kind, expect, human):
    a = decide(row(**row_kw), **contact_kw)
    assert a.kind is kind
    if kind is C:
        assert a.channel is expect
    else:
        assert a.reason == expect
    assert a.human_reason == human


def test_decide_updates_stamp_the_row_and_set_the_right_status():
    a = decide(row(dq_status=DqStatus.FAIL))
    assert a.updates[Col.STATUS] is Status.DQ_FAILED and a.updates[Col.HUMAN_FLAG] == "Yes"
    assert a.updates[Col.OUTCOME] is Outcome.DQ_FAILED and a.updates[Col.LAST_PROCESSED_DATE] == TODAY

    a = decide(row(), email_attempts=5, phone_attempts=5)
    assert a.updates[Col.STATUS] is Status.NEEDS_HUMAN and a.updates[Col.OUTCOME] is Outcome.MAX_ATTEMPTS_REACHED
    a = decide(row(), email_disabled=True, phone_disabled=True)
    assert a.updates[Col.OUTCOME] is Outcome.NEEDS_HUMAN_FOLLOW_UP

    a = decide(row(ready="no", outcome=Outcome.NO_ANSWER))
    assert a.updates[Col.STATUS] is Status.NEEDS_HUMAN and Col.OUTCOME not in a.updates  # existing outcome is kept

    a = decide(row(outcome=Outcome.PAID))
    assert a.updates[Col.STATUS] is Status.HANDLED and a.updates[Col.NEXT_FOLLOWUP_DATE] == ""

    a = decide(row(due_date=d(5)))
    assert a.updates == {Col.LAST_PROCESSED_DATE: TODAY}
    assert decide(row(last_processed_date=TODAY)).updates == {}  # nothing to write when already processed


def test_release_updates_come_with_a_contact_action():
    a = decide(row(status=Status.DQ_FAILED, human_reason=HumanReason.DQ))
    assert a.kind is C and a.updates[Col.STATUS] is Status.IN_PROGRESS and a.updates[Col.HUMAN_FLAG] == ""


def test_choose_channel_callback_does_not_override_email_when_phone_is_unavailable():
    ch, why = choose_channel(row(outcome=Outcome.CALLBACK_REQUESTED, phone_valid=False), ContactState(), CFG)
    assert (ch, why) == (E, None)


def test_contact_state_takes_the_maximum_across_rows():
    a = row(row_key="A", email_attempts=2, approvals=1, last_contact_date=d(-3), last_channel=E)
    b = row(row_key="B", email_attempts=4, phone_attempts=1, phone_disabled=True, system_errors=2,
            last_contact_date=d(-1), last_channel=P)
    c = ContactState.from_rows([a, b])
    assert (c.email_attempts, c.phone_attempts, c.approvals, c.system_errors) == (4, 1, 1, 2)
    assert c.phone_disabled and not c.email_disabled
    assert c.last_contact_date == d(-1) and c.last_channel is P
    assert ContactState.from_rows([]) == ContactState()


def test_row_state_from_a_sheet_row_and_from_dq():
    sheet = {Col.ROW_KEY: "INV-9#1", Col.STATUS: "IN_PROGRESS", Col.OUTCOME: "NO_ANSWER", Col.AMOUNT_OUTSTANDING: "1,200.50",
             Col.DUE_DATE: "2026-07-01", Col.NEXT_FOLLOWUP_DATE: "2026-08-22", Col.READY_FOR_CONTACT: "Yes",
             Col.EMAIL_ATTEMPTS: "2", Col.PHONE_ATTEMPTS: "", Col.EMAIL_DISABLED: "Yes", Col.LAST_CHANNEL: "email",
             Col.APPROVAL_STATUS: "PENDING", Col.HUMAN_REASON: "EXTENSION_APPROVAL_PENDING", Col.CAN_CONTACT_EMAIL: "Yes",
             Col.CAN_CONTACT_PHONE: "No", Col.DQ_STATUS: "WARN", Col.CONTACT_KEY: "k"}
    r = RowState.from_row(sheet)
    assert (r.status, r.outcome, r.amount_outstanding) == (Status.IN_PROGRESS, Outcome.NO_ANSWER, 1200.5)
    assert r.next_followup_date == dt.date(2026, 8, 22) and r.email_attempts == 2 and r.phone_attempts == 0
    assert r.email_disabled and r.last_channel is E and r.approval_status is ApprovalStatus.PENDING
    assert r.email_valid and not r.phone_valid and r.dq_status is DqStatus.WARN and r.contact_key == "k"
    fresh = RowState.from_row(sheet, dq={"dq_status": "FAIL", "can_contact_email": False, "can_contact_phone": True,
                                         "contact_key": "fresh", "exclusion_reason": "SETTLED", "human_reason": "DQ"})
    assert fresh.dq_status is DqStatus.FAIL and fresh.phone_valid and not fresh.email_valid and fresh.contact_key == "fresh"
    assert RowState.from_row({}).status is Status.NEW  # a blank status is NEW


# ---------------------------------------------------------------- plan_contact_event (BR-06)


def test_combined_contact_covers_every_due_invoice_with_the_oldest_as_primary():
    rows = [row(row_key="C", due_date=dt.date(2026, 7, 29)), row(row_key="A", due_date=dt.date(2026, 6, 23)),
            row(row_key="B", due_date=dt.date(2026, 7, 10))]
    ev = plan_contact_event(rows, TODAY, CFG, NOW)
    assert ev is not None and ev.channel is E
    assert ev.primary_row_key == "A" and ev.row_keys == ["A", "B", "C"]


def test_event_leaves_out_rows_that_are_not_due_or_waiting():
    rows = [row(row_key="A"), row(row_key="B", due_date=d(5)), row(row_key="C", next_followup_date=d(2)),
            row(row_key="D", status=Status.NEEDS_HUMAN, human_reason=HumanReason.DISPUTE)]
    ev = plan_contact_event(rows, TODAY, CFG, NOW)
    assert ev is not None and ev.row_keys == ["A"]


def test_no_event_when_the_contact_was_already_contacted_today_or_nothing_is_due():
    assert plan_contact_event([row(row_key="A", last_contact_date=TODAY), row(row_key="B")], TODAY, CFG) is None
    assert plan_contact_event([row(row_key="A", due_date=d(1))], TODAY, CFG) is None
    assert plan_contact_event([], TODAY, CFG) is None


def test_event_uses_contact_level_counters_from_any_row():
    rows = [row(row_key="A"), row(row_key="B", email_attempts=5)]
    ev = plan_contact_event(rows, TODAY, CFG)
    assert ev is not None and ev.channel is P  # one row's counter moves the whole contact to phone


def test_event_carries_release_updates():
    ev = plan_contact_event([row(row_key="A", status=Status.DQ_FAILED)], TODAY, CFG, NOW)
    assert ev is not None and ev.release_updates["A"][Col.STATUS] is Status.IN_PROGRESS


# ---------------------------------------------------------------- apply_outcome


def apply(outcome, channel=E, *, row_kw=None, contact_kw=None, **kw):
    r = row(**(row_kw or {}))
    c = contact_of(r, **(contact_kw or {}))
    return apply_outcome(r, c, channel, outcome, TODAY, CFG, now=NOW, **kw)


def test_unanswered_email_counts_one_attempt_and_retries_after_the_default_gap():
    res = apply(Outcome.NO_RESPONSE)
    assert res.counted_as_attempt and res.attempt_no_for_channel == 1
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == d(2)
    cu = res.contact_updates
    assert (cu[Col.EMAIL_ATTEMPTS], cu[Col.PHONE_ATTEMPTS], cu[Col.CONTACT_ATTEMPTS]) == (1, 0, 1)
    assert cu[Col.LAST_CONTACT_DATE] == TODAY and cu[Col.LAST_CHANNEL] is E
    assert res.row_updates[Col.OUTCOME] is Outcome.NO_RESPONSE and res.row_updates[Col.NEXT_FOLLOWUP_DATE] == d(2)


def test_contact_attempts_total_includes_legacy_and_both_channels():
    res = apply(Outcome.NO_ANSWER, P, contact_kw={"email_attempts": 2, "phone_attempts": 1, "legacy_attempts": 3})
    assert res.contact_updates[Col.CONTACT_ATTEMPTS] == 2 + 2 + 3 and res.attempt_no_for_channel == 2


def test_fifth_unanswered_email_moves_on_when_phone_is_still_usable():
    res = apply(Outcome.NO_RESPONSE, contact_kw={"email_attempts": 4})
    assert res.attempt_no_for_channel == 5 and res.status_after is Status.IN_PROGRESS


def test_fifth_unanswered_email_escalates_at_once_when_nothing_else_is_usable():
    res = apply(Outcome.NO_RESPONSE, row_kw={"phone_valid": False}, contact_kw={"email_attempts": 4})
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is HumanReason.ATTEMPT_CAP
    assert res.row_updates[Col.HUMAN_FLAG] == "Yes" and res.row_updates[Col.NEXT_FOLLOWUP_DATE] == ""


def test_fifth_unanswered_call_after_email_is_used_up_escalates():
    res = apply(Outcome.NO_ANSWER, P, contact_kw={"email_attempts": 5, "phone_attempts": 4})
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is HumanReason.ATTEMPT_CAP


def test_a_real_response_on_the_last_attempt_is_not_escalated_straight_away():
    res = apply(Outcome.PROMISE_TO_PAY, promised_days=3, row_kw={"phone_valid": False}, contact_kw={"email_attempts": 4})
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == d(3)  # decide_action escalates later


def test_responses_are_counted_per_channel_for_real_responses_only():
    assert apply(Outcome.DISPUTE).contact_updates[Col.EMAIL_RESPONSES] == 1
    assert apply(Outcome.DISPUTE, P).contact_updates[Col.PHONE_RESPONSES] == 1
    assert apply(Outcome.NO_RESPONSE).contact_updates[Col.EMAIL_RESPONSES] == 0
    assert apply(Outcome.OUT_OF_OFFICE, promised_days=7).contact_updates[Col.EMAIL_RESPONSES] == 0


@pytest.mark.parametrize("days, expect", [(1, d(1)), (2, d(2)), (5, d(5)), (0, d(1)), (-3, d(1))])
def test_br09_stated_date_within_five_days_is_used_without_approval(days, expect):
    res = apply(Outcome.PROMISE_TO_PAY, promised_days=days)
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == expect
    assert not res.approval_requested and res.row_updates[Col.APPROVAL_STATUS] is ApprovalStatus.NONE


@pytest.mark.parametrize("days", [6, 7, 14, 30])
@pytest.mark.parametrize("outcome", [Outcome.PROMISE_TO_PAY, Outcome.CALLBACK_REQUESTED, Outcome.PARTIAL_PAYMENT])
def test_br10_more_than_five_days_needs_human_approval(days, outcome):
    res = apply(outcome, promised_days=days, amount_paid=100.0)
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is HumanReason.EXTENSION_APPROVAL_PENDING
    assert res.approval_requested and res.row_updates[Col.APPROVAL_STATUS] is ApprovalStatus.PENDING
    assert res.row_updates[Col.PENDING_APPROVAL_DATE] == d(days) and res.row_updates[Col.NEXT_FOLLOWUP_DATE] == ""
    assert res.row_updates[Col.LAST_PROMISED_DATE] == d(days)


def test_br11_fourth_extension_request_is_not_offered_for_approval():
    res = apply(Outcome.PROMISE_TO_PAY, promised_days=14, contact_kw={"approvals": 3})
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is HumanReason.EXTENSION_LIMIT_REACHED
    assert not res.approval_requested and res.row_updates[Col.APPROVAL_STATUS] is ApprovalStatus.NONE
    assert apply(Outcome.PROMISE_TO_PAY, promised_days=14, contact_kw={"approvals": 2}).approval_requested


def test_the_approval_cap_does_not_block_short_promises():
    res = apply(Outcome.PROMISE_TO_PAY, promised_days=4, contact_kw={"approvals": 3})
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == d(4)


def test_out_of_office_return_date_needs_no_approval_and_uses_none():
    res = apply(Outcome.OUT_OF_OFFICE, promised_days=30, contact_kw={"approvals": 3})
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == d(30) and not res.approval_requested
    assert res.contact_updates[Col.EXTENSION_APPROVALS_COUNT] == 3
    assert res.contact_updates[Col.LAST_CONTACT_DATE] == TODAY  # the auto-reply still counted as an attempt


def test_date_outcomes_without_a_date_fall_back_to_the_default_gap():
    assert apply(Outcome.PROMISE_TO_PAY).next_followup_date == d(2)
    assert apply(Outcome.CALLBACK_REQUESTED, P).next_followup_date == d(2)


@pytest.mark.parametrize("outcome, days", [(Outcome.PAYMENT_IN_PROCESS, 3), (Outcome.REQUESTS_INVOICE_COPY, 2),
                                           (Outcome.EMAIL_BOUNCE_SOFT, 1), (Outcome.NO_RESPONSE, 2)])
def test_follow_up_gaps_come_from_the_taxonomy(outcome, days):
    assert apply(outcome).next_followup_date == d(days)


@pytest.mark.parametrize("outcome, channel, gap", [(Outcome.PHONE_LINE_BUSY, P, 1), (Outcome.NO_ANSWER, P, 2), (Outcome.LEFT_VOICEMAIL, P, 2)])
def test_phone_retry_gaps(outcome, channel, gap):
    assert apply(outcome, channel).next_followup_date == d(gap)


def test_paid_sets_the_balance_to_zero_and_handles_the_row():
    res = apply(Outcome.PAID, amount_paid=1000.0)
    assert res.status_after is Status.HANDLED and res.row_updates[Col.AMOUNT_OUTSTANDING] == 0
    assert res.row_updates[Col.NEXT_FOLLOWUP_DATE] == "" and res.row_updates[Col.HUMAN_FLAG] == ""
    assert not res.applies_to_all_rows


def test_partial_payment_reduces_the_balance_and_keeps_following_up():
    res = apply(Outcome.PARTIAL_PAYMENT, amount_paid=250.0, promised_days=3)
    assert res.status_after is Status.IN_PROGRESS and res.row_updates[Col.AMOUNT_OUTSTANDING] == 750.0
    assert res.next_followup_date == d(3)
    full = apply(Outcome.PARTIAL_PAYMENT, amount_paid=1000.0)
    assert full.status_after is Status.HANDLED and full.row_updates[Col.AMOUNT_OUTSTANDING] == 0


@pytest.mark.parametrize("outcome, reason", [
    (Outcome.DISPUTE, HumanReason.DISPUTE), (Outcome.SETTLEMENT_REQUESTED, HumanReason.SETTLEMENT),
    (Outcome.WRONG_CONTACT, HumanReason.WRONG_CONTACT), (Outcome.REFUSED_TO_PAY, HumanReason.REFUSED)])
def test_br16_escalating_outcomes(outcome, reason):
    res = apply(outcome)
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is reason
    assert res.row_updates[Col.HUMAN_FLAG] == "Yes" and res.row_updates[Col.HUMAN_REASON] is reason


def test_wrong_contact_switches_both_channels_off():
    cu = apply(Outcome.WRONG_CONTACT).contact_updates
    assert cu[Col.EMAIL_DISABLED] == "Yes" and cu[Col.PHONE_DISABLED] == "Yes"


@pytest.mark.parametrize("outcome, channel, email_off, phone_off", [
    (Outcome.EMAIL_BOUNCE_HARD, E, "Yes", ""), (Outcome.EMAIL_INVALID_ADDRESS, E, "Yes", ""),
    (Outcome.PHONE_INVALID_NUMBER, P, "", "Yes"), (Outcome.PHONE_DISCONNECTED, P, "", "Yes"),
    (Outcome.EMAIL_BOUNCE_SOFT, E, "", ""), (Outcome.PHONE_LINE_BUSY, P, "", "")])
def test_br13_which_errors_switch_a_channel_off(outcome, channel, email_off, phone_off):
    res = apply(outcome, channel)
    assert res.contact_updates[Col.EMAIL_DISABLED] == email_off and res.contact_updates[Col.PHONE_DISABLED] == phone_off
    assert res.counted_as_attempt and res.status_after is Status.IN_PROGRESS


def test_a_disabled_channel_stays_disabled_on_later_outcomes():
    res = apply(Outcome.NO_ANSWER, P, contact_kw={"email_disabled": True})
    assert res.contact_updates[Col.EMAIL_DISABLED] == "Yes"


def test_switching_off_the_last_channel_escalates_at_once():
    res = apply(Outcome.EMAIL_BOUNCE_HARD, row_kw={"phone_valid": False})
    assert res.status_after is Status.NEEDS_HUMAN and res.human_reason is HumanReason.NO_CHANNEL_LEFT
    res = apply(Outcome.PHONE_INVALID_NUMBER, P, row_kw={"email_valid": False})
    assert res.human_reason is HumanReason.NO_CHANNEL_LEFT
    res = apply(Outcome.EMAIL_BOUNCE_HARD, contact_kw={"phone_attempts": 5})  # phone is at the cap
    assert res.human_reason is HumanReason.ATTEMPT_CAP


def test_br14_system_errors_do_not_use_an_attempt_and_three_in_a_row_escalate():
    res = apply(Outcome.EMAIL_SEND_FAILURE)
    assert not res.counted_as_attempt and res.attempt_no_for_channel is None
    assert res.contact_updates[Col.EMAIL_ATTEMPTS] == 0 and Col.LAST_CONTACT_DATE not in res.contact_updates
    assert res.contact_updates[Col.CONSECUTIVE_SYSTEM_ERRORS] == 1
    assert res.status_after is Status.IN_PROGRESS and res.next_followup_date == d(1)
    assert apply(Outcome.SYSTEM_ERROR, contact_kw={"system_errors": 1}).contact_updates[Col.CONSECUTIVE_SYSTEM_ERRORS] == 2
    third = apply(Outcome.PHONE_SYSTEM_ERROR, P, contact_kw={"system_errors": 2})
    assert third.status_after is Status.NEEDS_HUMAN and third.human_reason is HumanReason.SYSTEM_ERRORS_REPEATED
    assert apply(Outcome.NO_RESPONSE, contact_kw={"system_errors": 2}).contact_updates[Col.CONSECUTIVE_SYSTEM_ERRORS] == 0


def test_invoice_level_outcomes_only_change_the_primary_row():
    for o in (Outcome.PAID, Outcome.PARTIAL_PAYMENT, Outcome.DISPUTE, Outcome.REQUESTS_INVOICE_COPY):
        assert not apply(o, amount_paid=10.0).applies_to_all_rows
    for o in (Outcome.PROMISE_TO_PAY, Outcome.NO_RESPONSE, Outcome.WRONG_CONTACT, Outcome.SETTLEMENT_REQUESTED):
        assert apply(o).applies_to_all_rows
    cov = covered_row_updates(TODAY, NOW)
    assert cov[Col.NEXT_FOLLOWUP_DATE] == d(1) and cov[Col.LAST_PROCESSED_DATE] == TODAY and Col.STATUS not in cov


def test_apply_outcome_rejects_impossible_combinations():
    with pytest.raises(ValueError, match="cannot happen"):
        apply(Outcome.NO_ANSWER, E)
    with pytest.raises(ValueError, match="cannot happen"):
        apply(Outcome.MAX_ATTEMPTS_REACHED, E)
    with pytest.raises(ValueError):
        apply("NOT_AN_OUTCOME")


@pytest.mark.parametrize("channel", [E, P])
def test_every_outcome_writes_only_valid_columns_and_never_notes(channel):
    for o in outcomes_for_channel(channel):
        res = apply(o, channel, promised_days=7, amount_paid=100.0, summary="s")
        keys = set(res.row_updates) | set(res.contact_updates)
        assert keys <= set(INVOICES_COLUMNS), keys - set(INVOICES_COLUMNS)
        assert not keys & PROTECTED_COLUMNS
        assert res.row_updates[Col.LAST_PROCESSED_DATE] == TODAY and res.row_updates[Col.SUMMARY] == "s"
        assert res.status_after in set(Status) - {Status.NEW, Status.DQ_FAILED}
        nxt = res.next_followup_date
        assert nxt is None or nxt >= d(1)  # BR-06: never earlier than tomorrow


# ---------------------------------------------------------------- human actions


def test_approve_extension_resumes_the_row_and_uses_one_approval():
    pending = row(status=Status.NEEDS_HUMAN, human_reason=HumanReason.EXTENSION_APPROVAL_PENDING,
                  approval_status=ApprovalStatus.PENDING, pending_approval_date=d(14), approvals=1)
    res = approve_extension(pending, contact_of(pending), TODAY, CFG, NOW)
    assert res.row_updates[Col.STATUS] is Status.IN_PROGRESS and res.row_updates[Col.NEXT_FOLLOWUP_DATE] == d(14)
    assert res.row_updates[Col.APPROVAL_STATUS] is ApprovalStatus.APPROVED and res.row_updates[Col.HUMAN_FLAG] == ""
    assert res.contact_updates[Col.EXTENSION_APPROVALS_COUNT] == 2 and "2 of 3" in res.audit_note


def test_approve_extension_refuses_without_a_pending_request_or_past_the_cap():
    with pytest.raises(ValueError, match="no pending"):
        approve_extension(row(), ContactState(), TODAY, CFG)
    pending = row(approval_status=ApprovalStatus.PENDING, pending_approval_date=d(9), approvals=3)
    with pytest.raises(ValueError, match="already used 3"):
        approve_extension(pending, contact_of(pending), TODAY, CFG)


def test_reject_extension_keeps_the_row_with_a_person():
    pending = row(status=Status.NEEDS_HUMAN, approval_status=ApprovalStatus.PENDING, pending_approval_date=d(9))
    res = reject_extension(pending, TODAY, NOW)
    assert res.row_updates[Col.APPROVAL_STATUS] is ApprovalStatus.REJECTED and Col.STATUS not in res.row_updates
    assert res.contact_updates == {}
    with pytest.raises(ValueError):
        reject_extension(row(), TODAY)


def test_resolve_row_can_close_or_return_to_follow_up():
    human = row(status=Status.NEEDS_HUMAN, human_reason=HumanReason.DISPUTE)
    closed = resolve_row(human, TODAY, NOW, close=True, note="Credit note issued")
    assert closed.row_updates[Col.STATUS] is Status.HANDLED and closed.row_updates[Col.HUMAN_RESOLUTION_NOTE] == "Credit note issued"
    back = resolve_row(human, TODAY, NOW, reset_attempts=True, reenable_channels=True, next_followup_date=d(3))
    assert back.row_updates[Col.STATUS] is Status.IN_PROGRESS and back.row_updates[Col.NEXT_FOLLOWUP_DATE] == d(3)
    assert back.contact_updates[Col.EMAIL_ATTEMPTS] == 0 and back.contact_updates[Col.PHONE_DISABLED] == ""
    plain = resolve_row(human, TODAY, NOW)
    assert plain.contact_updates == {} and Col.HUMAN_RESOLUTION_NOTE not in plain.row_updates


def test_resolve_row_refuses_data_quality_failures_and_rows_not_with_a_person():
    with pytest.raises(ValueError, match="fix the data"):
        resolve_row(row(status=Status.DQ_FAILED), TODAY)
    with pytest.raises(ValueError, match="not NEEDS_HUMAN"):
        resolve_row(row(status=Status.IN_PROGRESS), TODAY)


# ---------------------------------------------------------------- simulations through the sheet value path


class SimContact:
    """One contact with one invoice, stored as sheet strings so values go through to_cell like in production."""

    def __init__(self, **sheet: str) -> None:
        self.sheet = {Col.ROW_KEY: "INV-1#1", Col.DUE_DATE: "2026-07-01", Col.AMOUNT_OUTSTANDING: "1000",
                      Col.READY_FOR_CONTACT: "Yes", **sheet}
        self.dq = {"dq_status": "PASS", "can_contact_email": True, "can_contact_phone": True, "contact_key": "k"}
        self.log: list[tuple[dt.date, str, str]] = []

    def write(self, updates: dict) -> None:
        for k, v in updates.items():
            self.sheet[k] = "" if to_cell(v) == "" else str(to_cell(v))

    def day(self, today: dt.date, outcome_for, **kw) -> RowState:
        state = RowState.from_row(self.sheet, self.dq)
        ev = plan_contact_event([state], today, CFG)
        if ev is None:
            action = decide_action(state, ContactState.from_rows([state]), today, CFG)
            self.write(action.updates)
            self.log.append((today, action.kind.value, action.reason))
        else:
            outcome = outcome_for(ev.channel)
            res = apply_outcome(state, ContactState.from_rows([state]), ev.channel, outcome, today, CFG, **kw)
            self.write(res.contact_updates)
            self.write(res.row_updates)
            self.log.append((today, ev.channel.value, outcome.value))
        return RowState.from_row(self.sheet, self.dq)


def test_ten_unanswered_attempts_then_a_human_never_an_eleventh():
    sim = SimContact()
    unanswered = lambda ch: Outcome.NO_RESPONSE if ch is E else Outcome.NO_ANSWER  # noqa: E731
    for n in range(40):
        state = sim.day(TODAY + dt.timedelta(days=n), unanswered)
        if state.status is Status.NEEDS_HUMAN:
            break
    contacts = [(c, o) for _, c, o in sim.log if c in ("email", "phone")]
    assert [c for c, _ in contacts] == ["email"] * 5 + ["phone"] * 5
    assert state.status is Status.NEEDS_HUMAN and state.human_reason is HumanReason.ATTEMPT_CAP
    assert (state.email_attempts, state.phone_attempts) == (5, 5)
    # after escalation nothing more is contacted
    later = sim.day(TODAY + dt.timedelta(days=60), unanswered)
    assert later.phone_attempts == 5 and sim.log[-1][1] == "SKIP"


def test_attempts_are_at_least_two_days_apart_and_never_twice_in_a_day():
    sim = SimContact()
    for n in range(12):
        sim.day(TODAY + dt.timedelta(days=n), lambda ch: Outcome.NO_RESPONSE if ch is E else Outcome.NO_ANSWER)
    days = [d_ for d_, c, _ in sim.log if c in ("email", "phone")]
    assert len(days) == len(set(days))
    assert all((b - a).days >= 2 for a, b in zip(days, days[1:]))


def test_same_day_rerun_changes_nothing():
    sim = SimContact()
    first = sim.day(TODAY, lambda ch: Outcome.NO_RESPONSE)
    snapshot = dict(sim.sheet)
    again = sim.day(TODAY, lambda ch: Outcome.NO_RESPONSE)
    assert sim.sheet == snapshot and again.email_attempts == first.email_attempts == 1
    assert sim.log[-1] == (TODAY, "SKIP", Reason.ALREADY_PROCESSED)


def test_a_hard_bounce_moves_the_contact_to_phone_and_the_bounce_survives_the_daily_dq_refresh():
    sim = SimContact()
    sim.day(TODAY, lambda ch: Outcome.EMAIL_BOUNCE_HARD)
    state = sim.day(TODAY + dt.timedelta(days=1), lambda ch: Outcome.NO_ANSWER)
    assert sim.log[-1][1] == "phone" and state.email_disabled and state.email_valid  # address looks fine, bounce remembers


def test_a_long_promise_waits_for_approval_then_resumes_after_approval():
    sim = SimContact()
    state = sim.day(TODAY, lambda ch: Outcome.PROMISE_TO_PAY, promised_days=14)
    assert state.status is Status.NEEDS_HUMAN and state.approval_status is ApprovalStatus.PENDING
    waiting = sim.day(TODAY + dt.timedelta(days=1), lambda ch: Outcome.NO_RESPONSE)
    assert sim.log[-1][2] == Reason.WAITING_FOR_HUMAN and waiting.email_attempts == 1
    res = approve_extension(waiting, ContactState.from_rows([waiting]), TODAY + dt.timedelta(days=1), CFG)
    sim.write(res.row_updates)
    sim.write(res.contact_updates)
    approved = RowState.from_row(sim.sheet, sim.dq)
    assert approved.status is Status.IN_PROGRESS and approved.next_followup_date == d(14) and approved.approvals == 1
    before = sim.day(TODAY + dt.timedelta(days=10), lambda ch: Outcome.NO_RESPONSE)
    assert before.email_attempts == 1  # still waiting for the promised date
    after = sim.day(d(14), lambda ch: Outcome.PAID, amount_paid=1000.0)
    assert after.status is Status.HANDLED


def test_three_approvals_then_the_fourth_request_goes_to_a_person_without_approval():
    sim = SimContact()
    day = TODAY
    for _ in range(3):
        state = sim.day(day, lambda ch: Outcome.PROMISE_TO_PAY, promised_days=10)
        assert state.approval_status is ApprovalStatus.PENDING
        res = approve_extension(state, ContactState.from_rows([state]), day, CFG)
        sim.write(res.row_updates)
        sim.write(res.contact_updates)
        day = day + dt.timedelta(days=10)
    state = sim.day(day, lambda ch: Outcome.PROMISE_TO_PAY, promised_days=10)
    assert state.status is Status.NEEDS_HUMAN and state.human_reason is HumanReason.EXTENSION_LIMIT_REACHED
    assert state.approvals == 3 and state.approval_status is ApprovalStatus.NONE


# ---------------------------------------------------------------- the real file


@pytest.fixture(scope="module")
def real():
    df = pd.read_csv(Path(__file__).parent / "fixtures" / "overdue_invoices.csv", dtype=str, keep_default_na=False)
    df[Col.ROW_KEY] = df[Col.INVOICE_NUMBER] + "#" + (df.groupby(Col.INVOICE_NUMBER).cumcount() + 1).astype(str)
    dq = check_quality(df, run_date=TODAY)
    updates = {r[Col.ROW_KEY]: initialize_row(r, dq.iloc[i], TODAY, CFG, NOW) for i, r in enumerate(df.to_dict("records"))}
    return df, dq, updates


def test_baseline_buckets_match_the_design_3_1_6_25(real):
    _, _, updates = real
    counts = pd.Series([u[Col.STATUS].value for u in updates.values()]).value_counts().to_dict()
    assert counts == {"IN_PROGRESS": 25, "NEEDS_HUMAN": 6, "DQ_FAILED": 3, "HANDLED": 1}
    assert sum(counts.values()) == 35


def test_baseline_rows_and_reasons(real):
    _, _, u = real
    assert {k for k, v in u.items() if v[Col.STATUS] is Status.DQ_FAILED} == {"INV-2026-4471#2", "CN-2026-0118#1", "INV-2026-4144#1"}
    assert {k for k, v in u.items() if v[Col.STATUS] is Status.HANDLED} == {"INV-2026-4265#1"}
    human = {k: v[Col.HUMAN_REASON] for k, v in u.items() if v[Col.STATUS] is Status.NEEDS_HUMAN}
    assert human == {
        "INV-2026-4177#1": HumanReason.DISPUTE, "INV-2026-3980#1": HumanReason.READY_NO, "INV-2026-3902#1": HumanReason.ATTEMPT_CAP_LEGACY,
        "INV-2026-4415#1": HumanReason.READY_NO, "INV-2026-4029#1": HumanReason.WRONG_CONTACT, "INV-2026-3955#1": HumanReason.SETTLEMENT,
    }
    assert u["CN-2026-0118#1"][Col.HUMAN_REASON] is HumanReason.NOT_AN_INVOICE


def test_baseline_counters_dates_and_derived_flag(real):
    _, _, u = real
    assert u["INV-2026-4402#1"][Col.PHONE_ATTEMPTS] == 1 and u["INV-2026-4402#1"][Col.LAST_CHANNEL] is Channel.PHONE
    assert u["INV-2026-4118#1"][Col.EMAIL_ATTEMPTS] == 1 and u["INV-2026-4118#1"][Col.NEXT_FOLLOWUP_DATE] == dt.date(2026, 9, 1)
    assert u["INV-2026-3902#1"][Col.LEGACY_ATTEMPTS] == 5 and u["INV-2026-4310#1"][Col.LEGACY_ATTEMPTS] == 2
    assert u["INV-2026-4233#1"][Col.NEXT_FOLLOWUP_DATE] == dt.date(2026, 8, 31)  # honoured, not sent for approval (A-03)
    assert u["INV-2026-4021#1"][Col.NEXT_FOLLOWUP_DATE] == dt.date(2026, 6, 30)
    assert u["INV-2026-4054#1"][Col.NEXT_FOLLOWUP_DATE] == dt.date(2026, 8, 15)
    assert Col.NEXT_FOLLOWUP_DATE not in u["INV-2026-4199#1"]  # "Paid ... on 12 Aug" is the payment date, not a promise
    assert u["INV-2026-4362#1"][Col.READY_FOR_CONTACT] == "Yes" and Col.READY_FOR_CONTACT not in u["INV-2026-4488#1"]
    assert all(v[Col.APPROVAL_STATUS] is ApprovalStatus.NONE and v[Col.EXTENSION_APPROVALS_COUNT] == 0 for v in u.values())


def test_initialize_never_touches_notes_and_writes_only_real_columns(real):
    _, _, u = real
    for v in u.values():
        assert set(v) <= set(INVOICES_COLUMNS) and not set(v) & PROTECTED_COLUMNS


def test_first_day_plan_on_the_real_file(real):
    df, dq, updates = real
    states = []
    for i, r in enumerate(df.to_dict("records")):
        sheet = {**r, **{k: str(to_cell(v)) for k, v in updates[r[Col.ROW_KEY]].items()}}
        states.append(RowState.from_row(sheet, dq.iloc[i]))
    by_contact: dict[str, list[RowState]] = {}
    for s in states:
        by_contact.setdefault(s.contact_key, []).append(s)
    events = {k: plan_contact_event(v, TODAY, CFG, NOW) for k, v in by_contact.items()}
    events = {k: ev for k, ev in events.items() if ev}

    veltner = events["t.berger@veltner-gastro.at"]  # three invoices, one email
    assert veltner.channel is E and veltner.row_keys == ["INV-2026-4288#1", "INV-2026-4351#1", "INV-2026-4390#1"]
    assert veltner.primary_row_key == "INV-2026-4288#1"
    phone_only = {ev.primary_row_key: ev.channel for ev in events.values()}
    for key in ("INV-2026-4301#1", "INV-2026-4276#1", "INV-2026-4066#1"):  # no usable email
        assert phone_only[key] is P
    assert phone_only["INV-2026-4488#1"] is E
    contacted = {k for ev in events.values() for k in ev.row_keys}
    # never contacted on day one: failed rows, humans, paid, not yet due, and the promises still in the future
    for key in ("INV-2026-4471#2", "CN-2026-0118#1", "INV-2026-4144#1", "INV-2026-4265#1", "INV-2026-3980#1", "INV-2026-4415#1",
                "INV-2026-3902#1", "INV-2026-4177#1", "INV-2026-4029#1", "INV-2026-3955#1", "INV-2026-4512#1",
                "INV-2026-4233#1", "INV-2026-4118#1"):
        assert key not in contacted, key
    assert "INV-2026-4471#1" in contacted and "INV-2026-4021#1" in contacted  # master copy and the broken promise are due
    nordbach = events["k.vogel@nordbach-handels.de"]  # 4471 and 4362 combined; the duplicate is excluded
    assert nordbach.row_keys == ["INV-2026-4362#1", "INV-2026-4471#1"]  # oldest due date first: 13 Jul, then 8 Aug
    assert nordbach.primary_row_key == "INV-2026-4362#1"

