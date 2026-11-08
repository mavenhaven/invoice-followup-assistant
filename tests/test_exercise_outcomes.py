"""scripts/exercise_outcomes.py: every outcome is produced, the checks bite, and it will not run on a used sheet."""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest
from test_runner import make_store, sheet_rows

from core.schema import Col
from core.taxonomy import Outcome, info

ROOT = Path(__file__).resolve().parent.parent


class Titled:
    """The fake spreadsheet, plus the title the script prints."""

    def __init__(self, inner, title):
        self._inner, self.title = inner, title

    def __getattr__(self, name):
        return getattr(self._inner, name)
spec = importlib.util.spec_from_file_location("exercise_outcomes", ROOT / "scripts" / "exercise_outcomes.py")
ex = importlib.util.module_from_spec(spec)
sys.modules["exercise_outcomes"] = ex  # dataclasses look the module up by name
spec.loader.exec_module(ex)
START = dt.date(2026, 8, 20)


@pytest.fixture(scope="module")
def result():
    store, ws = make_store()
    report = ex.exercise(store, START, days=30, seed=1)
    return store, report


def test_every_outcome_a_contact_can_produce_is_seen_at_least_once(result):
    _, report = result
    by_name = {r.outcome: r for r in report.rows}
    producible = [o.value for o in Outcome if info(o).channels]
    assert len(producible) == 23
    missing = [o for o in producible if by_name[o].verdict != "seen"]
    assert not missing, f"never produced: {missing}"
    assert all(by_name[o].events >= 1 for o in producible)


def test_the_outcomes_the_app_decides_itself_are_covered_too(result):
    _, report = result
    by_name = {r.outcome: r for r in report.rows}
    assert by_name["DQ_FAILED"].verdict == "seen" and by_name["DQ_FAILED"].events >= 3  # the three blocked rows
    assert by_name["MAX_ATTEMPTS_REACHED"].verdict == "seen"  # the stubborn contact used up its attempts
    assert by_name["NEEDS_HUMAN_FOLLOW_UP"].verdict == "safety net"  # only reachable when a person removes a contact's channels
    assert report.missing == [] and len(report.rows) == len(Outcome)


def test_phone_only_and_email_only_outcomes_happen_on_the_right_channel(result):
    _, report = result
    channels = {r.outcome: r.channels for r in report.rows}
    for outcome in ("NO_RESPONSE", "EMAIL_BOUNCE_SOFT", "EMAIL_BOUNCE_HARD", "EMAIL_INVALID_ADDRESS", "EMAIL_SEND_FAILURE"):
        assert channels[outcome] == "email", outcome
    for outcome in ("NO_ANSWER", "LEFT_VOICEMAIL", "PHONE_LINE_BUSY", "PHONE_INVALID_NUMBER", "PHONE_DISCONNECTED", "PHONE_SYSTEM_ERROR"):
        assert channels[outcome] == "phone", outcome


def test_all_the_checks_pass_and_the_verdict_is_pass(result):
    _, report = result
    failed = [(w, d) for w, ok, d in report.checks if not ok]
    assert not failed, failed
    assert report.passed and len(report.checks) == 6
    text = ex.render(report)
    assert "RESULT: PASS" in text and "[FAIL]" not in text and "22 of 26 outcomes seen" not in text
    assert "NEEDS_HUMAN_FOLLOW_UP is a safety net" in text


def test_the_final_state_is_consistent(result):
    store, report = result
    assert sum(report.last_buckets.values()) == 35 and report.last_buckets["dq_failed"] == 3
    rows = store.read_invoices()
    assert (rows[Col.NOTES] != "").sum() == 6  # the six notes from the original file, untouched
    assert (rows[Col.EMAIL_ATTEMPTS].astype(int) <= 5).all() and (rows[Col.PHONE_ATTEMPTS].astype(int) <= 5).all()


def test_a_missing_outcome_is_reported_as_a_failure():
    report = ex.Report([ex.Row("PAID", "Contact made", 0, "-", "-", "MISSING")], 1, [("x", True, "ok")], {})
    assert report.missing == ["PAID"] and not report.passed and "RESULT: FAIL (missing: PAID)" in ex.render(report)
    failing_check = ex.Report([ex.Row("PAID", "Contact made", 1, "email", "2026-08-20", "seen")], 1, [("rule broke", False, "detail")], {})
    assert not failing_check.passed and "[FAIL] rule broke  (detail)" in ex.render(failing_check)


def test_the_stubborn_contact_is_a_single_invoice_contact_with_both_channels():
    store, _ = make_store()
    store.ensure_schema()
    email = ex.pick_stubborn_contact(store, START, store.load_config())
    assert email and email not in ("t.berger@veltner-gastro.at", "k.vogel@nordbach-handels.de")  # not a multi-invoice contact
    row = store.read_invoices().query("Email == @email").iloc[0]
    assert row[Col.PHONE] and row[Col.READY_FOR_CONTACT] == "Yes"
    tiny, _ = make_store(sheet_rows(0))
    tiny.ensure_schema()
    assert ex.pick_stubborn_contact(tiny, START, tiny.load_config()) is None


def test_the_script_refuses_to_run_on_a_used_sheet_or_without_confirmation(tmp_path, monkeypatch, capsys):
    store, _ = make_store()
    store.ensure_schema()
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('sheet_id = "SHEETID1234567890123"\n[gcp_service_account]\nclient_email = "a@b"\nprivate_key = "x"\n', encoding="utf-8")
    monkeypatch.setattr(ex, "SECRETS", secrets)
    store._ss = Titled(store._spreadsheet(), "Overdue invoices - Copy")
    import core.sheets as sheets
    monkeypatch.setattr(sheets, "connect", lambda info, sid, **kw: store)

    assert ex.main([]) == 2  # no --yes
    out = capsys.readouterr().out
    assert "Overdue invoices - Copy" in out and "scratch COPY" in out and len(store.read_audit_log()) == 0

    from core.schema import AuditEntry
    store.append_audit([AuditEntry(event_id="e", run_id="r", row_key="INV-2026-4471#1")])
    assert ex.main(["--yes"]) == 2  # the sheet already has an audit trail
    assert "not a fresh scratch copy" in capsys.readouterr().out and len(store.read_audit_log()) == 1


def test_the_script_runs_end_to_end_on_a_fresh_sheet_and_prints_the_checklist(tmp_path, monkeypatch, capsys):
    store, _ = make_store()
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('sheet_id = "SHEETID1234567890123"\n[gcp_service_account]\nclient_email = "a@b"\nprivate_key = "x"\n', encoding="utf-8")
    monkeypatch.setattr(ex, "SECRETS", secrets)
    store._ss = Titled(store._spreadsheet(), "Scratch copy")
    import core.sheets as sheets
    monkeypatch.setattr(sheets, "connect", lambda info, sid, **kw: store)
    assert ex.main(["--yes", "--pause", "0", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "day 30/30" in out and "PROMISE_TO_PAY" in out and "EMAIL_BOUNCE_HARD" in out
    assert tomllib.loads(secrets.read_text(encoding="utf-8"))["sheet_id"] == "SHEETID1234567890123"
