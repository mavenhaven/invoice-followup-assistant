"""What the Human Queue page shows, built from a fresh read of the sheet. No Streamlit here."""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pandas as pd

from core.quality import Severity, check_quality
from core.rules import RowState
from core.schema import ApprovalStatus, Col, Config, HumanReason, Status

HUMAN_REASON_LABELS = {
    HumanReason.EXTENSION_APPROVAL_PENDING.value: "Payment date needs approval",
    HumanReason.EXTENSION_LIMIT_REACHED.value: "Asked for a 4th extension (limit reached)",
    HumanReason.ATTEMPT_CAP.value: "5 email and 5 phone attempts, no response",
    HumanReason.ATTEMPT_CAP_LEGACY.value: "5 earlier attempts, no response",
    HumanReason.NO_CHANNEL_LEFT.value: "No usable email or phone left",
    HumanReason.DISPUTE.value: "Invoice disputed",
    HumanReason.SETTLEMENT.value: "Asked for an instalment plan",
    HumanReason.WRONG_CONTACT.value: "Wrong contact person",
    HumanReason.REFUSED.value: "Refused to pay",
    HumanReason.READY_NO.value: "Marked not ready for contact",
    HumanReason.SYSTEM_ERRORS_REPEATED.value: "Repeated system errors",
    HumanReason.DQ.value: "Data quality problem",
    HumanReason.NOT_AN_INVOICE.value: "Not an invoice (credit note)",
}

HUMAN_COLUMNS = ["row_key", "invoice_number", "customer", "country", "contact_name", "outstanding", "due_date",
                 "reason", "human_reason", "approval_status", "last_outcome", "last_summary", "email_attempts",
                 "phone_attempts", "last_contact_date", "notes", "human_resolution_note", "contact_key"]


def reason_label(code: str, approval_status: str = "") -> str:
    label = HUMAN_REASON_LABELS.get(code, code.replace("_", " ").capitalize() if code else "")
    if code == HumanReason.EXTENSION_APPROVAL_PENDING.value and approval_status == ApprovalStatus.REJECTED.value:
        return "Payment date rejected: decide what happens next"
    return label


def fmt_money(amount: Any, currency: str) -> str:
    try:
        return f"{float(amount):,.2f} {currency}".strip()
    except (TypeError, ValueError):
        return f"{amount} {currency}".strip()


@dataclasses.dataclass
class PendingApproval:
    contact_key: str
    customer: str
    contact_name: str
    pending_date: dt.date
    days_out: int
    approvals_used: int
    max_approvals: int
    invoices: list[dict[str, str]]  # row_key, invoice_number, outstanding, due_date
    last_reply: str
    asked_on: str

    @property
    def can_approve(self) -> bool:
        return self.approvals_used < self.max_approvals


@dataclasses.dataclass
class QueueView:
    pending: list[PendingApproval]
    human: pd.DataFrame  # rows with a person, apart from the pending decisions
    dq: pd.DataFrame  # rows that failed data quality
    counts: dict[str, int]


def _last_reply(audit: pd.DataFrame, row_keys: list[str]) -> tuple[str, str]:
    """The most recent real response text for any of these rows, and the date it arrived."""
    if audit is None or audit.empty:
        return "", ""
    rows = audit[audit["row_key"].isin(row_keys) & (audit["contact_event_id"] != "") & (audit["response_text"] != "")]
    rows = rows[~rows["response_text"].str.startswith("Covered by")]
    if rows.empty:
        return "", ""
    last = rows.sort_values(["timestamp", "event_id"]).iloc[-1]
    return str(last["response_text"]), str(last["run_date"])


def build_queue(df: pd.DataFrame, audit: pd.DataFrame | None, today: dt.date, config: Config | None = None) -> QueueView:
    cfg = config or Config()
    records = df.to_dict("records")
    states = [RowState.from_row(r) for r in records]

    # --- decisions waiting: one card per contact and requested date
    groups: dict[tuple[str, dt.date], list[tuple[dict, RowState]]] = {}
    for rec, s in zip(records, states):
        if (s.status is Status.NEEDS_HUMAN and s.human_reason is HumanReason.EXTENSION_APPROVAL_PENDING
                and s.approval_status is ApprovalStatus.PENDING and s.pending_approval_date):
            groups.setdefault((s.contact_key or s.row_key, s.pending_approval_date), []).append((rec, s))
    contact_approvals: dict[str, int] = {}
    for s in states:
        contact_approvals[s.contact_key] = max(contact_approvals.get(s.contact_key, 0), s.approvals)
    pending: list[PendingApproval] = []
    for (contact_key, date), members in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        keys = [s.row_key for _, s in members]
        text, asked = _last_reply(audit, keys)
        first = members[0][0]
        pending.append(PendingApproval(
            contact_key=contact_key, customer=str(first.get(Col.CUSTOMER, "")), contact_name=str(first.get(Col.CONTACT_NAME, "")),
            pending_date=date, days_out=(date - today).days, approvals_used=contact_approvals.get(contact_key, 0),
            max_approvals=cfg.max_approvals,
            invoices=[{"row_key": s.row_key, "invoice_number": str(rec.get(Col.INVOICE_NUMBER, "")),
                       "outstanding": fmt_money(s.amount_outstanding or 0, str(rec.get(Col.CURRENCY, ""))),
                       "due_date": str(rec.get(Col.DUE_DATE, ""))} for rec, s in members],
            last_reply=text, asked_on=asked))

    # --- rows with a person
    human_rows = []
    for rec, s in zip(records, states):
        waiting_decision = s.approval_status is ApprovalStatus.PENDING and s.human_reason is HumanReason.EXTENSION_APPROVAL_PENDING
        if s.status is Status.NEEDS_HUMAN and not waiting_decision:
            code = s.human_reason.value if s.human_reason else ""
            human_rows.append({
                "row_key": s.row_key, "invoice_number": rec.get(Col.INVOICE_NUMBER, ""), "customer": rec.get(Col.CUSTOMER, ""),
                "country": rec.get(Col.COUNTRY, ""), "contact_name": rec.get(Col.CONTACT_NAME, ""),
                "outstanding": fmt_money(s.amount_outstanding or 0, str(rec.get(Col.CURRENCY, ""))),
                "due_date": rec.get(Col.DUE_DATE, ""), "reason": reason_label(code, s.approval_status.value),
                "human_reason": code, "approval_status": s.approval_status.value,
                "last_outcome": rec.get(Col.OUTCOME, ""), "last_summary": rec.get(Col.SUMMARY, ""),
                "email_attempts": s.email_attempts, "phone_attempts": s.phone_attempts,
                "last_contact_date": rec.get(Col.LAST_CONTACT_DATE, ""), "notes": rec.get(Col.NOTES, ""),
                "human_resolution_note": rec.get(Col.HUMAN_RESOLUTION_NOTE, ""), "contact_key": s.contact_key})
    human = pd.DataFrame(human_rows, columns=HUMAN_COLUMNS)
    if not human.empty:
        human = human.sort_values(["reason", "due_date", "row_key"]).reset_index(drop=True)

    # --- data quality failures, with the reason in words
    dq_rows = []
    quality = check_quality(df, run_date=today, config=cfg).set_index("row_key") if len(df) else None
    for rec, s in zip(records, states):
        if s.status is Status.DQ_FAILED:
            issues = quality.loc[s.row_key, "dq_issues"] if quality is not None and s.row_key in quality.index else []
            blocking = "; ".join(i.message for i in issues if i.severity is Severity.BLOCK) or str(rec.get(Col.DQ_ISSUES, ""))
            dq_rows.append({"row_key": s.row_key, "invoice_number": rec.get(Col.INVOICE_NUMBER, ""), "customer": rec.get(Col.CUSTOMER, ""),
                            "country": rec.get(Col.COUNTRY, ""), "contact_name": rec.get(Col.CONTACT_NAME, ""),
                            "problem": blocking, "what_to_do": reason_label(s.human_reason.value if s.human_reason else "DQ"),
                            "notes": rec.get(Col.NOTES, "")})
    dq = pd.DataFrame(dq_rows, columns=["row_key", "invoice_number", "customer", "country", "contact_name", "problem", "what_to_do", "notes"])

    counts = {"pending_contacts": len(pending), "pending_rows": sum(len(p.invoices) for p in pending),
              "human": len(human), "dq": len(dq)}
    return QueueView(pending, human, dq, counts)
