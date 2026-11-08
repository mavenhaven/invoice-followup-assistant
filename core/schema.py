"""Sheet layout, enums, and row dataclasses (see docs/DESIGN.md sections 2 and 3).

No Streamlit or Google imports here, so everything is trivially testable.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
from decimal import Decimal
from typing import Any, Mapping


class Col:
    """Column names of the Invoices sheet. Existing columns first, then appended ones."""

    # Existing columns (never renamed or reordered)
    INVOICE_NUMBER = "Invoice Number"
    CUSTOMER = "Customer"
    COUNTRY = "Country"
    CONTACT_NAME = "Contact Name"
    EMAIL = "Email"
    PHONE = "Phone"
    INVOICE_DATE = "Invoice Date"
    DUE_DATE = "Due Date"
    INVOICE_AMOUNT = "Invoice Amount"
    AMOUNT_OUTSTANDING = "Amount Outstanding"
    CURRENCY = "Currency"
    LAST_CONTACT_DATE = "Last Contact Date"
    CONTACT_ATTEMPTS = "Contact Attempts"
    READY_FOR_CONTACT = "Ready for Contact"
    OUTCOME = "Outcome"
    SUMMARY = "Summary"
    LAST_UPDATED = "Last Updated"
    NOTES = "Notes"

    # Appended columns
    ROW_KEY = "row_key"
    CONTACT_KEY = "contact_key"
    EMAIL_ATTEMPTS = "email_attempts"
    PHONE_ATTEMPTS = "phone_attempts"
    LEGACY_ATTEMPTS = "legacy_attempts"
    EMAIL_RESPONSES = "email_responses"
    PHONE_RESPONSES = "phone_responses"
    LAST_CHANNEL = "last_channel"
    NEXT_FOLLOWUP_DATE = "next_followup_date"
    LAST_PROMISED_DATE = "last_promised_date"
    STATUS = "status"
    DQ_STATUS = "dq_status"
    DQ_ISSUES = "dq_issues"
    CAN_CONTACT_EMAIL = "can_contact_email"
    CAN_CONTACT_PHONE = "can_contact_phone"
    HUMAN_FLAG = "human_flag"
    HUMAN_REASON = "human_reason"
    APPROVAL_STATUS = "approval_status"
    PENDING_APPROVAL_DATE = "pending_approval_date"
    EXTENSION_APPROVALS_COUNT = "extension_approvals_count"
    CONSECUTIVE_SYSTEM_ERRORS = "consecutive_system_errors"
    LAST_PROCESSED_DATE = "last_processed_date"
    HUMAN_RESOLUTION_NOTE = "human_resolution_note"
    EMAIL_DISABLED = "email_disabled"  # "Yes" once a bounce or bad address switched email off
    PHONE_DISABLED = "phone_disabled"


INVOICES_EXISTING_COLUMNS: tuple[str, ...] = (
    Col.INVOICE_NUMBER, Col.CUSTOMER, Col.COUNTRY, Col.CONTACT_NAME, Col.EMAIL, Col.PHONE,
    Col.INVOICE_DATE, Col.DUE_DATE, Col.INVOICE_AMOUNT, Col.AMOUNT_OUTSTANDING, Col.CURRENCY,
    Col.LAST_CONTACT_DATE, Col.CONTACT_ATTEMPTS, Col.READY_FOR_CONTACT, Col.OUTCOME,
    Col.SUMMARY, Col.LAST_UPDATED, Col.NOTES,
)

INVOICES_NEW_COLUMNS: tuple[str, ...] = (
    Col.ROW_KEY, Col.CONTACT_KEY, Col.EMAIL_ATTEMPTS, Col.PHONE_ATTEMPTS, Col.LEGACY_ATTEMPTS,
    Col.EMAIL_RESPONSES, Col.PHONE_RESPONSES, Col.LAST_CHANNEL, Col.NEXT_FOLLOWUP_DATE,
    Col.LAST_PROMISED_DATE, Col.STATUS, Col.DQ_STATUS, Col.DQ_ISSUES, Col.CAN_CONTACT_EMAIL,
    Col.CAN_CONTACT_PHONE, Col.HUMAN_FLAG, Col.HUMAN_REASON, Col.APPROVAL_STATUS,
    Col.PENDING_APPROVAL_DATE, Col.EXTENSION_APPROVALS_COUNT, Col.CONSECUTIVE_SYSTEM_ERRORS,
    Col.LAST_PROCESSED_DATE, Col.HUMAN_RESOLUTION_NOTE, Col.EMAIL_DISABLED, Col.PHONE_DISABLED,
)

INVOICES_COLUMNS: tuple[str, ...] = INVOICES_EXISTING_COLUMNS + INVOICES_NEW_COLUMNS

# Columns the app must never write (BR-21: Notes is for humans only).
PROTECTED_COLUMNS: frozenset[str] = frozenset({Col.NOTES})

# Worksheet names
AUDIT_SHEET = "Audit_Log"
RUN_SHEET = "Run_Log"
CONFIG_SHEET = "Config"

AUDIT_COLUMNS: tuple[str, ...] = (
    "event_id", "run_id", "timestamp", "run_date", "row_key", "contact_key", "contact_event_id",
    "channel", "attempt_no_for_channel", "counted_as_attempt", "outcome", "response_text",
    "promised_date", "status_before", "status_after", "human_reason", "actor",
)

RUN_COLUMNS: tuple[str, ...] = (
    "run_id", "run_date", "started_at", "finished_at", "state", "rows_total", "rows_processed",
    "rows_skipped_already_processed", "rows_skipped_not_due", "rows_dq_failed",
    "rows_escalated", "attempts_email", "attempts_phone", "errors", "started_by",
)

CONFIG_COLUMNS: tuple[str, ...] = ("key", "value")


class Status(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    DQ_FAILED = "DQ_FAILED"
    HANDLED = "HANDLED"


class DqStatus(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ApprovalStatus(str, enum.Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Channel(str, enum.Enum):
    EMAIL = "email"
    PHONE = "phone"


class RunState(str, enum.Enum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class HumanReason(str, enum.Enum):
    DQ = "DQ"
    READY_NO = "READY_NO"
    ATTEMPT_CAP = "ATTEMPT_CAP"
    ATTEMPT_CAP_LEGACY = "ATTEMPT_CAP_LEGACY"
    NO_CHANNEL_LEFT = "NO_CHANNEL_LEFT"
    EXTENSION_APPROVAL_PENDING = "EXTENSION_APPROVAL_PENDING"
    EXTENSION_LIMIT_REACHED = "EXTENSION_LIMIT_REACHED"
    DISPUTE = "DISPUTE"
    SETTLEMENT = "SETTLEMENT"
    WRONG_CONTACT = "WRONG_CONTACT"
    REFUSED = "REFUSED"
    SYSTEM_ERRORS_REPEATED = "SYSTEM_ERRORS_REPEATED"
    NOT_AN_INVOICE = "NOT_AN_INVOICE"


def to_cell(value: Any) -> Any:
    """Convert a Python value to something safe to write into a sheet cell."""
    if value is None:
        return ""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


@dataclasses.dataclass
class AuditEntry:
    """One row of Audit_Log (append only)."""

    event_id: str
    run_id: str
    row_key: str
    timestamp: dt.datetime | str = ""
    run_date: dt.date | str = ""
    contact_key: str = ""
    contact_event_id: str = ""
    channel: Channel | str = ""
    attempt_no_for_channel: int | str = ""
    counted_as_attempt: bool | str = ""
    outcome: str = ""
    response_text: str = ""
    promised_date: dt.date | str = ""
    status_before: Status | str = ""
    status_after: Status | str = ""
    human_reason: HumanReason | str = ""
    actor: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return {k: to_cell(v) for k, v in dataclasses.asdict(self).items()}


@dataclasses.dataclass
class RunLogEntry:
    """One row of Run_Log (one per run; updated when the run finishes)."""

    run_id: str
    run_date: dt.date | str
    started_at: dt.datetime | str = ""
    finished_at: dt.datetime | str = ""
    state: RunState | str = RunState.RUNNING
    rows_total: int = 0
    rows_processed: int = 0
    rows_skipped_already_processed: int = 0
    rows_skipped_not_due: int = 0
    rows_dq_failed: int = 0
    rows_escalated: int = 0
    attempts_email: int = 0
    attempts_phone: int = 0
    errors: list[Any] = dataclasses.field(default_factory=list)
    started_by: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return {k: to_cell(v) for k, v in dataclasses.asdict(self).items()}


@dataclasses.dataclass
class Config:
    """Typed view of the Config worksheet (key / value rows)."""

    attempt_cap: int = 5
    promise_max_days: int = 5
    max_approvals: int = 3
    default_retry_days: int = 2
    system_error_limit: int = 3
    invoice_prefix: str = "INV-"
    timezone: str = "Europe/Berlin"
    skip_below_minimum: bool = False
    legacy_migration_done: bool = False
    min_amount: dict[str, float] = dataclasses.field(
        default_factory=lambda: {"EUR": 10.0, "CZK": 250.0, "HUF": 4000.0, "RON": 50.0}
    )

    @classmethod
    def default_rows(cls) -> dict[str, str]:
        """Config worksheet rows as strings, in the order they are seeded."""
        c = cls()
        rows: dict[str, str] = {
            "attempt_cap": str(c.attempt_cap),
            "promise_max_days": str(c.promise_max_days),
            "max_approvals": str(c.max_approvals),
            "default_retry_days": str(c.default_retry_days),
            "system_error_limit": str(c.system_error_limit),
            "invoice_prefix": c.invoice_prefix,
            "timezone": c.timezone,
            "skip_below_minimum": "false",
            "legacy_migration_done": "false",
        }
        for cur, amount in c.min_amount.items():
            rows[f"min_amount_{cur}"] = f"{amount:g}"
        return rows

    @classmethod
    def from_mapping(cls, raw: Mapping[str, str]) -> "Config":
        """Parse worksheet values. Missing keys fall back to defaults; bad values raise ValueError."""
        defaults = cls()

        def as_int(key: str, default: int) -> int:
            text = str(raw.get(key, "")).strip()
            if text == "":
                return default
            try:
                return int(text)
            except ValueError:
                raise ValueError(f"Config key {key!r} must be a whole number, got {text!r}") from None

        def as_bool(key: str, default: bool) -> bool:
            text = str(raw.get(key, "")).strip().lower()
            if text == "":
                return default
            if text in ("true", "yes", "1"):
                return True
            if text in ("false", "no", "0"):
                return False
            raise ValueError(f"Config key {key!r} must be true or false, got {text!r}")

        min_amount = dict(defaults.min_amount)
        for key, value in raw.items():
            if key.startswith("min_amount_") and str(value).strip() != "":
                try:
                    min_amount[key[len("min_amount_"):].upper()] = float(str(value).replace(",", "."))
                except ValueError:
                    raise ValueError(f"Config key {key!r} must be a number, got {value!r}") from None

        return cls(
            attempt_cap=as_int("attempt_cap", defaults.attempt_cap),
            promise_max_days=as_int("promise_max_days", defaults.promise_max_days),
            max_approvals=as_int("max_approvals", defaults.max_approvals),
            default_retry_days=as_int("default_retry_days", defaults.default_retry_days),
            system_error_limit=as_int("system_error_limit", defaults.system_error_limit),
            invoice_prefix=str(raw.get("invoice_prefix", defaults.invoice_prefix)) or defaults.invoice_prefix,
            timezone=str(raw.get("timezone", defaults.timezone)) or defaults.timezone,
            skip_below_minimum=as_bool("skip_below_minimum", defaults.skip_below_minimum),
            legacy_migration_done=as_bool("legacy_migration_done", defaults.legacy_migration_done),
            min_amount=min_amount,
        )
