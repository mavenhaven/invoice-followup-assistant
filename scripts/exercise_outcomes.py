"""Exercise every outcome end to end on a SCRATCH Google Sheet, and print a pass / fail checklist.

    python scripts/exercise_outcomes.py --yes

It runs the real daily run for up to 30 simulated days, with replies chosen so that every outcome a contact can produce
(email bounces, busy lines, disputes, promises, payments, system errors...) happens at least once, one contact is left
unanswered until its attempt limit is reached, and every rule that can be checked from the sheet's own logs is checked.

WARNING: it writes simulated results into the sheet in secrets.toml and moves its dates forward by up to 30 days. Use a
fresh COPY of your sheet and throw it away afterwards. It refuses to run on a sheet that already has an audit trail.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import random
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from core.clock import today_in  # noqa: E402
from core.mock_responses import ContactInfo, InvoiceInfo, MockResponse, generate_response  # noqa: E402
from core.quality import check_quality  # noqa: E402
from core.runner import run_daily  # noqa: E402
from core.schema import Channel, Col, Config  # noqa: E402
from core.sheets import SheetStore  # noqa: E402
from core.taxonomy import Outcome, category_of, info  # noqa: E402

SECRETS = ROOT / ".streamlit" / "secrets.toml"
O = Outcome

# What a contact can produce on the phone or email (everything with a channel), in the order we try them: outcomes that
# keep a contact in follow-up first, outcomes that end it (payment, dispute, refusal...) last.
ORDER = [O.EMAIL_BOUNCE_SOFT, O.PHONE_LINE_BUSY, O.LEFT_VOICEMAIL, O.OUT_OF_OFFICE, O.EMAIL_SEND_FAILURE, O.PHONE_SYSTEM_ERROR,
         O.SYSTEM_ERROR, O.PAYMENT_IN_PROCESS, O.REQUESTS_INVOICE_COPY, O.CALLBACK_REQUESTED, O.PROMISE_TO_PAY, O.PARTIAL_PAYMENT,
         O.EMAIL_BOUNCE_HARD, O.EMAIL_INVALID_ADDRESS, O.PHONE_INVALID_NUMBER, O.PHONE_DISCONNECTED, O.NO_RESPONSE, O.NO_ANSWER,
         O.DISPUTE, O.SETTLEMENT_REQUESTED, O.REFUSED_TO_PAY, O.WRONG_CONTACT, O.PAID]
PRODUCIBLE = [o for o in Outcome if info(o).channels]
SAFETY_NET = {O.NEEDS_HUMAN_FOLLOW_UP}  # only appears when a person removes a contact's channels outside a contact event


class Coverage:
    """A stand-in for the mock contact: picks the next outcome nobody has seen yet, and leaves one contact unanswered."""

    def __init__(self, stubborn_email: str | None) -> None:
        self.stubborn = (stubborn_email or "").lower()
        self.events_for_stubborn = 0
        self.seen: set[Outcome] = set()
        self.log: list[tuple[str, Outcome]] = []

    def __call__(self, channel, contact: ContactInfo, invoice: InvoiceInfo, rng, *, today, **kw) -> MockResponse:
        ch = Channel(channel)
        if self.stubborn and contact.email.lower() == self.stubborn:
            self.events_for_stubborn += 1
            # nine unanswered attempts, then a reply that is a real response but names no date: the next decision finds
            # the attempt limit used up
            outcome = O.REQUESTS_INVOICE_COPY if self.events_for_stubborn >= 10 else (O.NO_RESPONSE if ch is Channel.EMAIL else O.NO_ANSWER)
        else:
            usable = [o for o in ORDER if o not in self.seen and ch in info(o).channels]
            outcome = usable[0] if usable else (O.NO_RESPONSE if ch is Channel.EMAIL else O.NO_ANSWER)
            self.seen.add(outcome)
        self.log.append((contact.email or contact.phone, outcome))
        days = 2 if info(outcome).date_kind and outcome is not O.OUT_OF_OFFICE else None  # stay within 5 days: no approval needed
        return generate_response(ch, contact, invoice, rng, today=today, forced_outcome=outcome, promised_days=days)


@dataclasses.dataclass
class Row:
    outcome: str
    category: str
    events: int
    channels: str
    first_date: str
    verdict: str  # seen / MISSING / safety net


@dataclasses.dataclass
class Report:
    rows: list[Row]
    days_run: int
    checks: list[tuple[str, bool, str]]  # (what, passed, detail)
    last_buckets: dict[str, int]

    @property
    def missing(self) -> list[str]:
        return [r.outcome for r in self.rows if r.verdict == "MISSING"]

    @property
    def passed(self) -> bool:
        return not self.missing and all(ok for _, ok, _ in self.checks)


def pick_stubborn_contact(store: SheetStore, run_date: dt.date, cfg: Config) -> str | None:
    """A contact with a single, contactable invoice and both an email and a phone number."""
    df = store.read_invoices()
    q = check_quality(df, run_date=run_date, config=cfg)
    fresh = (df[Col.OUTCOME].str.strip() == "") & (df[Col.CONTACT_ATTEMPTS].str.strip().isin(["", "0"]))  # no history that would send it to a person
    ok = q[(q["dq_status"] != "FAIL") & q["can_contact_email"] & q["can_contact_phone"] & ~q["is_excluded"] & (q["contact_rows"] == 1) & fresh]
    return None if ok.empty else str(ok.sort_values("contact_key").iloc[0]["normalized_email"])


def exercise(store: SheetStore, start: dt.date, days: int = 30, seed: int = 1, pause: float = 0.0,
             say: Callable[[str], None] = lambda s: None) -> Report:
    store.ensure_schema()
    cfg = store.load_config()
    notes_before = store.read_invoices()[Col.NOTES].tolist()
    coverage = Coverage(pick_stubborn_contact(store, start, cfg))
    rng = random.Random(seed)
    buckets: dict[str, int] = {}
    problems: list[str] = []
    for n in range(days):
        day = start + dt.timedelta(days=n)
        res = run_daily(store, day, f"exercise-{n:02d}", rng, now=dt.datetime.combine(day, dt.time(9)), responder=coverage,
                        started_by="exercise_outcomes.py")
        buckets = res.buckets
        if not res.reconciled:
            problems.append(f"day {n}: the four groups did not add up")
        if [e for e in res.errors if e.get("type") in ("error", "fatal")]:
            problems.append(f"day {n}: {res.errors[0].get('message', '')[:80]}")
        say(f"  day {n + 1:2d}/{days}  {day}  contacts made: {res.contact_events:2d}  {res.buckets}")
        if n < days - 1 and pause:
            time.sleep(pause)

    audit = store.read_audit_log()
    events = audit[audit["contact_event_id"] != ""].drop_duplicates("contact_event_id")
    marks = audit[(audit["contact_event_id"] == "") & (audit["actor"] == "system")]
    rows: list[Row] = []
    for outcome in Outcome:
        # contact outcomes are events with a channel; the three the app decides itself are state changes without a contact
        src = events[events["outcome"] == outcome.value] if info(outcome).channels else marks[marks["outcome"] == outcome.value]
        n_seen = len(src)
        verdict = "seen" if n_seen else ("safety net" if outcome in SAFETY_NET else "MISSING")
        rows.append(Row(outcome.value, category_of(outcome).value, n_seen,
                        ", ".join(sorted(set(src["channel"]) - {""})) or ("app" if not info(outcome).channels else "-"),
                        str(src["run_date"].min()) if n_seen else "-", verdict))

    # checks that can be made from the sheet's own logs
    counted = events[events["counted_as_attempt"] == "Yes"]
    over = [k for k, g in counted.groupby("contact_key") if (g["channel"] == "email").sum() > cfg.attempt_cap or (g["channel"] == "phone").sum() > cfg.attempt_cap]
    twice = [k for k, g in counted.groupby("contact_key") if not g["run_date"].is_unique]
    again = run_daily(store, start + dt.timedelta(days=days - 1), "exercise-rerun", rng, now=dt.datetime.combine(start + dt.timedelta(days=days - 1), dt.time(18)),
                      responder=coverage, started_by="exercise_outcomes.py")
    invoices = store.read_invoices()
    checks = [
        (f"no contact ever got more than {cfg.attempt_cap} attempts on one channel", not over, ", ".join(over) or "ok"),
        ("no contact was contacted twice on one day", not twice, ", ".join(twice) or "ok"),
        ("the four status groups added up on every day", not any("did not add up" in p for p in problems), "; ".join(problems) or "ok"),
        ("running the last day again changed nothing", again.cells_written == 0 and again.contact_events == 0, f"{again.cells_written} cells written"),
        ("the Notes column was never changed", invoices[Col.NOTES].tolist() == notes_before, "Notes differs from the start"),
        ("one contact reached its attempt limit and went to a person",
         bool(len(marks[marks["outcome"] == O.MAX_ATTEMPTS_REACHED.value])), f"{len(marks[marks['outcome'] == O.MAX_ATTEMPTS_REACHED.value])} row(s)"),
    ]
    return Report(rows, days, checks, buckets)


def render(report: Report) -> str:
    lines = [f"\n{'Outcome':<24}{'Category':<15}{'Events':>7}  {'Channel':<12}{'First seen':<12}Result", "-" * 84]
    for r in report.rows:
        mark = {"seen": "OK", "safety net": "n/a", "MISSING": "MISSING"}[r.verdict]
        lines.append(f"{r.outcome:<24}{r.category:<15}{r.events:>7}  {r.channels:<12}{r.first_date:<12}{mark}")
    lines.append("\nChecks")
    for what, ok, detail in report.checks:
        lines.append(f"  [{'OK' if ok else 'FAIL'}] {what}" + ("" if ok else f"  ({detail})"))
    n_ok = sum(r.verdict == "seen" for r in report.rows)
    lines.append(f"\n{n_ok} of {len(report.rows)} outcomes seen"
                 + ("; NEEDS_HUMAN_FOLLOW_UP is a safety net that only appears when a person removes a contact's channels, covered by unit tests" if any(r.verdict == "safety net" for r in report.rows) else ""))
    lines.append("RESULT: " + ("PASS" if report.passed else f"FAIL (missing: {', '.join(report.missing) or 'none'})"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="confirm that you want simulated results written to the sheet")
    p.add_argument("--days", type=int, default=30, help="simulated days to run (default 30)")
    p.add_argument("--pause", type=float, default=8.0, help="seconds to wait between days, to stay under Google's request limits")
    p.add_argument("--allow-used-sheet", action="store_true", help="run even if the sheet already has an audit trail")
    args = p.parse_args(argv)

    if not SECRETS.exists():
        print("No .streamlit/secrets.toml. Create it with scripts/make_secrets.py first.")
        return 2
    cfg = tomllib.loads(SECRETS.read_text(encoding="utf-8"))
    from core.sheets import connect

    store = connect(dict(cfg["gcp_service_account"]), cfg["sheet_id"], invoices_tab=cfg.get("invoices_tab"))
    title = store._spreadsheet().title
    store.ensure_schema()
    used = len(store.read_audit_log())
    print(f"Sheet: {title!r}  ({cfg['sheet_id']})")
    if used and not args.allow_used_sheet:
        print(f"This sheet already has {used} audit log rows, so it is not a fresh scratch copy. Make a new copy of your original sheet, share it with the "
              "service account, put its address in secrets.toml (scripts/make_secrets.py --force), and run this again.")
        return 2
    if not args.yes:
        print("\nThis will write simulated follow-ups into that sheet and move its dates forward by up to "
              f"{args.days} days.\nUse a scratch COPY, never your real data. Run again with --yes to go ahead.")
        return 2
    start = today_in(store.load_config().timezone)
    print(f"Running {args.days} simulated days from {start} (about {int(args.days * (args.pause + 15) / 60) + 1} minutes)...")
    report = exercise(store, start, args.days, pause=args.pause, say=print)
    print(render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
