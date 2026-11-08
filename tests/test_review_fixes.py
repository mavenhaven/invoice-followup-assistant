"""Fixes from the final review: secrets never shown or stored, run safety, the access gate, and friendly errors."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_runner import DAY1, d, forced, make_store, run, sheet_rows

from core.actions import ActionError, clean_actor, decide_extension
from core.clock import now_in
from core.mock_responses import generate_response
from core.redact import MASK, redact, safe_error
from core.runner import RunDateError, RunInProgressError, run_daily
from core.schema import Col, RunLogEntry
from core.taxonomy import Outcome

ROOT = Path(__file__).resolve().parent.parent
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nabcdefghijklmnop\n-----END PRIVATE KEY-----\n"
SAME = {Col.CONTACT_NAME: "Same Person", Col.EMAIL: "same@x.de", Col.PHONE: "+49 30 5540 1100"}


@pytest.fixture(autouse=True)
def fresh_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def open_page(page: str, store, secrets: dict | None = None, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    at.session_state["store_override"] = store
    for k, v in state.items():
        at.session_state[k] = v
    for k, v in (secrets or {}).items():
        at.secrets[k] = v
    at.run()
    return at


# ---------------------------------------------------------------- secrets are masked


def test_redact_removes_keys_tokens_and_sheet_addresses():
    assert PEM.split("\n")[1] not in redact(f"failed with key {PEM} end")
    assert redact('{"private_key": "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n", "x": 1}') .count("ABC") == 0
    assert redact("private_key = 'abcdef123456'") == f"private_key = '{MASK}'"
    assert "ya29.A0ARrdaM-secret_token" not in redact("token ya29.A0ARrdaM-secret_token expired")
    assert "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop12345")
    url = "https://docs.google.com/spreadsheets/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg/edit"
    assert redact(url) == f"https://docs.google.com/spreadsheets/d/{MASK}/edit"


def test_redact_masks_exact_secret_values_including_their_escaped_form():
    key = "line1\nline2-SECRETBODY"
    assert "SECRETBODY" not in redact(f"bad key {key}", [key])
    assert "SECRETBODY" not in redact('bad key "line1\\nline2-SECRETBODY"', [key])
    assert redact("a password abc here", ["abc"]) == "a password abc here"  # too short to mask: would hide ordinary words
    assert redact("nothing secret here, invoice INV-2026-4471#2, 1,234.50 EUR") == "nothing secret here, invoice INV-2026-4471#2, 1,234.50 EUR"


def test_safe_error_names_the_error_masks_secrets_and_is_short():
    text = safe_error(RuntimeError("Could not parse " + PEM + " at /spreadsheets/d/" + "A" * 44), [])
    assert text.startswith("RuntimeError: ") and "MIIEvQ" not in text and "A" * 44 not in text
    assert len(safe_error(ValueError("x" * 5000))) <= 400


def leaking(secret: str):
    def responder(channel, contact, invoice, rng, *, today, **kw):
        if invoice.invoice_number == "INV-2":
            raise RuntimeError(f"gateway rejected credentials {secret}")
        return generate_response(channel, contact, invoice, rng, today=today, **kw)
    return responder


def test_an_error_that_contains_a_private_key_never_reaches_the_sheet():
    store, ws = make_store(sheet_rows(2))
    res = run(store, responder=leaking(PEM))
    everything = json.dumps(res.errors) + store.read_run_log().to_json() + store.read_audit_log().to_json()
    everything += json.dumps([[str(c) for c in row] for row in ws.grid])
    assert "MIIEvQ" not in everything and "BEGIN PRIVATE KEY" not in everything
    assert res.errors and "gateway rejected credentials" in res.errors[0]["message"]  # still tells you what went wrong


# ---------------------------------------------------------------- the run


def test_a_run_dated_before_rows_already_processed_is_refused_and_logged():
    store, ws = make_store(sheet_rows(2))
    run(store, d(5), "run-late")
    before = [list(r) for r in ws.grid]
    with pytest.raises(RunDateError, match="earlier than rows already processed on 2026-08-25"):
        run(store, d(2), "run-early")
    assert [list(r) for r in ws.grid] == before  # nothing was changed
    log = store.read_run_log().set_index("run_id")
    assert log.loc["run-early", "state"] == "FAILED" and "earlier than rows already processed" in log.loc["run-early", "errors"]
    run(store, d(5), "run-same")  # the same date again is fine (it is a no-op)
    run(store, d(6), "run-next")  # and a later one too


def test_two_people_pressing_run_together_cannot_both_run():
    store, ws = make_store(sheet_rows(2))
    store.ensure_schema()
    original = store.append_run_log

    def slip_in_a_rival(entry):  # between our lock check and our own log row, someone else's run starts earlier
        original(RunLogEntry(run_id="rival", run_date=DAY1, started_at=dt.datetime(2026, 8, 20, 8, 59, 59)))
        return original(entry)
    store.append_run_log = slip_in_a_rival
    grid = [list(r) for r in ws.grid]
    with pytest.raises(RunInProgressError, match="rival"):
        run(store)
    assert [list(r) for r in ws.grid] == grid  # the loser touched no invoice
    log = store.read_run_log().set_index("run_id")
    assert log.loc["run-1", "state"] == "FAILED" and "started first" in log.loc["run-1", "errors"] and log.loc["rival", "state"] == "RUNNING"


def test_the_earlier_of_two_overlapping_runs_carries_on():
    store, _ = make_store(sheet_rows(2))
    store.ensure_schema()
    original = store.append_run_log

    def slip_in_a_later_rival(entry):
        out = original(entry)
        original(RunLogEntry(run_id="later-rival", run_date=DAY1, started_at=dt.datetime(2026, 8, 20, 9, 0, 5)))
        return out
    store.append_run_log = slip_in_a_later_rival
    assert run(store).state.value == "DONE"


def test_a_failing_log_update_does_not_hide_the_real_error():
    store, _ = make_store(sheet_rows(1))
    real_read = store.read_invoices
    calls = {"n": 0}

    def read_then_fail():
        calls["n"] += 1
        if calls["n"] >= 1 and store._invoices_ws() is not None and getattr(store, "_armed", False):
            raise RuntimeError("the real problem")
        return real_read()
    store.ensure_schema()
    store._armed = True
    store.read_invoices = read_then_fail
    store.update_run_log = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("log update failed too"))
    with pytest.raises(RuntimeError, match="the real problem"):
        run(store)


def test_the_run_log_says_who_started_it_and_uses_the_team_time_zone():
    store, _ = make_store(sheet_rows(1))
    store.ensure_schema()
    store.set_config("timezone", "Pacific/Kiritimati")  # UTC+14, far from any server clock
    before = now_in("Pacific/Kiritimati")
    run_daily(store, DAY1, "r1", started_by="alex@example.com")
    log = store.read_run_log().iloc[0]
    started = dt.datetime.strptime(log["started_at"], "%Y-%m-%d %H:%M:%S")
    assert log["started_by"] == "alex@example.com" and abs((started - before).total_seconds()) < 120
    run_daily(store, d(1), "r2")
    assert store.read_run_log().set_index("run_id").loc["r2", "started_by"] == "system"


# ---------------------------------------------------------------- people's names and the audit log


def test_actor_names_are_cleaned_before_they_are_stored():
    assert clean_actor("  Alex   Müller \n\t") == "Alex Müller"
    assert clean_actor("Eve\r\n=HYPERLINK(\"http://x\")") == 'Eve =HYPERLINK("http://x")'
    assert clean_actor("x" * 300) == "x" * 80 and clean_actor("\n\t ") == "" and clean_actor(None) == ""


def test_a_name_that_cleans_to_nothing_is_refused_and_a_messy_one_is_stored_tidily():
    store, _ = make_store(sheet_rows(2, **SAME))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=14))
    with pytest.raises(ActionError, match="Enter your name"):
        decide_extension(store, "same@x.de", approve=True, actor="\n\t", today=DAY1, now=dt.datetime(2026, 8, 20, 11))
    decide_extension(store, "same@x.de", approve=True, actor="  Alex \n Smith ", today=DAY1, now=dt.datetime(2026, 8, 20, 11))
    assert set(store.read_audit_log().query("actor != 'system'")["actor"]) == {"Alex Smith"}


def test_values_starting_with_formula_characters_are_written_as_text_not_formulas():
    store, ws = make_store(sheet_rows(1))
    run(store, responder=forced(Outcome.DISPUTE, Outcome.DISPUTE))
    from core.actions import resolve_row_action
    options: list[object] = []
    for sheet in store._worksheets():  # record how every write is sent to Google
        for name in ("batch_update", "append_rows"):
            original = getattr(sheet, name)
            setattr(sheet, name, lambda *a, _o=original, **k: (options.append(k.get("value_input_option")), _o(*a, **k))[1])
    resolve_row_action(store, "INV-1#1", actor="=cmd()", today=DAY1, now=dt.datetime(2026, 8, 20, 11), close=True, note="=1+1")
    # every write uses the RAW input option, so Google stores the text and never evaluates a cell as a formula
    assert options and set(options) == {"RAW"}
    source = (ROOT / "core" / "sheets.py").read_text(encoding="utf-8")
    assert "USER_ENTERED" not in source


# ---------------------------------------------------------------- the access gate


def test_with_a_password_set_nothing_is_shown_until_it_is_entered():
    store, _ = make_store()
    run(store)
    for page in ("app.py", "pages/1_Run.py", "pages/2_Human_Queue.py", "pages/3_Dashboard.py"):
        at = open_page(page, store, secrets={"app_password": "pw-123456"})
        assert not at.exception and not at.metric and any("protected" in m.value for m in at.markdown), page
        assert at.text_input(key="access_password").proto.type == 1  # a password box (characters hidden)


def test_a_wrong_password_is_refused_and_the_right_one_opens_the_app():
    store, _ = make_store()
    run(store)
    at = open_page("app.py", store, secrets={"app_password": "pw-123456"})
    at.text_input(key="access_password").input("wrong")  # (no run() between typing and pressing: form values are sent on submit)
    at.button[0].click().run()
    assert any("not right" in e.value for e in at.error) and not at.metric and ("access_granted" not in at.session_state or not at.session_state["access_granted"])
    at.text_input(key="access_password").input("pw-123456")
    at.button[0].click().run()
    assert not at.exception and metrics(at)["Total rows"] == "35"
    at.run()
    assert at.session_state["access_granted"] is True and metrics(at)["Total rows"] == "35"  # stays open for the session


def test_about_does_not_read_the_sheet_for_someone_who_has_not_entered_the_password():
    store, _ = make_store()
    store.ensure_schema()
    store.set_config("attempt_cap", 7)
    at = open_page("pages/4_About.py", store, secrets={"app_password": "pw-123456"})
    text = " ".join(m.value for m in at.markdown)
    assert not at.exception and "Showing the defaults" in text and "at most 7 counted attempts" not in text
    granted = open_page("pages/4_About.py", store, secrets={"app_password": "pw-123456"}, access_granted=True)
    assert "at most 7 counted attempts" in " ".join(m.value for m in granted.markdown)


def test_no_password_set_means_no_gate():
    store, _ = make_store()
    assert not open_page("app.py", store).exception and metrics(open_page("app.py", store))["Total rows"] == "35"


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


# ---------------------------------------------------------------- errors become messages, not stack traces


def test_a_missing_tab_gives_a_readable_message_on_every_data_page():
    store, ws = make_store()
    run(store)
    store.ensure_schema_once()
    store._client.spreadsheet._sheets[:] = [w for w in store._client.spreadsheet._sheets if w.title != "Audit_Log"]
    store._ws_list = None
    for page in ("app.py", "pages/1_Run.py", "pages/2_Human_Queue.py", "pages/3_Dashboard.py"):
        st.cache_data.clear()
        at = open_page(page, store)
        assert not at.exception, page
        assert any("Could not read the Google Sheet" in e.value for e in at.error), page
        assert not at.metric, page


def test_a_failing_write_during_an_approval_is_reported_calmly_and_without_secrets():
    store, _ = make_store(sheet_rows(2, **SAME))
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=14))
    at = open_page("pages/2_Human_Queue.py", store, actor_name="Alex", run_date=DAY1)

    def broken(*a, **k):
        raise ConnectionError("Google said no. key was " + PEM)
    store.update_rows = broken
    at.button(key="approve-same@x.de-2026-09-03").click().run()
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "That did not complete" in errors and "Some of it may have been saved" in errors
    assert "MIIEvQ" not in errors and "BEGIN PRIVATE KEY" not in errors


def test_text_from_the_sheet_cannot_inject_markdown_into_the_page():
    rows = sheet_rows(2, **SAME)
    header = rows[0]
    rows[1][header.index(Col.CUSTOMER)] = "[Click here](http://evil.example) **ACT NOW** $5"
    store, _ = make_store(rows)
    run(store, responder=forced(Outcome.PROMISE_TO_PAY, Outcome.PROMISE_TO_PAY, days=14))
    at = open_page("pages/2_Human_Queue.py", store, run_date=DAY1)
    card = next(m.value for m in at.markdown if "ACT NOW" in m.value)
    assert "[Click here](" not in card and "\\[Click here\\]" in card and "\\*\\*ACT NOW\\*\\*" in card and "\\$5" in card


def test_the_run_page_blocks_a_date_before_processed_rows_before_anyone_clicks():
    store, _ = make_store(sheet_rows(1))
    run(store, d(5), "late")
    at = open_page("pages/1_Run.py", store, run_date=d(1))
    assert not at.exception and at.button[0].disabled
    assert any("2026-08-21 is too early" in e.value and "2026-08-25" in e.value for e in at.error)
    assert len(store.read_run_log()) == 1  # nothing was started
    ok = open_page("pages/1_Run.py", store, run_date=d(5))
    assert not ok.button[0].disabled and not ok.error


def test_the_runner_still_refuses_an_early_date_if_it_is_called_directly():
    store, _ = make_store(sheet_rows(1))
    run(store, d(5), "late")
    with pytest.raises(RunDateError):
        run(store, d(1), "early")
    assert store.read_run_log().set_index("run_id").loc["early", "state"] == "FAILED"


def test_the_run_page_passes_the_name_of_whoever_pressed_run():
    store, _ = make_store(sheet_rows(1))
    at = open_page("pages/1_Run.py", store, run_date=DAY1, actor_name="Sam Rivers")
    at.button[0].click().run()
    assert not at.exception and store.read_run_log().iloc[0]["started_by"] == "Sam Rivers"


# ---------------------------------------------------------------- the schema check is not repeated for every visitor


def test_the_schema_check_runs_once_per_ten_minutes_not_once_per_visit():
    from test_sheets import FakeClient, FakeSpreadsheet, FakeWorksheet, invoice_rows
    from core.sheets import SheetStore

    class Clock:
        now = 1000.0

    rows = invoice_rows()
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    store = SheetStore(FakeClient(FakeSpreadsheet([ws])), "id", clock=lambda: Clock.now, sleeper=lambda s: None)
    assert store.ensure_schema_once().changed  # the first visit sets everything up
    reads = len([c for c in ws.calls if c[0] in ("row_values", "get_all_values", "col_values")])
    for _ in range(5):
        assert not store.ensure_schema_once().changed
    assert len([c for c in ws.calls if c[0] in ("row_values", "get_all_values", "col_values")]) == reads  # no further reads
    Clock.now += 601
    store.ensure_schema_once()
    assert len([c for c in ws.calls if c[0] in ("row_values", "get_all_values", "col_values")]) > reads


# ---------------------------------------------------------------- the repository itself


def test_secret_files_and_private_data_are_kept_out_of_git():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".streamlit/secrets.toml", "*service-account*.json", "*credentials*.json", ".env", "node_modules/", "~$*"):
        assert pattern in ignore, pattern
    # a real secrets.toml may sit in the folder for local runs; what matters is that git ignores it (asserted above)


def test_the_secrets_example_has_placeholders_only():
    example = (ROOT / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8")
    assert "PASTE_SHEET_ID_HERE" in example and "app_password" in example
    assert "MII" not in example  # no real key material
    assert not any(len(token) > 60 and token.isalnum() for token in example.split())


def test_no_code_prints_or_evaluates_anything_dangerous():
    banned = ("unsafe_allow_html", "eval(", "exec(", "os.system", "subprocess", "pickle", "print(")
    for path in list((ROOT / "core").glob("*.py")) + list((ROOT / "pages").glob("*.py")) + [ROOT / "app.py", ROOT / "app_common.py"]:
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text.replace("exec(code", ""), f"{path.name} contains {word}"


def test_streamlit_is_configured_to_hide_internals_and_usage_tracking():
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert "gatherUsageStats = false" in config and 'showErrorDetails = "type"' in config and "enableXsrfProtection = true" in config


def test_tests_never_see_the_developers_real_secrets(tmp_path):
    """Guard for tests/conftest.py: a page test with no secrets of its own must start with none at all."""
    probe = tmp_path / "probe.py"
    probe.write_text("import streamlit as st\ntry:\n    keys = sorted(st.secrets.keys())\nexcept Exception:\n    keys = []\n"
                     "st.write(', '.join(keys) if keys else 'none')\n", encoding="utf-8")
    at = AppTest.from_file(str(probe))
    at.run()
    assert [m.value for m in at.markdown] == ["none"]


def test_the_apps_first_time_decisions_are_in_the_audit_log_and_not_repeated():
    store, _ = make_store()
    run(store)
    log = store.read_audit_log()
    setup = log[log["response_text"].str.startswith("First-time setup")]
    assert len(setup) == 10  # 3 failed data quality + 1 already paid + 6 sent to a person; the 25 in progress need no entry
    assert set(setup["status_before"]) == {"NEW"} and set(setup["actor"]) == {"system"} and set(setup["contact_event_id"]) == {""}
    by_row = setup.set_index("row_key")
    assert by_row.loc["INV-2026-4471#2", "status_after"] == "DQ_FAILED" and by_row.loc["INV-2026-4471#2", "outcome"] == "DQ_FAILED"
    assert by_row.loc["INV-2026-4265#1", "status_after"] == "HANDLED" and "already paid" in by_row.loc["INV-2026-4265#1", "response_text"]
    assert by_row.loc["INV-2026-3902#1", "human_reason"] == "ATTEMPT_CAP_LEGACY" and "ATTEMPT_CAP_LEGACY" in by_row.loc["INV-2026-3902#1", "response_text"]
    assert by_row.loc["INV-2026-3980#1", "human_reason"] == "READY_NO"
    assert not set(setup["row_key"]) & {"INV-2026-4488#1", "INV-2026-4402#1"}  # rows that simply start in progress
    run(store, run_id="run-2", hour=15)
    log2 = store.read_audit_log()
    assert len(log2[log2["response_text"].str.startswith("First-time setup")]) == 10  # a second run adds none


def test_requirements_name_the_versions_the_app_was_proven_on():
    import re

    lines = [l.strip() for l in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    pins = {re.split(r"[<>=]", l)[0]: l for l in lines}
    streamlit_floor = tuple(int(x) for x in re.search(r">=([\d.]+)", pins["streamlit"]).group(1).split("."))
    assert streamlit_floor >= (1, 49), "the app uses width='stretch', which needs Streamlit 1.49 or newer"
    for name in ("streamlit", "gspread", "pandas", "plotly"):
        assert "<" in pins[name], f"{name} needs an upper bound so a future major release cannot break a fresh deploy"
    assert "tzdata" in pins and "gspread" in pins
