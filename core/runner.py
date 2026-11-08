"""The daily run: ties the sheet, data quality, rules engine and mock responder together.

    result = run_daily(store, today=date.today(), run_id="run-20260820-0900", rng=random.Random(1))

Steps (each writes in batches, never row by row):
  0. ensure the sheet layout, take the run lock (BR-24), log a RUNNING row in Run_Log
  1. read the invoices once and run the data quality checks; set up rows that have no status yet,
     refresh stored data quality columns that changed
  2. finish decisions people made by editing the sheet (BR-25)
  3. per contact: plan one combined contact (BR-06), simulate the response, apply the outcome,
     write the row updates and Audit_Log entries. A failure for one contact is logged and recorded
     as a SYSTEM_ERROR for that contact; the run carries on (BR-14).
  4. write the Run_Log summary and check the four buckets add up to the row count

Running it twice on one date changes nothing the second time (BR-07): every row decided on a date
is stamped with last_processed_date.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from core.clock import now_in
from core.mock_responses import ContactInfo, InvoiceInfo, MockResponse, generate_response
from core.quality import DQSummary, check_quality, parse_date, summarize
from core.rules import (
    Action, ActionKind, ContactEvent, ContactState, Reason, RowState, apply_outcome, build_event_updates,
    decide_action, dq_refresh_updates, initialize_row, plan_contact_event, reconcile_sheet_edits,
)
from core.redact import safe_error
from core.schema import AuditEntry, Channel, Col, Config, RunLogEntry, RunState, Status, to_cell
from core.sheets import SheetStore
from core.taxonomy import Outcome

NOT_DUE_REASONS = {Reason.NOT_DUE, Reason.WAITING_FOLLOWUP, Reason.CONTACTED_TODAY, Reason.BELOW_MINIMUM}
BUCKETS = {Status.DQ_FAILED: "dq_failed", Status.NEEDS_HUMAN: "needs_human", Status.HANDLED: "handled",
           Status.IN_PROGRESS: "in_progress", Status.NEW: "in_progress"}

Responder = Callable[..., MockResponse]
Progress = Callable[[int, int, str], None]  # (done, total, label)


class RunInProgressError(RuntimeError):
    """Another run for this date is RUNNING (and not stale)."""


class RunDateError(ValueError):
    """The run date is earlier than rows that were already processed, which would contact people out of order."""


@dataclasses.dataclass
class RunResult:
    run_id: str
    run_date: dt.date
    state: RunState
    rows_total: int = 0
    rows_processed: int = 0  # rows that got a decision in this run
    rows_skipped_already_processed: int = 0
    rows_skipped_not_due: int = 0
    rows_dq_failed: int = 0  # rows with status DQ_FAILED at the end
    rows_escalated: int = 0  # rows newly sent to a person during this run
    rows_initialized: int = 0  # rows set up for the first time
    contact_events: int = 0
    attempts_email: int = 0
    attempts_phone: int = 0
    cells_written: int = 0
    sheet_edits_applied: int = 0  # rows changed by reconcile_sheet_edits
    buckets: dict[str, int] = dataclasses.field(default_factory=dict)
    errors: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    dq_summary: DQSummary | None = None  # data quality counts after the run, for the Run page

    @property
    def reconciled(self) -> bool:
        """DQ_FAILED + NEEDS_HUMAN + IN_PROGRESS + HANDLED == total rows."""
        return sum(self.buckets.values()) == self.rows_total

    def log_fields(self) -> dict[str, Any]:
        return {"state": self.state, "rows_total": self.rows_total, "rows_processed": self.rows_processed,
                "rows_skipped_already_processed": self.rows_skipped_already_processed,
                "rows_skipped_not_due": self.rows_skipped_not_due, "rows_dq_failed": self.rows_dq_failed,
                "rows_escalated": self.rows_escalated, "attempts_email": self.attempts_email,
                "attempts_phone": self.attempts_phone, "errors": self.errors}


def _same(stored: Any, new: Any) -> bool:
    return str(stored if stored is not None else "").strip() == str(to_cell(new)).strip()


def _parse_ts(text: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


class _Run:
    def __init__(self, sheet: SheetStore, today: dt.date, run_id: str, rng: random.Random, now: dt.datetime,
                 cfg: Config, responder: Responder, flush_every: int, progress: Progress | None = None) -> None:
        self.sheet, self.today, self.run_id, self.rng, self.now, self.cfg = sheet, today, run_id, rng, now, cfg
        self.responder, self.flush_every = responder, flush_every
        self.progress = progress or (lambda done, total, label: None)
        self.result = RunResult(run_id=run_id, run_date=today, state=RunState.RUNNING)
        self.records: dict[str, dict[str, Any]] = {}
        self.dq: dict[str, dict[str, Any]] = {}
        self.status: dict[str, Status] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.audits: list[AuditEntry] = []
        self.decided: set[str] = set()
        self.seq = 0

    # ---------------------------------------------------------------- bookkeeping

    def next_id(self) -> str:
        self.seq += 1
        return f"{self.run_id}-{self.seq:05d}"

    def commit(self, updates: Mapping[str, Mapping[str, Any]], audits: Sequence[AuditEntry] = (), *,
               count_escalations: bool = True) -> None:
        """Remember updates (to write at the next flush) and mirror them into the in-memory rows."""
        for key, u in updates.items():
            if not u:
                continue
            self.pending.setdefault(key, {}).update(u)
            rec = self.records[key]
            for col, value in u.items():
                rec[col] = str(to_cell(value))
            new_status = u.get(Col.STATUS)
            if new_status is not None:
                new_status = Status(new_status)
                if count_escalations and new_status is Status.NEEDS_HUMAN and self.status[key] is not Status.NEEDS_HUMAN:
                    self.result.rows_escalated += 1
                self.status[key] = new_status
        self.audits.extend(audits)

    def flush(self) -> None:
        if self.pending:
            self.result.cells_written += self.sheet.update_rows(self.pending)
            self.pending = {}
        if self.audits:
            self.sheet.append_audit(self.audits)
            self.audits = []

    def states(self) -> dict[str, RowState]:
        return {k: RowState.from_row(rec, self.dq[k]) for k, rec in self.records.items()}

    # ---------------------------------------------------------------- stages

    def load(self) -> None:
        df = self.sheet.read_invoices()
        quality = check_quality(df, run_date=self.today, config=self.cfg)
        self.dq = {rec["row_key"]: rec for rec in quality.to_dict("records")}
        self.records = {rec[Col.ROW_KEY]: rec for rec in df.to_dict("records")}
        self.result.rows_total = len(self.records)
        done = [d for d in (parse_date(str(r.get(Col.LAST_PROCESSED_DATE, ""))) for r in self.records.values()) if d]
        if done and self.today < max(done):
            raise RunDateError(f"The run date {self.today.isoformat()} is earlier than rows already processed on {max(done).isoformat()}. "
                               "Pick that date or a later one; running an earlier date would contact people out of order.")

    def setup_rows(self) -> None:
        """First-time setup for rows with no status (counters, starting status, derived flags)."""
        updates: dict[str, dict[str, Any]] = {}
        audits: list[AuditEntry] = []
        for key, rec in self.records.items():
            if not str(rec.get(Col.STATUS, "")).strip():
                updates[key] = initialize_row(rec, self.dq[key], self.today, self.cfg, self.now)
                self.result.rows_initialized += 1
                # a row that starts anywhere other than "in progress" is a decision by the app: say why in the audit log
                status = updates[key][Col.STATUS]
                if status is not Status.IN_PROGRESS:
                    reason = updates[key].get(Col.HUMAN_REASON) or ("already paid" if status is Status.HANDLED else "")
                    reason = getattr(reason, "value", reason)
                    audits.append(AuditEntry(
                        event_id=self.next_id(), run_id=self.run_id, row_key=key, timestamp=self.now, run_date=self.today,
                        contact_key=str(updates[key].get(Col.CONTACT_KEY, "")), outcome=updates[key].get(Col.OUTCOME, ""),
                        status_before=Status.NEW, status_after=status, human_reason=updates[key].get(Col.HUMAN_REASON, ""),
                        response_text=f"First-time setup: {reason}" if reason else "First-time setup", actor="system"))
        self.status = {k: Status.NEW for k in self.records}
        self.commit(updates, audits, count_escalations=False)
        self.status = {k: RowState.from_row(rec, self.dq[k]).status for k, rec in self.records.items()}
        self.refresh_dq()
        self.flush()
        if self.result.rows_initialized and not self.cfg.legacy_migration_done:
            self.sheet.set_config("legacy_migration_done", True)

    def refresh_dq(self) -> None:
        """Recompute data quality on the rows as they are now and store any column that changed.

        Done after setup and again at the end of a run, so the stored columns match what tomorrow's
        check will compute and a re-run on the same day finds nothing to write.
        """
        frame = pd.DataFrame(list(self.records.values()), dtype=object)
        quality = check_quality(frame, run_date=self.today, config=self.cfg)
        self.dq = {rec["row_key"]: rec for rec in quality.to_dict("records")}
        updates = {}
        for key, rec in self.records.items():
            changed = {c: v for c, v in dq_refresh_updates(self.dq[key]).items() if not _same(rec.get(c), v)}
            if changed:
                updates[key] = changed
        self.commit(updates, count_escalations=False)

    def apply_sheet_edits(self) -> None:
        sync = reconcile_sheet_edits(list(self.states().values()), self.today, self.cfg, self.now)
        for issue in sync.issues:
            self.result.errors.append({"type": "sheet_edit", "code": issue.code, "row_keys": issue.row_keys, "message": issue.message})
        audits = [AuditEntry(event_id=self.next_id(), run_id=self.run_id, row_key=a.row_key, timestamp=self.now,
                             run_date=self.today, contact_key=a.contact_key, status_before=a.status_before,
                             status_after=a.status_after, human_reason=a.human_reason or "", response_text=a.note,
                             actor=a.actor) for a in sync.audits]
        self.commit(sync.updates, audits, count_escalations=False)
        self.result.sheet_edits_applied = len(sync.updates)
        self.flush()

    def process_contacts(self) -> None:
        groups: dict[str, list[str]] = {}
        for key, state in self.states().items():
            groups.setdefault(state.contact_key or key, []).append(key)
        total = len(groups)
        for n, contact_key in enumerate(sorted(groups), start=1):
            keys = groups[contact_key]
            try:
                self.process_contact(keys)
            except Exception as exc:  # noqa: BLE001 - one bad contact must not stop the run
                self.record_failure(keys, exc)
            if n % self.flush_every == 0:
                self.flush()
            self.progress(n, total, f"{n} of {total} contacts")

    # ---------------------------------------------------------------- one contact

    def process_contact(self, keys: list[str]) -> None:
        states = [RowState.from_row(self.records[k], self.dq[k]) for k in keys]
        contact = ContactState.from_rows(states)
        event = plan_contact_event(states, self.today, self.cfg, self.now)
        in_event = set(event.row_keys) if event else set()

        updates: dict[str, dict[str, Any]] = {}
        audits: list[AuditEntry] = []
        decided: set[str] = set()
        already = not_due = 0
        for s in states:
            if s.row_key in in_event:
                continue
            action = decide_action(s, contact, self.today, self.cfg, self.now)
            if action.reason == Reason.ALREADY_PROCESSED:
                already += 1
                continue
            decided.add(s.row_key)
            if action.reason in NOT_DUE_REASONS:
                not_due += 1
            if action.updates:
                updates.setdefault(s.row_key, {}).update(action.updates)
            if action.kind is ActionKind.MARK and Col.STATUS in action.updates:
                audits.append(self.mark_audit(s, action))

        attempt: tuple[Channel, int] | None = None
        if event:
            more_updates, more_audits, attempt = self.run_event(event, states, contact)
            for k, u in more_updates.items():
                updates.setdefault(k, {}).update(u)
            audits.extend(more_audits)
            decided.update(event.row_keys)

        # nothing above raised, so now it is safe to record it
        self.commit(updates, audits)
        self.decided.update(decided)
        self.result.rows_skipped_already_processed += already
        self.result.rows_skipped_not_due += not_due
        if attempt:
            self.result.contact_events += 1
            if attempt[1]:
                if attempt[0] is Channel.EMAIL:
                    self.result.attempts_email += 1
                else:
                    self.result.attempts_phone += 1

    def run_event(self, event: ContactEvent, states: list[RowState], contact: ContactState,
                  outcome: Outcome | None = None) -> tuple[dict[str, dict[str, Any]], list[AuditEntry], tuple[Channel, int]]:
        by_key = {s.row_key: s for s in states}
        primary = by_key[event.primary_row_key]
        rec, dq = self.records[primary.row_key], self.dq[primary.row_key]
        if outcome is None:
            response = self.responder(
                event.channel,
                ContactInfo(name=str(rec.get(Col.CONTACT_NAME, "")), customer=str(rec.get(Col.CUSTOMER, "")),
                            email=str(dq.get("normalized_email") or rec.get(Col.EMAIL, "")),
                            phone=str(dq.get("normalized_phone") or rec.get(Col.PHONE, "")),
                            country=str(rec.get(Col.COUNTRY, ""))),
                InvoiceInfo(invoice_number=str(rec.get(Col.INVOICE_NUMBER, "")), amount_outstanding=primary.amount_outstanding or 0.0,
                            currency=str(rec.get(Col.CURRENCY, "")), due_date=primary.due_date),
                self.rng, today=self.today)
            res = apply_outcome(primary, contact, event.channel, response.outcome, self.today, self.cfg,
                                promised_days=response.promised_days, amount_paid=response.amount_paid,
                                summary=response.summary, now=self.now)
            text = response.text
        else:  # recording a system error after a failure
            res = apply_outcome(primary, contact, event.channel, outcome, self.today, self.cfg, now=self.now)
            text = f"{outcome.value}: the follow-up for this contact failed and will be retried"
        updates = build_event_updates(event, res, states, self.today, self.now)

        event_id = self.next_id()
        audits = []
        for key in event.row_keys:
            s, is_primary = by_key[key], key == event.primary_row_key
            after = updates[key].get(Col.STATUS, s.status)
            audits.append(AuditEntry(
                event_id=self.next_id(), run_id=self.run_id, row_key=key, timestamp=self.now, run_date=self.today,
                contact_key=s.contact_key, contact_event_id=event_id, channel=event.channel,
                attempt_no_for_channel=res.attempt_no_for_channel if res.counted_as_attempt else "",
                counted_as_attempt=res.counted_as_attempt and is_primary,  # one attempt per combined contact
                outcome=res.outcome, response_text=text if is_primary else f"Covered by the contact of {event.primary_row_key}",
                promised_date=updates[key].get(Col.LAST_PROMISED_DATE, ""), status_before=s.status,
                status_after=after, human_reason=updates[key].get(Col.HUMAN_REASON, ""), actor="system"))
        return updates, audits, (event.channel, int(res.counted_as_attempt))

    def mark_audit(self, s: RowState, action: Action) -> AuditEntry:
        return AuditEntry(
            event_id=self.next_id(), run_id=self.run_id, row_key=s.row_key, timestamp=self.now, run_date=self.today,
            contact_key=s.contact_key, outcome=action.updates.get(Col.OUTCOME, ""), status_before=s.status,
            status_after=action.updates[Col.STATUS], human_reason=action.human_reason or "", actor="system",
            response_text=f"Decision: {action.reason}")

    def record_failure(self, keys: list[str], exc: Exception) -> None:
        """Log the failure, then try to record a SYSTEM_ERROR for the contact so it is not retried today."""
        self.result.errors.append({"type": "error", "row_keys": keys, "message": safe_error(exc)})
        try:
            states = [RowState.from_row(self.records[k], self.dq[k]) for k in keys]
            event = plan_contact_event(states, self.today, self.cfg, self.now)
            if event is None:
                return
            updates, audits, _ = self.run_event(event, states, ContactState.from_rows(states), Outcome.SYSTEM_ERROR)
            self.commit(updates, audits)
            self.decided.update(event.row_keys)
        except Exception as inner:  # noqa: BLE001 - leave the rows unstamped so a re-run today tries again
            self.result.errors.append({"type": "error", "row_keys": keys, "message": f"could not record the failure: {safe_error(inner)}"})

    # ---------------------------------------------------------------- finish

    def finish(self) -> None:
        r = self.result
        r.rows_processed = len(self.decided)
        r.buckets = {name: 0 for name in ("dq_failed", "needs_human", "in_progress", "handled")}
        for status in self.status.values():
            r.buckets[BUCKETS[status]] += 1
        r.rows_dq_failed = r.buckets["dq_failed"]
        if self.dq:
            r.dq_summary = summarize(pd.DataFrame(list(self.dq.values())))
        if not r.reconciled:
            r.errors.append({"type": "reconciliation", "message": f"buckets add up to {sum(r.buckets.values())}, not {r.rows_total}"})
        r.state = RunState.DONE


def _check_lock(sheet: SheetStore, today: dt.date, now: dt.datetime, stale_after_minutes: int) -> None:
    log = sheet.read_run_log()
    if log.empty:
        return
    for _, row in log[(log["run_date"] == str(today)) & (log["state"] == RunState.RUNNING.value)].iterrows():
        started = _parse_ts(str(row["started_at"]))
        if started is not None and now - started < dt.timedelta(minutes=stale_after_minutes):
            raise RunInProgressError(f"run {row['run_id']} for {today} started at {row['started_at']} is still RUNNING")
        sheet.update_run_log(str(row["run_id"]), {"state": RunState.FAILED, "finished_at": now,
                                                   "errors": [{"type": "stale", "message": "marked FAILED: it never finished"}]})


def _confirm_lock(sheet: SheetStore, today: dt.date, run_id: str, now: dt.datetime, stale_after_minutes: int) -> None:
    """After logging our own RUNNING row, check we are the earliest active run for the date.

    Two people pressing Run in the same second can both pass the first check. The earlier row (by start
    time, then run id) wins; the other marks itself FAILED and stops without touching any invoice.
    """
    log = sheet.read_run_log()
    active = log[(log["run_date"] == str(today)) & (log["state"] == RunState.RUNNING.value)]
    rivals = []
    for _, row in active.iterrows():
        started = _parse_ts(str(row["started_at"]))
        if started is not None and now - started < dt.timedelta(minutes=stale_after_minutes):
            rivals.append((started, str(row["run_id"])))
    if rivals and min(rivals)[1] != run_id:
        winner = min(rivals)[1]
        sheet.update_run_log(run_id, {"state": RunState.FAILED, "finished_at": now,
                                      "errors": [{"type": "lock", "message": f"another run ({winner}) started first, so this one did not run"}]})
        raise RunInProgressError(f"run {winner} for {today} started first and is still RUNNING")


def run_daily(sheet: SheetStore, today: dt.date, run_id: str | None = None, rng: random.Random | None = None, *,
              now: dt.datetime | None = None, config: Config | None = None, responder: Responder = generate_response,
              stale_after_minutes: int = 30, flush_every: int = 50, progress: Progress | None = None,
              started_by: str = "system") -> RunResult:
    """Run today's follow-ups. Raises RunInProgressError if another run for the date is active.

    ``responder`` replaces the mock contact (tests pass one that forces outcomes). ``flush_every`` is how
    many contacts to process between writes to the sheet. ``progress(done, total, label)`` is called as
    contacts are processed, for a progress bar.
    """
    sheet.ensure_schema()
    cfg = config or sheet.load_config()
    fixed_clock = now is not None  # tests pass a fixed time; otherwise use the real clock in the team time zone
    now = now or now_in(cfg.timezone)
    clock = (lambda: now) if fixed_clock else (lambda: now_in(cfg.timezone))
    run_id = run_id or f"run-{today:%Y%m%d}-{now:%H%M%S}"
    rng = rng or random.Random()
    _check_lock(sheet, today, now, stale_after_minutes)
    sheet.append_run_log(RunLogEntry(run_id=run_id, run_date=today, started_at=now, started_by=(started_by or "system")[:80]))
    _confirm_lock(sheet, today, run_id, now, stale_after_minutes)

    run = _Run(sheet, today, run_id, rng, now, cfg, responder, flush_every, progress)
    try:
        run.progress(0, 1, "Reading the sheet and checking data quality")
        run.load()
        run.setup_rows()
        run.progress(0, 1, "Applying decisions typed into the sheet")
        run.apply_sheet_edits()
        run.process_contacts()
        run.refresh_dq()
        run.flush()
        run.finish()
    except Exception as exc:
        run.result.state = RunState.FAILED
        run.result.errors.append({"type": "fatal", "message": safe_error(exc)})
        try:
            sheet.update_run_log(run_id, {**run.result.log_fields(), "finished_at": clock()})
        except Exception:  # noqa: BLE001 - never let a failing log update hide the real error
            pass
        raise
    sheet.update_run_log(run_id, {**run.result.log_fields(), "finished_at": clock()})
    return run.result
