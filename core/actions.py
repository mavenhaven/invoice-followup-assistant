"""Human actions on the sheet: approve or reject a date, resolve a row, sync typed-in decisions.

Every action reads the sheet fresh, refuses while a run is in progress, uses the same decision
functions as the daily run (core.rules), writes in one batch, and records who did it in Audit_Log.
No Streamlit here, so the pages stay thin and these can be tested directly.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Sequence

from core.clock import now_in
from core.rules import (
    RowState, SyncResult, approve_contact_extension, fan_out_updates, reconcile_sheet_edits, reject_contact_extension,
    resolve_row,
)
from core.schema import AuditEntry, Config, Status
from core.sheets import SheetStore


class ActionError(Exception):
    """The action was refused. The message is meant to be shown to the person."""


def running_run(store: SheetStore, now: dt.datetime, stale_after_minutes: int = 30) -> str | None:
    """run_id of a run that is RUNNING right now (any date, not stale), or None."""
    log = store.read_run_log()
    if log.empty:
        return None
    for _, row in log[log["state"] == "RUNNING"].iterrows():
        try:
            started = dt.datetime.strptime(str(row["started_at"]).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if now - started < dt.timedelta(minutes=stale_after_minutes):
            return str(row["run_id"])
    return None


def clean_actor(actor: str) -> str:
    """A name safe to store in the audit log: one line, no control characters, at most 80 characters."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(actor or "")).split())[:80]


def _fresh(store: SheetStore, now: dt.datetime, actor: str) -> list[RowState]:
    if not clean_actor(actor):
        raise ActionError("Enter your name first, so the decision can be recorded.")
    active = running_run(store, now)
    if active:
        raise ActionError(f"A run is in progress ({active}). Try again when it has finished.")
    return [RowState.from_row(rec) for rec in store.read_invoices().to_dict("records")]


def _audit(rows: Sequence[RowState], after: dict[str, Status], note: str, actor: str, today: dt.date, now: dt.datetime) -> list[AuditEntry]:
    return [AuditEntry(event_id=f"ui-{now:%Y%m%d%H%M%S}-{i:03d}", run_id="", row_key=s.row_key, timestamp=now, run_date=today,
                       contact_key=s.contact_key, status_before=s.status, status_after=after.get(s.row_key, s.status),
                       human_reason=s.human_reason or "", response_text=note, actor=clean_actor(actor))
            for i, s in enumerate(rows, start=1)]


def decide_extension(store: SheetStore, contact_key: str, *, approve: bool, actor: str, today: dt.date,
                     now: dt.datetime | None = None, pending_date: dt.date | None = None,
                     config: Config | None = None) -> str:
    """Approve or reject a contact's pending payment date. Returns a message to show."""
    cfg = config or store.load_config()
    now = now or now_in(cfg.timezone)
    states = _fresh(store, now, actor)
    contact_rows = [s for s in states if (s.contact_key or s.row_key) == contact_key]
    if not contact_rows:
        raise ActionError("That contact is no longer in the sheet. Refresh the page.")
    try:
        decision = (approve_contact_extension(contact_rows, today, cfg, now, pending_date=pending_date) if approve
                    else reject_contact_extension(contact_rows, today, now, pending_date=pending_date))
    except ValueError as exc:
        raise ActionError(f"Nothing was changed: {exc}. It may already have been decided; refresh the page.") from exc
    store.update_rows(decision.updates)
    waiting = [s for s in contact_rows if s.row_key in decision.row_keys]
    after = {k: (Status.IN_PROGRESS if approve else Status.NEEDS_HUMAN) for k in decision.row_keys}
    store.append_audit(_audit(waiting, after, decision.audit_note, actor, today, now))
    count = len(decision.row_keys)
    return f"{decision.audit_note}. {count} invoice{'s' if count != 1 else ''} updated."


def resolve_row_action(store: SheetStore, row_key: str, *, actor: str, today: dt.date, now: dt.datetime | None = None,
                       close: bool = False, reset_attempts: bool = False, reenable_channels: bool = False,
                       note: str = "", next_followup_date: dt.date | None = None) -> str:
    """Finish with a row that is with a person: close it, or send it back to follow-up."""
    now = now or now_in(store.load_config().timezone)
    states = _fresh(store, now, actor)
    row = next((s for s in states if s.row_key == row_key), None)
    if row is None:
        raise ActionError("That row is no longer in the sheet. Refresh the page.")
    contact_rows = [s for s in states if row.contact_key and s.contact_key == row.contact_key] or [row]
    try:
        result = resolve_row(row, today, now, close=close, reset_attempts=reset_attempts,
                             reenable_channels=reenable_channels, note=note, next_followup_date=next_followup_date)
    except ValueError as exc:
        raise ActionError(f"Nothing was changed: {exc}.") from exc
    store.update_rows(fan_out_updates(contact_rows, result.contact_updates, {row_key: result.row_updates}))
    after = {row_key: Status.HANDLED if close else Status.IN_PROGRESS}
    store.append_audit(_audit([row], after, result.audit_note, actor, today, now))
    return f"{row.row_key}: {'closed' if close else 'returned to follow-up'}."


def sync_sheet_edits(store: SheetStore, today: dt.date, now: dt.datetime | None = None,
                     config: Config | None = None) -> SyncResult:
    """Finish decisions people typed straight into the sheet (BR-25). Safe to repeat."""
    cfg = config or store.load_config()
    now = now or now_in(cfg.timezone)
    states = _fresh(store, now, "sync")
    result = reconcile_sheet_edits(states, today, cfg, now)
    if result.updates:
        store.update_rows(result.updates)
    if result.audits:
        by_key = {s.row_key: s for s in states}
        store.append_audit([AuditEntry(event_id=f"ui-{now:%Y%m%d%H%M%S}-{i:03d}", run_id="", row_key=a.row_key, timestamp=now,
                                       run_date=today, contact_key=a.contact_key, status_before=a.status_before,
                                       status_after=a.status_after, human_reason=a.human_reason or "", response_text=a.note,
                                       actor=a.actor) for i, a in enumerate(result.audits, start=1) if a.row_key in by_key])
    return result

