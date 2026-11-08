"""Tests for core/metrics.py: contact events, KPIs, status counts, filters, drill-down and themes."""
from __future__ import annotations


import pandas as pd
import pytest
from test_runner import DAY1, d, forced, make_store, run, sheet_rows

from core.metrics import (
    EVENT_COLUMNS, attempt_events, attempts_per_day, channel_comparison, contact_history, contact_options, filter_events,
    filter_invoices, kpis, outcome_mix, outstanding_by_currency, reply_text, status_counts, success_by_country, theme_counts,
    top_words,
)
from core.schema import Col
from core.taxonomy import Outcome


def event(**kw) -> dict:
    base = dict(event_id="e1", run_date=DAY1, contact_key="a@x.de", channel="email", outcome="NO_RESPONSE", category="No contact",
                counted=True, real_response=False, success=False, failure=True, system_error=False, attempt_no=1,
                primary_row_key="INV-1#1", row_keys=["INV-1#1"], country="DE", customer="Alpha GmbH", contact_name="Anna",
                response_text="No reply received.", promised_date=None)
    base.update(kw)
    return base


def frame(*events: dict) -> pd.DataFrame:
    return pd.DataFrame(list(events), columns=EVENT_COLUMNS)


# ---------------------------------------------------------------- events from the audit log


def test_no_audit_gives_an_empty_event_table():
    store, _ = make_store(sheet_rows(1))
    store.ensure_schema()
    ev = attempt_events(store.read_audit_log(), store.read_invoices())
    assert ev.empty and list(ev.columns) == EVENT_COLUMNS
    assert attempt_events(None, store.read_invoices()).empty


def test_a_combined_contact_is_one_event_with_all_its_invoices():
    store, _ = make_store()
    run(store, responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    ev = attempt_events(store.read_audit_log(), store.read_invoices())
    log = store.read_audit_log()
    assert len(ev) == log["contact_event_id"].replace("", pd.NA).nunique()
    assert int(ev["counted"].sum()) == (log["counted_as_attempt"] == "Yes").sum()
    veltner = ev[ev["contact_key"] == "t.berger@veltner-gastro.at"].iloc[0]
    assert sorted(veltner["row_keys"]) == ["INV-2026-4288#1", "INV-2026-4351#1", "INV-2026-4390#1"]
    assert veltner["primary_row_key"] == "INV-2026-4288#1" and veltner["counted"] and veltner["country"] == "AT"
    assert veltner["customer"] == "Veltner Gastro GmbH" and veltner["run_date"] == DAY1
    assert not veltner["response_text"].startswith("Covered by")  # the contact's own words, not the pointer


def test_outcome_flags_follow_the_taxonomy():
    cases = [(Outcome.PROMISE_TO_PAY, dict(real_response=True, success=True, failure=False, system_error=False, counted=True)),
             (Outcome.PAID, dict(real_response=True, success=True, failure=False, system_error=False, counted=True)),
             (Outcome.DISPUTE, dict(real_response=True, success=False, failure=False, system_error=False, counted=True)),
             (Outcome.NO_RESPONSE, dict(real_response=False, success=False, failure=True, system_error=False, counted=True)),
             (Outcome.OUT_OF_OFFICE, dict(real_response=False, success=False, failure=True, system_error=False, counted=True)),
             (Outcome.EMAIL_BOUNCE_HARD, dict(real_response=False, success=False, failure=True, system_error=False, counted=True))]
    for outcome, expect in cases:
        store, _ = make_store(sheet_rows(1))
        run(store, responder=forced(outcome, outcome, days=3))
        row = attempt_events(store.read_audit_log(), store.read_invoices()).iloc[0]
        assert {k: bool(row[k]) for k in expect} == expect, outcome


def test_a_system_error_is_an_event_that_is_not_an_attempt():
    store, _ = make_store(sheet_rows(1))

    def broken(*a, **k):
        raise RuntimeError("gateway down")
    run(store, responder=broken)
    ev = attempt_events(store.read_audit_log(), store.read_invoices())
    assert len(ev) == 1 and ev.iloc[0]["outcome"] == "SYSTEM_ERROR" and ev.iloc[0]["category"] == "System error"
    assert bool(ev.iloc[0]["system_error"]) and not bool(ev.iloc[0]["counted"])
    k = kpis(ev, store.read_invoices())
    assert (k.attempts, k.system_errors, k.events, k.system_error_rate) == (0, 1, 1, 1.0) and k.response_rate is None


# ---------------------------------------------------------------- KPIs and status counts


def test_kpi_arithmetic_on_a_hand_built_frame():
    ev = frame(
        *[event(event_id=f"r{i}", real_response=True, success=i < 2, failure=False, outcome="PROMISE_TO_PAY", category="Contact made") for i in range(4)],
        *[event(event_id=f"f{i}") for i in range(3)],  # failures
        *[event(event_id=f"n{i}", failure=False, outcome="NO_RESPONSE") for i in range(3)],  # counted, neither response nor failure
        event(event_id="s1", counted=False, failure=False, system_error=True, outcome="SYSTEM_ERROR", category="System error"),
        event(event_id="s2", counted=False, failure=False, system_error=True, outcome="SYSTEM_ERROR", category="System error"))
    inv = pd.DataFrame({Col.STATUS: ["NEEDS_HUMAN", "DQ_FAILED", "IN_PROGRESS", "HANDLED"]})
    k = kpis(ev, inv)
    assert (k.attempts, k.responses, k.successes, k.failures, k.system_errors, k.events) == (10, 4, 2, 3, 2, 12)
    assert (k.response_rate, k.success_rate, k.failure_rate) == (0.4, 0.2, 0.3)
    assert k.system_error_rate == pytest.approx(2 / 12)
    assert (k.human_flagged, k.dq_failed, k.needs_human, k.rows, k.human_rate) == (2, 1, 1, 4, 0.5)


def test_kpis_with_no_events_have_no_rates_rather_than_zero_division():
    k = kpis(frame(), pd.DataFrame({Col.STATUS: []}))
    assert (k.attempts, k.response_rate, k.success_rate, k.failure_rate, k.system_error_rate, k.human_rate) == (0, None, None, None, None, None)


def test_status_counts_add_up_and_unknown_statuses_are_flagged():
    inv = pd.DataFrame({Col.STATUS: ["IN_PROGRESS", "", "NEW", "NEEDS_HUMAN", "DQ_FAILED", "HANDLED", "HANDLED"]})
    sc = status_counts(inv)
    assert sc.buckets == {"in_progress": 3, "needs_human": 1, "dq_failed": 1, "handled": 2, "other": 0} and sc.total == 7 and sc.reconciled
    odd = status_counts(pd.DataFrame({Col.STATUS: ["IN_PROGRESS", "WHATEVER"]}))
    assert odd.buckets["other"] == 1 and not odd.reconciled
    assert status_counts(pd.DataFrame({Col.STATUS: []})).reconciled


def test_status_counts_on_the_real_file_after_a_run():
    store, _ = make_store()
    run(store)
    inv = store.read_invoices()
    sc = status_counts(inv)
    assert sc.total == 35 and sc.reconciled and sc.buckets["dq_failed"] == 3
    assert sum(v for k, v in sc.buckets.items() if k != "other") == 35


# ---------------------------------------------------------------- filters


def inv_frame() -> pd.DataFrame:
    cols = [Col.ROW_KEY, Col.INVOICE_NUMBER, Col.CUSTOMER, Col.CONTACT_NAME, Col.EMAIL, Col.COUNTRY]
    return pd.DataFrame([["A#1", "INV-1", "Alpha GmbH", "Anna Alpha", "anna@alpha.de", "DE"],
                         ["B#1", "INV-2", "Beta s.r.o.", "Boris Beta", "boris@beta.cz", "CZ"],
                         ["C#1", "INV-3", "Gamma GmbH", "Greta Gamma", "greta@gamma.at", "AT"]], columns=cols)


def test_filter_by_country_and_by_text():
    inv = inv_frame()
    assert list(filter_invoices(inv, ["DE", "AT"])[Col.ROW_KEY]) == ["A#1", "C#1"]
    assert len(filter_invoices(inv, [])) == 3 and len(filter_invoices(inv, None)) == 3
    for text, expect in [("alpha", ["A#1"]), ("BORIS", ["B#1"]), ("gamma.at", ["C#1"]), ("inv-2", ["B#1"]), ("gmbh", ["A#1", "C#1"]), ("  ", ["A#1", "B#1", "C#1"]), ("zzz", [])]:
        assert list(filter_invoices(inv, [], text)[Col.ROW_KEY]) == expect, text
    assert list(filter_invoices(inv, ["DE"], "gmbh")[Col.ROW_KEY]) == ["A#1"]  # filters combine


def test_an_event_stays_when_any_invoice_it_covered_is_in_view():
    ev = frame(event(event_id="e1", row_keys=["A#1", "B#1"]), event(event_id="e2", row_keys=["C#1"]))
    assert list(filter_events(ev, ["B#1"])["event_id"]) == ["e1"]
    assert list(filter_events(ev, ["A#1", "C#1"])["event_id"]) == ["e1", "e2"] and filter_events(ev, []).empty
    assert filter_events(frame(), ["A#1"]).empty


# ---------------------------------------------------------------- chart tables


def test_attempts_per_day_counts_only_attempts_and_splits_by_channel():
    ev = frame(event(event_id="1"), event(event_id="2"), event(event_id="3", channel="phone"),
               event(event_id="4", run_date=d(1)), event(event_id="5", counted=False, system_error=True))
    out = attempts_per_day(ev)
    assert {(r.run_date, r.channel): r.attempts for r in out.itertuples()} == {(DAY1, "email"): 2, (DAY1, "phone"): 1, (d(1), "email"): 1}
    assert attempts_per_day(frame()).empty


def test_outcome_mix_is_ranked_and_keeps_the_category():
    ev = frame(event(event_id="1"), event(event_id="2"), event(event_id="3", outcome="PAID", category="Contact made"),
               event(event_id="4", outcome="SYSTEM_ERROR", category="System error", counted=False))
    out = outcome_mix(ev)
    assert list(out["outcome"]) == ["NO_RESPONSE", "PAID", "SYSTEM_ERROR"] and list(out["events"]) == [2, 1, 1]
    assert dict(zip(out["outcome"], out["category"]))["PAID"] == "Contact made"


def test_channel_comparison_always_lists_both_channels():
    ev = frame(event(event_id="1", real_response=True, success=True, failure=False), event(event_id="2"))
    out = channel_comparison(ev).set_index("channel")
    assert list(out.index) == ["email", "phone"]
    assert (out.loc["email", "attempts"], out.loc["email", "response_rate"], out.loc["email", "failure_rate"]) == (2, 0.5, 0.5)
    assert out.loc["phone", "attempts"] == 0 and pd.isna(out.loc["phone", "success_rate"])
    empty = channel_comparison(frame())
    assert list(empty["attempts"]) == [0, 0]


def test_success_by_country_ranks_by_rate_then_volume():
    ev = frame(event(event_id="1", country="DE", success=True, failure=False, real_response=True), event(event_id="2", country="DE"),
               event(event_id="3", country="AT", success=True, failure=False, real_response=True),
               event(event_id="4", country="CZ", counted=False))
    out = success_by_country(ev)
    assert list(out["country"]) == ["AT", "DE"] and list(out["success_rate"]) == [1.0, 0.5] and list(out["attempts"]) == [1, 2]
    assert success_by_country(frame()).empty


def test_outstanding_is_summed_within_a_currency_only():
    store, _ = make_store()
    run(store)
    inv = store.read_invoices()
    out = outstanding_by_currency(inv)
    assert set(out["currency"]) == {"EUR", "CZK", "HUF", "RON"}
    for cur in ("EUR", "CZK", "HUF", "RON"):
        expected = sum(float(str(v).replace(",", "")) for v, c in zip(inv[Col.AMOUNT_OUTSTANDING], inv[Col.CURRENCY]) if c == cur)
        assert out[out["currency"] == cur]["outstanding"].sum() == pytest.approx(expected)
        assert out[out["currency"] == cur]["rows"].sum() == (inv[Col.CURRENCY] == cur).sum()
    assert outstanding_by_currency(inv.iloc[0:0]).empty


# ---------------------------------------------------------------- drill-down and themes


def test_contact_options_and_history_for_the_veltner_contact():
    store, _ = make_store()
    for n in range(3):
        run(store, d(n * 3), f"run-{n}", responder=forced(Outcome.NO_RESPONSE, Outcome.NO_ANSWER))
    inv, audit = store.read_invoices(), store.read_audit_log()
    opts = contact_options(inv).set_index("contact_key")
    assert opts.loc["t.berger@veltner-gastro.at", "label"] == "Veltner Gastro GmbH · Thomas Berger (3 invoices)"
    assert opts.loc["a.toth@kekeskft.hu", "label"].endswith("(1 invoice)")
    hist = contact_history(attempt_events(audit, inv), "t.berger@veltner-gastro.at")
    assert list(hist["date"]) == [d(0), d(3), d(6)] and list(hist["attempt"]) == [1, 2, 3] and set(hist["channel"]) == {"email"}
    assert hist.loc[0, "invoices"] == "INV-2026-4288, INV-2026-4351, INV-2026-4390" and hist["counted"].all()
    assert contact_history(attempt_events(audit, inv), "nobody").empty


def test_reply_text_keeps_only_what_the_contact_said():
    email = "Subject: RE: Overdue invoice INV-1\n\nHello,\n\nWe will pay by 14 Sep 2026.\n\nBest regards,\nAnna\nAlpha GmbH"
    assert reply_text(email, "email") == "We will pay by 14 Sep 2026."
    phone = "Agent: Hello, calling about INV-1.\nContact: It is approved.\nAgent: Thanks.\nContact: Goodbye."
    assert reply_text(phone, "phone") == "It is approved. Goodbye."
    assert reply_text("No reply received.", "email") == "" and reply_text("Covered by the contact of X", "email") == "" and reply_text("", "phone") == ""
    assert "[call placed" not in reply_text("Agent: [call placed; rang 8 times; no answer]", "phone")


def history(*pairs: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"reply": [p[0] for p in pairs], "channel": [p[1] for p in pairs]})


def test_themes_are_found_in_realistic_replies():
    h = history(
        ("Subject: RE: x\n\nHello,\n\nInvoice INV-1 is approved and will be included in our payment run on 14 Sep 2026.\n\nRegards,\nA\nB GmbH", "email"),
        ("Agent: Hi\nContact: We are not paying that one. 40 crates were never delivered.\nAgent: ok", "phone"),
        ("Automatic reply: I am out of the office until 7 Sep 2026.", "email"),
        ("Mail Delivery Subsystem: 550 5.1.1 The email account x@y.de does not exist. Message not delivered.", "email"),
        ("No reply received.", "email"),
        ("Agent: [call placed to +49 30; rang 8 times; no answer]", "phone"))
    got = dict(theme_counts(h).itertuples(index=False, name=None))
    assert got["Will pay on a date"] == 1 and got["Disputes the invoice"] == 1 and got["Away or out of office"] == 1
    assert got["Email did not arrive"] == 1
    assert "Could not reach" not in got  # the agent's own bracketed note is not something the contact said
    assert theme_counts(history()).empty and theme_counts(history(("No reply received.", "email"))).empty


def test_top_words_skip_greetings_and_filler():
    h = history(("Subject: x\n\nHello,\n\nPlease note the shipment shipment arrived damaged.\n\nKind regards,\nAnna\nAlpha", "email"),
                ("Agent: hi\nContact: The shipment was damaged again.", "phone"))
    words = dict(top_words(h).itertuples(index=False, name=None))
    assert words["shipment"] == 3 and words["damaged"] == 2
    assert not {"hello", "please", "regards", "kind"} & set(words) and top_words(history()).empty
