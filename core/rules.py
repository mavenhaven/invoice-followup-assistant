"""Business-rules engine (BR-01 to BR-24, docs/DESIGN.md section 5). Pure functions, no I/O.

The runner (Prompt 6) does the reading and writing. For each contact it does roughly:

    states   = [RowState.from_row(row, dq_row) for each row of the contact]
    contact  = ContactState.from_rows(states)
    event    = plan_contact_event(states, today, config)        # who is contacted, on which channel
    response = mock_responses.generate_response(...)
    result   = apply_outcome(primary_state, contact, event.channel, response.outcome, today, ...)

Everything returned as "updates" is a dict {sheet column name: value}. Values can be enums, dates
or None; SheetStore converts them. Contact-level fields (attempt counters, disabled channels,
approvals, system-error streak, last contact) are replicated onto every open row of the contact.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Any, Iterable, Mapping, Sequence

from core.quality import parse_amount, parse_date
from core.schema import ApprovalStatus, Channel, Col, Config, DqStatus, HumanReason, Status
from core.taxonomy import Outcome, extract_promised_date, info, is_system_error, parse_outcome

# Outcomes about one invoice. For these only the primary row of a combined contact is changed;
# the other invoices in the same contact are just marked as covered.
INVOICE_LEVEL_OUTCOMES = frozenset({
    Outcome.PAID, Outcome.PARTIAL_PAYMENT, Outcome.DISPUTE, Outcome.REQUESTS_INVOICE_COPY,
})
# Existing sheet outcomes that already mean "a person must act" (assumption A-09).
HUMAN_OUTCOME_REASONS = {
    Outcome.DISPUTE: HumanReason.DISPUTE,
    Outcome.SETTLEMENT_REQUESTED: HumanReason.SETTLEMENT,
    Outcome.WRONG_CONTACT: HumanReason.WRONG_CONTACT,
    Outcome.REFUSED_TO_PAY: HumanReason.REFUSED,
}
CHANNEL_ORDER = (Channel.EMAIL, Channel.PHONE)  # BR-02: email first


# ---------------------------------------------------------------- state objects


def _yes(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("yes", "true", "1")


def _int(value: Any) -> int:
    try:
        return int(float(str(value).strip())) if str(value or "").strip() else 0
    except ValueError:
        return 0


def _enum(cls: type[enum.Enum], value: Any, default: Any = None) -> Any:
    text = str(value or "").strip()
    if not text:
        return default
    for candidate in (text, text.upper(), text.lower(), text.upper().replace(" ", "_")):  # people type these by hand
        try:
            return cls(candidate)
        except ValueError:
            continue
    return default


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return parse_date(str(value or ""))


@dataclasses.dataclass
class RowState:
    """What the engine needs to know about one invoice row."""

    row_key: str
    contact_key: str = ""
    status: Status = Status.NEW
    outcome: Outcome | None = None
    amount_outstanding: float | None = None
    due_date: dt.date | None = None
    next_followup_date: dt.date | None = None
    last_processed_date: dt.date | None = None
    last_contact_date: dt.date | None = None
    last_promised_date: dt.date | None = None
    pending_approval_date: dt.date | None = None
    ready: str = ""  # "yes", "no" or ""
    dq_status: DqStatus = DqStatus.PASS
    dq_exclusion_reason: str = ""
    dq_human_reason: str = ""
    email_valid: bool = True
    phone_valid: bool = True
    human_reason: HumanReason | None = None
    approval_status: ApprovalStatus = ApprovalStatus.NONE
    # contact-level fields as stored on this row
    email_attempts: int = 0
    phone_attempts: int = 0
    legacy_attempts: int = 0
    email_responses: int = 0
    phone_responses: int = 0
    email_disabled: bool = False
    phone_disabled: bool = False
    approvals: int = 0
    system_errors: int = 0
    last_channel: Channel | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any], dq: Mapping[str, Any] | None = None) -> "RowState":
        """Build from a sheet row (strings) plus, ideally, today's fresh check_quality() result row.

        Without ``dq`` the stored dq_status, can_contact_* and contact_key columns are used.
        """
        g = lambda c: row.get(c, "")  # noqa: E731
        if dq is not None:
            dq_status = _enum(DqStatus, dq.get("dq_status"), DqStatus.PASS)
            email_valid, phone_valid = bool(dq.get("can_contact_email")), bool(dq.get("can_contact_phone"))
            contact_key = str(dq.get("contact_key") or g(Col.CONTACT_KEY) or "")
            exclusion, dq_human = str(dq.get("exclusion_reason") or ""), str(dq.get("human_reason") or "")
        else:
            dq_status = _enum(DqStatus, g(Col.DQ_STATUS), DqStatus.PASS)
            email_valid, phone_valid = _yes(g(Col.CAN_CONTACT_EMAIL)), _yes(g(Col.CAN_CONTACT_PHONE))
            contact_key, exclusion, dq_human = str(g(Col.CONTACT_KEY) or ""), "", ""
        return cls(
            row_key=str(g(Col.ROW_KEY)), contact_key=contact_key,
            status=_enum(Status, g(Col.STATUS), Status.NEW), outcome=parse_outcome(str(g(Col.OUTCOME) or ""), strict=False),
            amount_outstanding=parse_amount(str(g(Col.AMOUNT_OUTSTANDING) or "")),
            due_date=_date(g(Col.DUE_DATE)), next_followup_date=_date(g(Col.NEXT_FOLLOWUP_DATE)),
            last_processed_date=_date(g(Col.LAST_PROCESSED_DATE)), last_contact_date=_date(g(Col.LAST_CONTACT_DATE)),
            last_promised_date=_date(g(Col.LAST_PROMISED_DATE)), pending_approval_date=_date(g(Col.PENDING_APPROVAL_DATE)),
            ready=str(g(Col.READY_FOR_CONTACT) or "").strip().lower(),
            dq_status=dq_status, dq_exclusion_reason=exclusion, dq_human_reason=dq_human,
            email_valid=email_valid, phone_valid=phone_valid,
            human_reason=_enum(HumanReason, g(Col.HUMAN_REASON)),
            approval_status=_enum(ApprovalStatus, g(Col.APPROVAL_STATUS), ApprovalStatus.NONE),
            email_attempts=_int(g(Col.EMAIL_ATTEMPTS)), phone_attempts=_int(g(Col.PHONE_ATTEMPTS)),
            legacy_attempts=_int(g(Col.LEGACY_ATTEMPTS)), email_responses=_int(g(Col.EMAIL_RESPONSES)),
            phone_responses=_int(g(Col.PHONE_RESPONSES)), email_disabled=_yes(g(Col.EMAIL_DISABLED)),
            phone_disabled=_yes(g(Col.PHONE_DISABLED)), approvals=_int(g(Col.EXTENSION_APPROVALS_COUNT)),
            system_errors=_int(g(Col.CONSECUTIVE_SYSTEM_ERRORS)), last_channel=_enum(Channel, g(Col.LAST_CHANNEL)),
        )


@dataclasses.dataclass(frozen=True)
class ContactState:
    """The contact-level counters. The maximum across a contact's rows, so a late-added row cannot reset them."""

    email_attempts: int = 0
    phone_attempts: int = 0
    legacy_attempts: int = 0
    email_responses: int = 0
    phone_responses: int = 0
    email_disabled: bool = False
    phone_disabled: bool = False
    approvals: int = 0
    system_errors: int = 0
    last_contact_date: dt.date | None = None
    last_channel: Channel | None = None

    @classmethod
    def from_rows(cls, rows: Iterable[RowState]) -> "ContactState":
        rows = list(rows)
        if not rows:
            return cls()
        dated = [r for r in rows if r.last_contact_date]
        latest = max(dated, key=lambda r: r.last_contact_date) if dated else None  # type: ignore[arg-type,return-value]
        return cls(
            email_attempts=max(r.email_attempts for r in rows), phone_attempts=max(r.phone_attempts for r in rows),
            legacy_attempts=max(r.legacy_attempts for r in rows), email_responses=max(r.email_responses for r in rows),
            phone_responses=max(r.phone_responses for r in rows),
            email_disabled=any(r.email_disabled for r in rows), phone_disabled=any(r.phone_disabled for r in rows),
            approvals=max(r.approvals for r in rows), system_errors=max(r.system_errors for r in rows),
            last_contact_date=latest.last_contact_date if latest else None,
            last_channel=latest.last_channel if latest else None,
        )

    def attempts(self, channel: Channel) -> int:
        return self.email_attempts if channel is Channel.EMAIL else self.phone_attempts


# ---------------------------------------------------------------- actions


class ActionKind(str, enum.Enum):
    CONTACT = "CONTACT"  # make an attempt on `channel`
    SKIP = "SKIP"  # nothing to do today
    MARK = "MARK"  # change status without contacting (escalate, handle, DQ fail)


class Reason:
    """Reason codes on an Action. The runner counts them for Run_Log."""

    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    NOT_DUE = "NOT_DUE"
    WAITING_FOLLOWUP = "WAITING_FOLLOWUP"
    CONTACTED_TODAY = "CONTACTED_TODAY"
    ALREADY_HANDLED = "ALREADY_HANDLED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    BELOW_MINIMUM = "BELOW_MINIMUM"
    DQ_FAILED = "DQ_FAILED"
    SETTLED = "SETTLED"
    READY_NO = "READY_NO"
    ATTEMPT_CAP = "ATTEMPT_CAP"
    ATTEMPT_CAP_LEGACY = "ATTEMPT_CAP_LEGACY"
    NO_CHANNEL_LEFT = "NO_CHANNEL_LEFT"
    CONTACT = "CONTACT"


@dataclasses.dataclass
class Action:
    kind: ActionKind
    reason: str
    channel: Channel | None = None
    human_reason: HumanReason | None = None
    # Sheet updates for this row. For a CONTACT action these are only the "release" updates
    # (for example DQ_FAILED back to IN_PROGRESS); apply them first, then the outcome's updates.
    updates: dict[str, Any] = dataclasses.field(default_factory=dict)


def _ts(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")


def _next_day(today: dt.date, days: int | None) -> dt.date:
    """A retry date is never earlier than tomorrow (BR-06: one contact per day)."""
    return today + dt.timedelta(days=max(days or 1, 1))


def _clear_human() -> dict[str, Any]:
    return {Col.HUMAN_FLAG: "", Col.HUMAN_REASON: ""}


def _needs_human(reason: HumanReason, today: dt.date, now: dt.datetime, *, outcome: Outcome | None = None,
                 changed: bool = True) -> dict[str, Any]:
    u: dict[str, Any] = {Col.STATUS: Status.NEEDS_HUMAN, Col.HUMAN_FLAG: "Yes", Col.HUMAN_REASON: reason,
                         Col.LAST_PROCESSED_DATE: today}
    if changed:
        u[Col.LAST_UPDATED] = _ts(now)
        if outcome is not None:
            u[Col.OUTCOME] = outcome
    return u


def channel_available(row: RowState, contact: ContactState, channel: Channel) -> bool:
    """The stored address is valid and no bounce or dead number has switched it off (on any of the contact's rows)."""
    if channel is Channel.EMAIL:
        return row.email_valid and not (row.email_disabled or contact.email_disabled)
    return row.phone_valid and not (row.phone_disabled or contact.phone_disabled)


def choose_channel(row: RowState, contact: ContactState, config: Config) -> tuple[Channel | None, HumanReason | None]:
    """BR-02 to BR-05. Email first; phone when email is used up or off; otherwise a reason to escalate.

    A callback request prefers the phone.
    """
    avail = {ch: channel_available(row, contact, ch) for ch in CHANNEL_ORDER}
    usable = {ch: avail[ch] and contact.attempts(ch) < config.attempt_cap for ch in CHANNEL_ORDER}
    if row.outcome is Outcome.CALLBACK_REQUESTED and usable[Channel.PHONE]:
        return Channel.PHONE, None
    for ch in CHANNEL_ORDER:
        if usable[ch]:
            return ch, None
    capped = any(avail[ch] and contact.attempts(ch) >= config.attempt_cap for ch in CHANNEL_ORDER)
    return None, HumanReason.ATTEMPT_CAP if capped else HumanReason.NO_CHANNEL_LEFT


def decide_action(row: RowState, contact: ContactState, today: dt.date, config: Config | None = None,
                  now: dt.datetime | None = None) -> Action:
    """Decide what to do with one row today. First matching step wins (BR-01, 02-07, 17, 18)."""
    cfg = config or Config()
    now = now or dt.datetime.combine(today, dt.time())
    stamp = {Col.LAST_PROCESSED_DATE: today}

    def skip(reason: str, **kw: Any) -> Action:
        return Action(ActionKind.SKIP, reason, updates={**stamp, **kw.get("extra", {})})

    # BR-07 idempotent daily run
    if row.last_processed_date == today:
        return Action(ActionKind.SKIP, Reason.ALREADY_PROCESSED)

    # BR-18 blocking data quality issue
    if row.dq_status is DqStatus.FAIL:
        if row.status is Status.DQ_FAILED:
            return skip(Reason.DQ_FAILED)
        reason = _enum(HumanReason, row.dq_human_reason, HumanReason.DQ)
        u = {Col.STATUS: Status.DQ_FAILED, Col.HUMAN_FLAG: "Yes", Col.HUMAN_REASON: reason, Col.OUTCOME: Outcome.DQ_FAILED,
             Col.LAST_PROCESSED_DATE: today, Col.LAST_UPDATED: _ts(now)}
        return Action(ActionKind.MARK, Reason.DQ_FAILED, human_reason=reason, updates=u)

    release: dict[str, Any] = {}
    if row.status is Status.DQ_FAILED:  # data was fixed; the row is back in play
        release = {Col.STATUS: Status.IN_PROGRESS, **_clear_human(), Col.LAST_UPDATED: _ts(now)}

    # Paid or nothing outstanding: handled
    settled = row.dq_exclusion_reason == "SETTLED" or row.outcome is Outcome.PAID or row.amount_outstanding == 0
    if settled:
        if row.status is Status.HANDLED:
            return skip(Reason.ALREADY_HANDLED)
        u = {Col.STATUS: Status.HANDLED, **_clear_human(), Col.NEXT_FOLLOWUP_DATE: "", Col.LAST_PROCESSED_DATE: today,
             Col.LAST_UPDATED: _ts(now)}
        return Action(ActionKind.MARK, Reason.SETTLED, updates=u)
    if row.status is Status.HANDLED:  # closed by a person
        return skip(Reason.ALREADY_HANDLED)

    # BR-17 Ready for Contact = No
    if row.ready == "no":
        if row.status is Status.NEEDS_HUMAN and row.human_reason is HumanReason.READY_NO:
            return skip(Reason.WAITING_FOR_HUMAN)
        return Action(ActionKind.MARK, Reason.READY_NO, human_reason=HumanReason.READY_NO,
                      updates=_needs_human(HumanReason.READY_NO, today, now))
    if row.status is Status.NEEDS_HUMAN and row.human_reason is HumanReason.READY_NO:  # flag was changed back to Yes
        release = {Col.STATUS: Status.IN_PROGRESS, **_clear_human(), Col.LAST_UPDATED: _ts(now)}
    elif row.status is Status.NEEDS_HUMAN:
        return skip(Reason.WAITING_FOR_HUMAN)

    if row.dq_exclusion_reason == "BELOW_MINIMUM":
        return skip(Reason.BELOW_MINIMUM, extra=release)

    # BR-01 overdue, and any follow-up date reached
    if row.due_date and row.due_date > today:
        return skip(Reason.NOT_DUE, extra=release)
    if row.next_followup_date and row.next_followup_date > today:
        return skip(Reason.WAITING_FOLLOWUP, extra=release)

    # BR-06 one contact per day
    if contact.last_contact_date == today:
        return skip(Reason.CONTACTED_TODAY, extra=release)

    # BR-19 safety net: legacy attempts of unknown channel already at the cap
    if contact.legacy_attempts >= cfg.attempt_cap:
        return Action(ActionKind.MARK, Reason.ATTEMPT_CAP_LEGACY, human_reason=HumanReason.ATTEMPT_CAP_LEGACY,
                      updates={**release, **_needs_human(HumanReason.ATTEMPT_CAP_LEGACY, today, now, outcome=Outcome.MAX_ATTEMPTS_REACHED)})

    # BR-02 to BR-05 channel
    channel, human = choose_channel(row, contact, cfg)
    if channel is None:
        assert human is not None
        outcome = Outcome.MAX_ATTEMPTS_REACHED if human is HumanReason.ATTEMPT_CAP else Outcome.NEEDS_HUMAN_FOLLOW_UP
        return Action(ActionKind.MARK, human.value, human_reason=human,
                      updates={**release, **_needs_human(human, today, now, outcome=outcome)})
    return Action(ActionKind.CONTACT, Reason.CONTACT, channel=channel, updates=release)


@dataclasses.dataclass
class ContactEvent:
    """One combined contact covering every due invoice of one contact (BR-06)."""

    channel: Channel
    row_keys: list[str]  # all rows covered, primary first
    primary_row_key: str
    release_updates: dict[str, dict[str, Any]]  # per row_key, apply before the outcome


def plan_contact_event(rows: Sequence[RowState], today: dt.date, config: Config | None = None,
                       now: dt.datetime | None = None) -> ContactEvent | None:
    """For the rows of ONE contact, return the single contact to make today, or None.

    The primary row is the oldest due invoice. The channel comes from the contact-level counters.
    """
    cfg = config or Config()
    contact = ContactState.from_rows(rows)
    due: list[tuple[RowState, Action]] = []
    for r in rows:
        a = decide_action(r, contact, today, cfg, now)
        if a.kind is ActionKind.CONTACT:
            due.append((r, a))
    if not due:
        return None
    due.sort(key=lambda p: (p[0].due_date or dt.date.max, p[0].row_key))
    channel = due[0][1].channel
    assert channel is not None
    return ContactEvent(channel=channel, row_keys=[r.row_key for r, _ in due], primary_row_key=due[0][0].row_key,
                        release_updates={r.row_key: a.updates for r, a in due if a.updates})


# ---------------------------------------------------------------- applying an outcome


@dataclasses.dataclass
class OutcomeResult:
    outcome: Outcome
    channel: Channel
    counted_as_attempt: bool
    attempt_no_for_channel: int | None
    status_before: Status
    status_after: Status
    human_reason: HumanReason | None
    approval_requested: bool
    next_followup_date: dt.date | None
    contact_updates: dict[str, Any]  # replicate onto every open row of the contact
    row_updates: dict[str, Any]  # primary row (and every row of the event if applies_to_all_rows)
    applies_to_all_rows: bool


def _contact_after(contact: ContactState, **changes: Any) -> ContactState:
    return dataclasses.replace(contact, **changes)


def covered_row_updates(today: dt.date, now: dt.datetime | None = None) -> dict[str, Any]:
    """Updates for the other invoices in a combined contact when the outcome was about one invoice.

    They keep their status and are due again tomorrow.
    """
    now = now or dt.datetime.combine(today, dt.time())
    return {Col.LAST_PROCESSED_DATE: today, Col.NEXT_FOLLOWUP_DATE: _next_day(today, 1), Col.LAST_UPDATED: _ts(now)}


def apply_outcome(row: RowState, contact: ContactState, channel: Channel | str, outcome: Outcome | str,
                  today: dt.date, config: Config | None = None, *, promised_days: int | None = None,
                  amount_paid: float | None = None, summary: str = "", now: dt.datetime | None = None) -> OutcomeResult:
    """Turn one contact outcome into sheet updates (BR-03, 08-16, 19)."""
    cfg = config or Config()
    ch, oc = Channel(channel), Outcome(outcome)
    oi = info(oc)
    if ch not in oi.channels:
        raise ValueError(f"{oc.value} cannot happen on the {ch.value} channel")
    now = now or dt.datetime.combine(today, dt.time())
    status_before = row.status

    # --- contact-level counters
    counted = oi.counts_as_attempt
    email_a, phone_a = contact.email_attempts, contact.phone_attempts
    email_r, phone_r = contact.email_responses, contact.phone_responses
    attempt_no: int | None = None
    if counted:
        if ch is Channel.EMAIL:
            email_a, attempt_no = email_a + 1, email_a + 1
        else:
            phone_a, attempt_no = phone_a + 1, phone_a + 1
    if oi.is_real_response:
        email_r, phone_r = (email_r + 1, phone_r) if ch is Channel.EMAIL else (email_r, phone_r + 1)
    email_off = contact.email_disabled or Channel.EMAIL in oi.disables_channels
    phone_off = contact.phone_disabled or Channel.PHONE in oi.disables_channels
    system_errors = contact.system_errors + 1 if is_system_error(oc) else 0
    after = _contact_after(contact, email_attempts=email_a, phone_attempts=phone_a, email_responses=email_r,
                           phone_responses=phone_r, email_disabled=email_off, phone_disabled=phone_off,
                           system_errors=system_errors)
    contact_updates: dict[str, Any] = {
        Col.EMAIL_ATTEMPTS: email_a, Col.PHONE_ATTEMPTS: phone_a, Col.LEGACY_ATTEMPTS: contact.legacy_attempts,
        Col.CONTACT_ATTEMPTS: email_a + phone_a + contact.legacy_attempts,
        Col.EMAIL_RESPONSES: email_r, Col.PHONE_RESPONSES: phone_r,
        Col.EMAIL_DISABLED: "Yes" if email_off else "", Col.PHONE_DISABLED: "Yes" if phone_off else "",
        Col.CONSECUTIVE_SYSTEM_ERRORS: system_errors, Col.EXTENSION_APPROVALS_COUNT: contact.approvals,
    }
    if counted:
        contact_updates[Col.LAST_CONTACT_DATE] = today
        contact_updates[Col.LAST_CHANNEL] = ch

    # --- row-level result
    u: dict[str, Any] = {Col.OUTCOME: oc, Col.LAST_PROCESSED_DATE: today, Col.LAST_UPDATED: _ts(now)}
    if summary:
        u[Col.SUMMARY] = summary
    status = Status.IN_PROGRESS
    human: HumanReason | None = None
    approval_requested = False
    next_date: dt.date | None = None
    approval_status, pending = ApprovalStatus.NONE, ""

    paid_in_full = oc is Outcome.PAID
    if oc in (Outcome.PAID, Outcome.PARTIAL_PAYMENT):
        outstanding = row.amount_outstanding or 0.0
        paid = outstanding if oc is Outcome.PAID or amount_paid is None else amount_paid
        remaining = round(max(outstanding - paid, 0.0), 2)
        u[Col.AMOUNT_OUTSTANDING] = remaining
        paid_in_full = remaining == 0

    if paid_in_full:
        status = Status.HANDLED
    elif oi.status_effect is Status.NEEDS_HUMAN:
        status, human = Status.NEEDS_HUMAN, oi.human_reason
    else:
        if oi.date_kind is not None and promised_days is not None:
            stated = today + dt.timedelta(days=promised_days)
            u[Col.LAST_PROMISED_DATE] = stated
            if oi.exempt_from_approval or promised_days <= cfg.promise_max_days:
                next_date = max(stated, _next_day(today, 1))  # BR-09
            elif contact.approvals >= cfg.max_approvals:  # BR-11
                status, human = Status.NEEDS_HUMAN, HumanReason.EXTENSION_LIMIT_REACHED
            else:  # BR-10
                status, human = Status.NEEDS_HUMAN, HumanReason.EXTENSION_APPROVAL_PENDING
                approval_requested, approval_status, pending = True, ApprovalStatus.PENDING, stated
        else:
            next_date = _next_day(today, oi.followup_days or cfg.default_retry_days)  # BR-12

        # BR-14 repeated system errors
        if status is Status.IN_PROGRESS and system_errors >= cfg.system_error_limit:
            status, human, next_date = Status.NEEDS_HUMAN, HumanReason.SYSTEM_ERRORS_REPEATED, None
        # BR-05 straight away when an unanswered attempt used up the last usable channel
        elif status is Status.IN_PROGRESS and not oi.is_real_response and oc is not Outcome.OUT_OF_OFFICE:
            row_after = dataclasses.replace(row, outcome=oc)
            nxt, why = choose_channel(row_after, after, cfg)
            if nxt is None:
                status, human, next_date = Status.NEEDS_HUMAN, why, None

    u[Col.STATUS] = status
    u[Col.NEXT_FOLLOWUP_DATE] = next_date if next_date else ""
    u[Col.APPROVAL_STATUS] = approval_status
    u[Col.PENDING_APPROVAL_DATE] = pending
    if status is Status.NEEDS_HUMAN:
        u[Col.HUMAN_FLAG], u[Col.HUMAN_REASON] = "Yes", human
    else:
        u.update(_clear_human())
    return OutcomeResult(
        outcome=oc, channel=ch, counted_as_attempt=counted, attempt_no_for_channel=attempt_no,
        status_before=status_before, status_after=status, human_reason=human, approval_requested=approval_requested,
        next_followup_date=next_date, contact_updates=contact_updates, row_updates=u,
        applies_to_all_rows=oc not in INVOICE_LEVEL_OUTCOMES,
    )


# ---------------------------------------------------------------- human actions (BR-10, BR-21)


@dataclasses.dataclass
class HumanActionResult:
    row_updates: dict[str, Any]
    contact_updates: dict[str, Any] = dataclasses.field(default_factory=dict)
    audit_note: str = ""


def approve_extension(row: RowState, contact: ContactState, today: dt.date, config: Config | None = None,
                      now: dt.datetime | None = None) -> HumanActionResult:
    """A person approves the pending date: the row resumes on that date and one approval is used."""
    cfg = config or Config()
    now = now or dt.datetime.combine(today, dt.time())
    if row.approval_status is not ApprovalStatus.PENDING or row.pending_approval_date is None:
        raise ValueError(f"{row.row_key} has no pending extension to approve")
    if contact.approvals >= cfg.max_approvals:
        raise ValueError(f"{row.row_key}: this contact already used {cfg.max_approvals} approvals")
    date = row.pending_approval_date
    return HumanActionResult(
        row_updates={Col.STATUS: Status.IN_PROGRESS, **_clear_human(), Col.APPROVAL_STATUS: ApprovalStatus.APPROVED,
                     Col.PENDING_APPROVAL_DATE: "", Col.NEXT_FOLLOWUP_DATE: max(date, _next_day(today, 1)),
                     Col.LAST_PROMISED_DATE: date, Col.LAST_UPDATED: _ts(now)},
        contact_updates={Col.EXTENSION_APPROVALS_COUNT: contact.approvals + 1},
        audit_note=f"Extension to {date.isoformat()} approved ({contact.approvals + 1} of {cfg.max_approvals})",
    )


def reject_extension(row: RowState, today: dt.date, now: dt.datetime | None = None) -> HumanActionResult:
    """A person rejects the pending date. The row stays with a human, marked REJECTED, and no approval is used."""
    now = now or dt.datetime.combine(today, dt.time())
    if row.approval_status is not ApprovalStatus.PENDING:
        raise ValueError(f"{row.row_key} has no pending extension to reject")
    return HumanActionResult(
        row_updates={Col.APPROVAL_STATUS: ApprovalStatus.REJECTED, Col.LAST_UPDATED: _ts(now)},
        audit_note="Extension rejected",
    )


def is_open(row: RowState) -> bool:
    """Counters are replicated onto a contact's open rows: not paid or closed, and not failed data quality."""
    return row.status not in (Status.HANDLED, Status.DQ_FAILED) and row.dq_status is not DqStatus.FAIL


def fan_out_updates(rows: Sequence[RowState], contact_updates: Mapping[str, Any],
                    row_updates: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Merge contact-level updates onto every open row of the contact.

    Returns {row_key: {column: value}} ready for SheetStore.update_rows(). Row-specific updates win
    over contact-level ones for the same column, and a row named in row_updates is always included.
    """
    row_updates = row_updates or {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if is_open(r) or r.row_key in row_updates:
            out[r.row_key] = {**contact_updates, **row_updates.get(r.row_key, {})}
    for key, u in row_updates.items():
        out.setdefault(key, dict(u))
    return out


def build_event_updates(event: ContactEvent, result: OutcomeResult, rows: Sequence[RowState], today: dt.date,
                        now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    """All sheet updates for one contact event, per row: release, then counters, then the outcome.

    A contact-level outcome (a promise, no reply, wrong contact...) changes every row in the event.
    An invoice-level one (paid, partial, dispute, invoice copy) changes the primary row only; the
    other invoices are marked covered and are due again tomorrow.
    """
    covered = covered_row_updates(today, now)
    by_key = {r.row_key: r for r in rows}
    per_row: dict[str, dict[str, Any]] = {}
    for key in event.row_keys:
        u: dict[str, Any] = dict(event.release_updates.get(key, {}))
        if key == event.primary_row_key or result.applies_to_all_rows:
            u.update(result.row_updates)
        else:
            u.update(covered)
        per_row[key] = u
    unknown = [k for k in event.row_keys if k not in by_key]
    if unknown:
        raise KeyError(f"event rows not in the contact's rows: {unknown}")
    return fan_out_updates(rows, result.contact_updates, per_row)


@dataclasses.dataclass
class ContactDecision:
    """A person's decision on a contact's pending extension, as updates for every row involved."""

    updates: dict[str, dict[str, Any]]  # {row_key: {column: value}} for SheetStore.update_rows
    row_keys: list[str]  # the rows that were waiting on this decision
    pending_date: dt.date
    approvals_used: int  # the contact's approval count after the decision
    audit_note: str


def _pending_rows(rows: Sequence[RowState], pending_date: dt.date | None) -> tuple[list[RowState], dt.date]:
    pending = [r for r in rows if r.approval_status is ApprovalStatus.PENDING and r.pending_approval_date]
    if not pending:
        raise ValueError("this contact has no pending extension")
    dates = sorted({r.pending_approval_date for r in pending})  # type: ignore[type-var]
    if pending_date is None:
        if len(dates) > 1:
            raise ValueError(f"this contact has pending requests for several dates {[x.isoformat() for x in dates]}; pass pending_date")
        pending_date = dates[0]
    chosen = [r for r in pending if r.pending_approval_date == pending_date]
    if not chosen:
        raise ValueError(f"no pending request for {pending_date.isoformat()}")
    return chosen, pending_date


def approve_contact_extension(rows: Sequence[RowState], today: dt.date, config: Config | None = None,
                              now: dt.datetime | None = None, *, pending_date: dt.date | None = None) -> ContactDecision:
    """Approve a contact's pending date. All rows waiting on that promise resume, and ONE approval is used.

    ``rows`` are all the rows of the contact. Prefer this over approve_extension(): a promise covering
    three invoices makes three rows pending, and approving them one by one would spend three approvals.
    """
    cfg = config or Config()
    now = now or dt.datetime.combine(today, dt.time())
    chosen, date = _pending_rows(rows, pending_date)
    if ContactState.from_rows(rows).approvals >= cfg.max_approvals:
        raise ValueError(f"this contact already used {cfg.max_approvals} approvals")
    return _approve(rows, chosen, date, today, cfg, now)


def _approve(rows: Sequence[RowState], chosen: Sequence[RowState], date: dt.date, today: dt.date, cfg: Config,
             now: dt.datetime) -> ContactDecision:
    """The one approval transition, shared by the app button and the sheet-edit reconcile."""
    used = ContactState.from_rows(rows).approvals
    resume = {Col.STATUS: Status.IN_PROGRESS, **_clear_human(), Col.APPROVAL_STATUS: ApprovalStatus.APPROVED,
              Col.PENDING_APPROVAL_DATE: "", Col.NEXT_FOLLOWUP_DATE: max(date, _next_day(today, 1)),
              Col.LAST_PROMISED_DATE: date, Col.LAST_UPDATED: _ts(now)}
    updates = fan_out_updates(rows, {Col.EXTENSION_APPROVALS_COUNT: used + 1}, {r.row_key: dict(resume) for r in chosen})
    return ContactDecision(updates, [r.row_key for r in chosen], date, used + 1,
                           f"Extension to {date.isoformat()} approved ({used + 1} of {cfg.max_approvals})")


def reject_contact_extension(rows: Sequence[RowState], today: dt.date, now: dt.datetime | None = None, *,
                             pending_date: dt.date | None = None) -> ContactDecision:
    """Reject a contact's pending date. The rows stay with a person, marked REJECTED, and no approval is used."""
    now = now or dt.datetime.combine(today, dt.time())
    chosen, date = _pending_rows(rows, pending_date)
    reject = {Col.APPROVAL_STATUS: ApprovalStatus.REJECTED, Col.LAST_UPDATED: _ts(now)}
    return ContactDecision({r.row_key: dict(reject) for r in chosen}, [r.row_key for r in chosen], date,
                           ContactState.from_rows(rows).approvals, f"Extension to {date.isoformat()} rejected")


# ---------------------------------------------------------------- decisions made by editing the sheet (BR-25)


@dataclasses.dataclass
class SyncAudit:
    """One Audit_Log entry to write for a change found in the sheet. The editor cannot be known, so actor is 'sheet edit'."""

    row_key: str
    contact_key: str
    status_before: Status
    status_after: Status
    note: str
    human_reason: HumanReason | None = None
    actor: str = "sheet edit"


@dataclasses.dataclass
class SyncIssue:
    code: str  # CONFLICT, CAP_REACHED, MISSING_DATE, IGNORED_APPROVAL, STALE_PENDING
    row_keys: list[str]
    message: str


@dataclasses.dataclass
class SyncResult:
    updates: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)  # for SheetStore.update_rows
    audits: list[SyncAudit] = dataclasses.field(default_factory=list)
    issues: list[SyncIssue] = dataclasses.field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.updates)


def reconcile_sheet_edits(rows: Sequence[RowState], today: dt.date, config: Config | None = None,
                          now: dt.datetime | None = None) -> SyncResult:
    """Finish decisions a person made by typing in the sheet, using the same transitions as the app buttons.

    How to decide in the sheet: on a row with status NEEDS_HUMAN and reason EXTENSION_APPROVAL_PENDING,
    change ``approval_status`` from PENDING to APPROVED or REJECTED (you may also edit
    ``pending_approval_date`` first to approve a different date). Edit any one row of the contact:
    all rows waiting on the same date follow. Run this at the start of every run and whenever the Human
    Queue refreshes. It is idempotent: a second pass over the result changes nothing.

    Also repairs stale leftovers: PENDING on a row a person moved out of NEEDS_HUMAN, and human_reason
    left behind on a row that is no longer with a person.
    """
    cfg = config or Config()
    now = now or dt.datetime.combine(today, dt.time())
    result = SyncResult()
    by_contact: dict[str, list[RowState]] = {}
    for r in rows:
        by_contact.setdefault(r.contact_key or r.row_key, []).append(r)
    for contact_rows in by_contact.values():
        _reconcile_contact(contact_rows, today, cfg, now, result)
    return result


def _reconcile_contact(rows: list[RowState], today: dt.date, cfg: Config, now: dt.datetime, out: SyncResult) -> None:
    pending_reason = HumanReason.EXTENSION_APPROVAL_PENDING
    touched: set[str] = set()

    def put(key: str, u: Mapping[str, Any]) -> None:
        out.updates.setdefault(key, {}).update(u)

    def audit(r: RowState, after: Status, note: str) -> None:
        out.audits.append(SyncAudit(r.row_key, r.contact_key, r.status, after, note, r.human_reason))

    used = ContactState.from_rows(rows).approvals
    waiting = [r for r in rows if r.status is Status.NEEDS_HUMAN and r.human_reason is pending_reason and r.pending_approval_date]
    for date in sorted({r.pending_approval_date for r in waiting}):  # type: ignore[type-var]
        group = [r for r in waiting if r.pending_approval_date == date]
        keys = [r.row_key for r in group]
        states = {r.approval_status for r in group}
        approved, rejected = ApprovalStatus.APPROVED in states, ApprovalStatus.REJECTED in states
        if approved and rejected:
            out.issues.append(SyncIssue("CONFLICT", keys, f"Rows for {date.isoformat()} are marked both APPROVED and REJECTED; fix one"))
            touched.update(keys)
        elif approved and used >= cfg.max_approvals:
            note = f"Approval ignored: this contact already used {cfg.max_approvals} approvals"
            for r in group:
                put(r.row_key, {Col.APPROVAL_STATUS: ApprovalStatus.REJECTED, Col.HUMAN_RESOLUTION_NOTE: note, Col.LAST_UPDATED: _ts(now)})
                audit(r, r.status, note)
            out.issues.append(SyncIssue("CAP_REACHED", keys, note))
            touched.update(keys)
        elif approved:
            decision = _approve(rows, group, date, today, cfg, now)
            for key, u in decision.updates.items():
                put(key, u)
            for r in group:
                audit(r, Status.IN_PROGRESS, decision.audit_note + " (edited in the sheet)")
            touched.update(keys)
        elif rejected and ApprovalStatus.PENDING in states:  # one row rejected by hand: the rest of the promise follows
            for r in group:
                if r.approval_status is ApprovalStatus.PENDING:
                    put(r.row_key, {Col.APPROVAL_STATUS: ApprovalStatus.REJECTED, Col.LAST_UPDATED: _ts(now)})
                    audit(r, r.status, f"Extension to {date.isoformat()} rejected (edited in the sheet)")
            touched.update(keys)

    for r in rows:
        if r.row_key in touched:
            continue
        if r.approval_status is ApprovalStatus.APPROVED and r.status is Status.NEEDS_HUMAN and r.human_reason is pending_reason and not r.pending_approval_date:
            put(r.row_key, {Col.APPROVAL_STATUS: ApprovalStatus.PENDING})
            out.issues.append(SyncIssue("MISSING_DATE", [r.row_key], "Approved without a pending_approval_date; enter the date, then approve again"))
        elif r.approval_status is ApprovalStatus.APPROVED and r.status is Status.NEEDS_HUMAN:
            put(r.row_key, {Col.APPROVAL_STATUS: ApprovalStatus.NONE})
            out.issues.append(SyncIssue("IGNORED_APPROVAL", [r.row_key], "APPROVED only applies to rows waiting for an extension approval"))
        elif r.approval_status is ApprovalStatus.PENDING and r.status is not Status.NEEDS_HUMAN:
            put(r.row_key, {Col.APPROVAL_STATUS: ApprovalStatus.NONE, Col.PENDING_APPROVAL_DATE: ""})
            out.issues.append(SyncIssue("STALE_PENDING", [r.row_key], "A person moved the row on, so the pending request was cleared"))
        if r.status in (Status.NEW, Status.IN_PROGRESS, Status.HANDLED) and r.human_reason is not None:
            put(r.row_key, _clear_human())


def resolve_row(row: RowState, today: dt.date, now: dt.datetime | None = None, *, close: bool = False,
                reset_attempts: bool = False, reenable_channels: bool = False, note: str = "",
                next_followup_date: dt.date | None = None) -> HumanActionResult:
    """A person finishes with a NEEDS_HUMAN row: close it (HANDLED) or send it back to IN_PROGRESS.

    reset_attempts zeroes the contact's attempt counters (history stays in Audit_Log).
    reenable_channels clears the disabled flags (use after fixing the email or phone).
    Rows with a data quality failure cannot be resolved here: fix the data and re-run.
    """
    now = now or dt.datetime.combine(today, dt.time())
    if row.status is Status.DQ_FAILED:
        raise ValueError(f"{row.row_key} failed data quality: fix the data in the sheet, then re-run")
    if row.status is not Status.NEEDS_HUMAN:
        raise ValueError(f"{row.row_key} is {row.status.value}, not NEEDS_HUMAN")
    u: dict[str, Any] = {
        Col.STATUS: Status.HANDLED if close else Status.IN_PROGRESS, **_clear_human(),
        Col.APPROVAL_STATUS: ApprovalStatus.NONE, Col.PENDING_APPROVAL_DATE: "",
        Col.NEXT_FOLLOWUP_DATE: "" if close or next_followup_date is None else next_followup_date,
        Col.LAST_UPDATED: _ts(now),
    }
    if note:
        u[Col.HUMAN_RESOLUTION_NOTE] = note
    c: dict[str, Any] = {}
    if reset_attempts:
        c.update({Col.EMAIL_ATTEMPTS: 0, Col.PHONE_ATTEMPTS: 0, Col.LEGACY_ATTEMPTS: 0, Col.CONTACT_ATTEMPTS: 0,
                  Col.CONSECUTIVE_SYSTEM_ERRORS: 0})
    if reenable_channels:
        c.update({Col.EMAIL_DISABLED: "", Col.PHONE_DISABLED: ""})
    return HumanActionResult(u, c, audit_note=note or ("Closed" if close else "Returned to follow-up"))


# ---------------------------------------------------------------- first-time setup of a row


def dq_refresh_updates(dq: Mapping[str, Any]) -> dict[str, Any]:
    """The data-quality columns for a row, from a check_quality() result row. Safe to write daily."""
    return {Col.DQ_STATUS: dq["dq_status"], Col.DQ_ISSUES: dq["dq_issues_text"],
            Col.CAN_CONTACT_EMAIL: bool(dq["can_contact_email"]), Col.CAN_CONTACT_PHONE: bool(dq["can_contact_phone"]),
            Col.CONTACT_KEY: dq["contact_key"]}


def initialize_row(row: Mapping[str, Any], dq: Mapping[str, Any], today: dt.date, config: Config | None = None,
                   now: dt.datetime | None = None) -> dict[str, Any]:
    """One-time migration of a row that has no status yet (BR-19, assumptions A-01, A-03, A-09).

    Sets counters from the legacy attempt suggestion, the starting status, a derived Ready for Contact
    when blank, and any follow-up date found in a legacy summary (honoured, not sent for approval).
    The Notes column and existing Outcome are left alone.
    """
    now = now or dt.datetime.combine(today, dt.time())
    u: dict[str, Any] = dq_refresh_updates(dq)
    e, p, leg = int(dq["suggested_email_attempts"]), int(dq["suggested_phone_attempts"]), int(dq["suggested_legacy_attempts"])
    u.update({
        Col.EMAIL_ATTEMPTS: e, Col.PHONE_ATTEMPTS: p, Col.LEGACY_ATTEMPTS: leg, Col.EMAIL_RESPONSES: 0, Col.PHONE_RESPONSES: 0,
        Col.EMAIL_DISABLED: "", Col.PHONE_DISABLED: "", Col.EXTENSION_APPROVALS_COUNT: 0, Col.CONSECUTIVE_SYSTEM_ERRORS: 0,
        Col.APPROVAL_STATUS: ApprovalStatus.NONE, Col.PENDING_APPROVAL_DATE: "",
        Col.LAST_CHANNEL: Channel.PHONE if p and not e else Channel.EMAIL if e and not p else "",
    })
    if not str(row.get(Col.READY_FOR_CONTACT, "") or "").strip():
        u[Col.READY_FOR_CONTACT] = dq["ready_for_contact_derived"]

    outcome = parse_outcome(str(row.get(Col.OUTCOME, "") or ""), strict=False)
    status, human = Status.IN_PROGRESS, ""
    if dq["dq_status"] == DqStatus.FAIL.value:
        status, human = Status.DQ_FAILED, _enum(HumanReason, dq.get("human_reason"), HumanReason.DQ)
        if outcome is None:
            u[Col.OUTCOME] = Outcome.DQ_FAILED
    elif dq.get("exclusion_reason") == "SETTLED":
        status = Status.HANDLED
    elif dq.get("exclusion_reason") == "READY_NO":
        status, human = Status.NEEDS_HUMAN, HumanReason.READY_NO
    elif dq.get("suggested_human_reason"):
        status, human = Status.NEEDS_HUMAN, HumanReason(dq["suggested_human_reason"])
    elif outcome in HUMAN_OUTCOME_REASONS:
        status, human = Status.NEEDS_HUMAN, HUMAN_OUTCOME_REASONS[outcome]
    u[Col.STATUS] = status
    u[Col.HUMAN_FLAG], u[Col.HUMAN_REASON] = ("Yes", human) if human else ("", "")

    # a date said in the past, found in the legacy summary
    if status is Status.IN_PROGRESS and outcome is not None and info(outcome).date_kind:
        said_on = _date(row.get(Col.LAST_CONTACT_DATE)) or _date(row.get(Col.LAST_UPDATED))
        found = extract_promised_date(str(row.get(Col.SUMMARY, "") or ""), said_on) if said_on else None
        if found and found > said_on:  # type: ignore[operator]
            u[Col.LAST_PROMISED_DATE] = found
            u[Col.NEXT_FOLLOWUP_DATE] = found
    u[Col.LAST_UPDATED] = _ts(now)
    return u
