"""Outcome taxonomy (docs/DESIGN.md section 3): codes, categories and what each one means.

Pure Python. The rules engine reads OutcomeInfo to decide status, retries and disabled channels;
the mock responder reads it to know which outcomes a channel can produce.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
from typing import Iterable

from core.schema import Channel, HumanReason, Status


class Category(str, enum.Enum):
    CONTACT_MADE = "Contact made"
    NO_CONTACT = "No contact"
    CHANNEL_ERROR = "Channel error"  # recipient side: bounce, busy line, bad number. Uses an attempt.
    SYSTEM_ERROR = "System error"  # our side: gateway or code failure. Does not use an attempt.
    ESCALATION = "Escalation"  # set by the app itself, never produced by a contact


class Outcome(str, enum.Enum):
    # Contact made
    PAID = "PAID"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PAYMENT_IN_PROCESS = "PAYMENT_IN_PROCESS"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    REQUESTS_INVOICE_COPY = "REQUESTS_INVOICE_COPY"
    SETTLEMENT_REQUESTED = "SETTLEMENT_REQUESTED"
    DISPUTE = "DISPUTE"
    WRONG_CONTACT = "WRONG_CONTACT"
    REFUSED_TO_PAY = "REFUSED_TO_PAY"
    # No contact
    NO_RESPONSE = "NO_RESPONSE"
    NO_ANSWER = "NO_ANSWER"
    LEFT_VOICEMAIL = "LEFT_VOICEMAIL"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    # Channel errors
    EMAIL_BOUNCE_SOFT = "EMAIL_BOUNCE_SOFT"
    EMAIL_BOUNCE_HARD = "EMAIL_BOUNCE_HARD"
    EMAIL_INVALID_ADDRESS = "EMAIL_INVALID_ADDRESS"
    PHONE_LINE_BUSY = "PHONE_LINE_BUSY"
    PHONE_INVALID_NUMBER = "PHONE_INVALID_NUMBER"
    PHONE_DISCONNECTED = "PHONE_DISCONNECTED"
    # System errors
    EMAIL_SEND_FAILURE = "EMAIL_SEND_FAILURE"
    PHONE_SYSTEM_ERROR = "PHONE_SYSTEM_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    # Escalation (set by the app)
    NEEDS_HUMAN_FOLLOW_UP = "NEEDS_HUMAN_FOLLOW_UP"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    DQ_FAILED = "DQ_FAILED"


BOTH = frozenset({Channel.EMAIL, Channel.PHONE})
EMAIL_ONLY = frozenset({Channel.EMAIL})
PHONE_ONLY = frozenset({Channel.PHONE})
NONE = frozenset()


@dataclasses.dataclass(frozen=True)
class OutcomeInfo:
    code: Outcome
    category: Category
    description: str
    next_action: str
    channels: frozenset[Channel]  # channels that can produce this outcome
    counts_as_attempt: bool
    is_real_response: bool
    status_effect: Status | None = None  # None = stay IN_PROGRESS unless another rule overrides
    human_reason: HumanReason | None = None
    disables_channels: frozenset[Channel] = NONE
    date_kind: str | None = None  # "payment", "callback" or "return": the contact may state a date
    exempt_from_approval: bool = False  # date needs no approval and uses none of the 3 (OUT_OF_OFFICE)
    followup_days: int | None = None  # None = use Config.default_retry_days (or the stated date)


def _info(code: Outcome, category: Category, description: str, next_action: str, channels: frozenset[Channel],
          counts: bool, real: bool, **kw) -> OutcomeInfo:
    return OutcomeInfo(code, category, description, next_action, channels, counts, real, **kw)


_CM, _NC, _CE, _SE, _ES = (Category.CONTACT_MADE, Category.NO_CONTACT, Category.CHANNEL_ERROR,
                           Category.SYSTEM_ERROR, Category.ESCALATION)

_REGISTRY: dict[Outcome, OutcomeInfo] = {i.code: i for i in [
    _info(Outcome.PAID, _CM, "Paid in full", "Set outstanding to 0 and mark HANDLED", BOTH, True, True,
          status_effect=Status.HANDLED),
    _info(Outcome.PARTIAL_PAYMENT, _CM, "Part of the balance was paid", "Reduce outstanding; follow up on the stated date for the rest",
          BOTH, True, True, date_kind="payment"),
    _info(Outcome.PROMISE_TO_PAY, _CM, "Contact states a payment date", "Apply the 5-day approval rule to the date",
          BOTH, True, True, date_kind="payment"),
    _info(Outcome.PAYMENT_IN_PROCESS, _CM, "Payment already initiated", "Confirm in 3 days", BOTH, True, True,
          followup_days=3),
    _info(Outcome.CALLBACK_REQUESTED, _CM, "Asked to be called back at a given time", "Call on the stated date",
          BOTH, True, True, date_kind="callback"),
    _info(Outcome.REQUESTS_INVOICE_COPY, _CM, "Needs the invoice document again", "Log the copy as sent; follow up in 2 days",
          BOTH, True, True, followup_days=2),
    _info(Outcome.SETTLEMENT_REQUESTED, _CM, "Asks for an instalment plan", "Escalate to a person",
          BOTH, True, True, status_effect=Status.NEEDS_HUMAN, human_reason=HumanReason.SETTLEMENT),
    _info(Outcome.DISPUTE, _CM, "Disputes the invoice", "Escalate to a person (account manager)",
          BOTH, True, True, status_effect=Status.NEEDS_HUMAN, human_reason=HumanReason.DISPUTE),
    _info(Outcome.WRONG_CONTACT, _CM, "Not the person responsible for payment", "Escalate; both channels off until the contact is fixed",
          BOTH, True, True, status_effect=Status.NEEDS_HUMAN, human_reason=HumanReason.WRONG_CONTACT,
          disables_channels=BOTH),
    _info(Outcome.REFUSED_TO_PAY, _CM, "Explicit refusal", "Escalate to a person",
          BOTH, True, True, status_effect=Status.NEEDS_HUMAN, human_reason=HumanReason.REFUSED),

    _info(Outcome.NO_RESPONSE, _NC, "Email sent, no reply", "Retry after the default gap", EMAIL_ONLY, True, False),
    _info(Outcome.NO_ANSWER, _NC, "Call not answered", "Retry after the default gap", PHONE_ONLY, True, False),
    _info(Outcome.LEFT_VOICEMAIL, _NC, "Voicemail message left", "Retry after the default gap", PHONE_ONLY, True, False),
    _info(Outcome.OUT_OF_OFFICE, _NC, "Auto-reply or colleague says the contact is away", "Follow up on the return date",
          BOTH, True, False, date_kind="return", exempt_from_approval=True),

    _info(Outcome.EMAIL_BOUNCE_SOFT, _CE, "Temporary delivery failure (mailbox full, server busy)", "Retry on the next run date",
          EMAIL_ONLY, True, False, followup_days=1),
    _info(Outcome.EMAIL_BOUNCE_HARD, _CE, "Permanent delivery failure", "Disable email; use phone",
          EMAIL_ONLY, True, False, disables_channels=EMAIL_ONLY, followup_days=1),
    _info(Outcome.EMAIL_INVALID_ADDRESS, _CE, "Address rejected as invalid", "Disable email; use phone",
          EMAIL_ONLY, True, False, disables_channels=EMAIL_ONLY, followup_days=1),
    _info(Outcome.PHONE_LINE_BUSY, _CE, "Line busy", "Retry on the next run date", PHONE_ONLY, True, False, followup_days=1),
    _info(Outcome.PHONE_INVALID_NUMBER, _CE, "Number not in service or invalid", "Disable phone",
          PHONE_ONLY, True, False, disables_channels=PHONE_ONLY, followup_days=1),
    _info(Outcome.PHONE_DISCONNECTED, _CE, "Number disconnected", "Disable phone",
          PHONE_ONLY, True, False, disables_channels=PHONE_ONLY, followup_days=1),

    _info(Outcome.EMAIL_SEND_FAILURE, _SE, "Our email gateway failed", "Retry on the next run date; no attempt used",
          EMAIL_ONLY, False, False, followup_days=1),
    _info(Outcome.PHONE_SYSTEM_ERROR, _SE, "Our telephony gateway failed", "Retry on the next run date; no attempt used",
          PHONE_ONLY, False, False, followup_days=1),
    _info(Outcome.SYSTEM_ERROR, _SE, "Unexpected error while processing the row", "Retry on the next run date; no attempt used",
          BOTH, False, False, followup_days=1),

    _info(Outcome.NEEDS_HUMAN_FOLLOW_UP, _ES, "App escalated the row to a person", "Show in the human queue", NONE, False, False,
          status_effect=Status.NEEDS_HUMAN),
    _info(Outcome.MAX_ATTEMPTS_REACHED, _ES, "Both channels used up or disabled", "Show in the human queue", NONE, False, False,
          status_effect=Status.NEEDS_HUMAN, human_reason=HumanReason.ATTEMPT_CAP),
    _info(Outcome.DQ_FAILED, _ES, "Row failed a blocking data quality rule", "Show in the data quality queue", NONE, False, False,
          status_effect=Status.DQ_FAILED, human_reason=HumanReason.DQ),
]}

# Sanity: every enum member has a registry entry (checked at import time, so a typo fails fast).
assert set(_REGISTRY) == set(Outcome), "taxonomy registry is out of sync with the Outcome enum"


# ---------------------------------------------------------------- lookups


def info(code: Outcome | str) -> OutcomeInfo:
    return _REGISTRY[Outcome(code)]


def category_of(code: Outcome | str) -> Category:
    return info(code).category


def is_real_response(code: Outcome | str) -> bool:
    """True for outcomes where a person engaged (the Contact made category)."""
    return info(code).is_real_response


def is_system_error(code: Outcome | str) -> bool:
    return info(code).category is Category.SYSTEM_ERROR


def is_channel_error(code: Outcome | str) -> bool:
    return info(code).category is Category.CHANNEL_ERROR


def counts_as_attempt(code: Outcome | str) -> bool:
    return info(code).counts_as_attempt


def outcomes_in(category: Category) -> list[Outcome]:
    return [o for o, i in _REGISTRY.items() if i.category is category]


def outcomes_for_channel(channel: Channel | str) -> list[Outcome]:
    """Outcomes a contact event on this channel can produce (escalations are never produced)."""
    ch = Channel(channel)
    return [o for o, i in _REGISTRY.items() if ch in i.channels]


def parse_outcome(text: str | None, *, strict: bool = True) -> Outcome | None:
    """Parse an Outcome cell. Blank gives None. An unknown value raises ValueError (or None if not strict)."""
    value = (text or "").strip().upper().replace(" ", "_")
    if not value:
        return None
    try:
        return Outcome(value)
    except ValueError:
        if strict:
            raise ValueError(f"Unknown outcome {text!r}. Known: {', '.join(o.value for o in Outcome)}") from None
        return None


def taxonomy_table(codes: Iterable[Outcome] | None = None) -> list[dict[str, str]]:
    """Rows for the About page and docs: one dict per outcome, in category order."""
    rows = []
    for o in (codes or Outcome):
        i = info(o)
        rows.append({
            "code": i.code.value,
            "category": i.category.value,
            "description": i.description,
            "channels": ", ".join(sorted(c.value for c in i.channels)) or "app only",
            "counts_as_attempt": "yes" if i.counts_as_attempt else "no",
            "real_response": "yes" if i.is_real_response else "no",
            "next_action": i.next_action,
        })
    return rows


# ---------------------------------------------------------------- stated dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MON = (r"(january|february|march|april|may|june|july|august|september|october|november|december"
        r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b\.?")
_NUMBER_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "fourteen": 14}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MON = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}(?:,?\s+(\d{{4}}))?", re.I)
_MON_DAY = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_IN_N = re.compile(r"\bin\s+(\d{1,3}|a|one|two|three|four|five|six|seven|eight|nine|ten|fourteen)\s+(day|week)s?\b", re.I)


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def extract_promised_date(text: str | None, ref_date: dt.date) -> dt.date | None:
    """Find the date a contact stated in free text, relative to ref_date (when it was said).

    Understands "2026-08-31", "31 Aug 2026", "30 June", "Aug 15", "in 7 days", "in two weeks",
    "tomorrow" and "next week". A date without a year gets ref_date's year, or the next year if
    that would be earlier than ref_date. Returns the first date found, or None.

    It only finds a date; it does not know whether the date is a promise. Callers should use it
    for PROMISE_TO_PAY, PARTIAL_PAYMENT, CALLBACK_REQUESTED and OUT_OF_OFFICE text only (a summary
    like "Called 4 Aug, no answer" also contains a date, but it is the call date).
    """
    if not text:
        return None

    def resolve(year: str | None, month: int, day: int) -> dt.date | None:
        if year:
            return _safe_date(int(year), month, day)
        candidate = _safe_date(ref_date.year, month, day)
        if candidate is not None and candidate < ref_date:
            candidate = _safe_date(ref_date.year + 1, month, day)
        return candidate

    m = _ISO.search(text)
    if m and (d := _safe_date(int(m[1]), int(m[2]), int(m[3]))):
        return d
    m = _DAY_MON.search(text)
    if m and (d := resolve(m[3], _MONTHS[m[2].lower()[:3]], int(m[1]))):
        return d
    m = _MON_DAY.search(text)
    if m and (d := resolve(m[3], _MONTHS[m[1].lower()[:3]], int(m[2]))):
        return d
    m = _IN_N.search(text)
    if m:
        n = int(m[1]) if m[1].isdigit() else _NUMBER_WORDS[m[1].lower()]
        return ref_date + dt.timedelta(days=n * (7 if m[2].lower() == "week" else 1))
    lowered = text.lower()
    if "tomorrow" in lowered:
        return ref_date + dt.timedelta(days=1)
    if "next week" in lowered:
        return ref_date + dt.timedelta(days=7)
    return None


def promised_days(text: str | None, ref_date: dt.date) -> int | None:
    """Whole days from ref_date to the date stated in text, or None."""
    d = extract_promised_date(text, ref_date)
    return None if d is None else (d - ref_date).days
