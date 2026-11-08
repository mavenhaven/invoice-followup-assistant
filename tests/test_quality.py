"""Tests for core/quality.py: one test group per rule, plus the real 35-row file."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from core.quality import (
    Severity, check_quality, check_row, normalize_phone, parse_amount, parse_date, parse_timestamp,
    summarize, validate_email,
)
from core.schema import Col, Config

RUN = dt.date(2026, 8, 20)
FIXTURE = Path(__file__).parent / "fixtures" / "overdue_invoices.csv"


def base_row(**overrides) -> dict[str, str]:
    """A clean, contactable invoice row. Override any column by its sheet name via a dict(**{"Email": ""})."""
    row = {
        Col.INVOICE_NUMBER: "INV-2026-1000", Col.CUSTOMER: "Alpha GmbH", Col.COUNTRY: "DE",
        Col.CONTACT_NAME: "Anna Alpha", Col.EMAIL: "anna@alpha.de", Col.PHONE: "+49 30 1234 5678",
        Col.INVOICE_DATE: "2026-06-01", Col.DUE_DATE: "2026-07-01", Col.INVOICE_AMOUNT: "1000",
        Col.AMOUNT_OUTSTANDING: "1000", Col.CURRENCY: "EUR", Col.LAST_CONTACT_DATE: "",
        Col.CONTACT_ATTEMPTS: "0", Col.READY_FOR_CONTACT: "Yes", Col.OUTCOME: "", Col.SUMMARY: "",
        Col.LAST_UPDATED: "", Col.NOTES: "",
    }
    row.update(overrides)
    return row


def codes(row: dict[str, str], **kw) -> set[str]:
    return {i.code for i in check_row(row, run_date=RUN, **kw).issues}


def issue(row: dict[str, str], code: str, **kw):
    return next(i for i in check_row(row, run_date=RUN, **kw).issues if i.code == code)


def frame(*rows: dict[str, str]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows), dtype=object)
    df[Col.ROW_KEY] = df[Col.INVOICE_NUMBER] + "#" + (df.groupby(Col.INVOICE_NUMBER).cumcount() + 1).astype(str)
    return df


# ---------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize("text, expected", [
    ("4820", 4820.0), ("12750.5", 12750.5), ("1,840,000", 1840000.0), ("4.820,50", 4820.5),
    ("4,820.50", 4820.5), ("1.234.567", 1234567.0), ("-1450", -1450.0), ("2,5", 2.5), (" 7 ", 7.0),
    ("", None), (None, None), ("abc", None), ("12-3", None), ("1.2.3", None),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_parse_date_and_timestamp():
    assert parse_date("2026-08-08") == dt.date(2026, 8, 8)
    assert parse_date("2026-8-8") == dt.date(2026, 8, 8)
    assert parse_date("2026-08-04 10:12") == dt.date(2026, 8, 4)
    assert parse_date("2026-02-30") is None and parse_date("08/08/2026") is None and parse_date("") is None
    assert parse_timestamp("2026-08-04 10:12") == dt.datetime(2026, 8, 4, 10, 12)
    assert parse_timestamp("2026-06-15 9:05") == dt.datetime(2026, 6, 15, 9, 5)  # hour without a leading zero
    assert parse_timestamp("not a time") is None


@pytest.mark.parametrize("email, ok", [
    ("k.vogel@nordbach-handels.de", True), ("a@b.co", True), ("first+tag@sub.example.org", True),
    ("t.blaha@silesiafresh", False), ("r.stan@adriaticprov..ro", False), ("", False), ("no-at-sign.de", False),
    ("two@@signs.de", False), ("a@b.c", False), (".lead@x.de", False), ("trail.@x.de", False),
    ("sp ace@x.de", False), ("a@-bad.de", False),
])
def test_validate_email(email, ok):
    assert validate_email(email) is ok


@pytest.mark.parametrize("text, country, e164, normalized, mismatch", [
    ("+49 30 5540 1182", "DE", "+493055401182", False, False),
    ("+43 3112 440 91", "AT", "+43311244091", False, False),
    ("0170 4471129", "DE", "+491704471129", True, False),
    ("0049 170 4471129", "DE", "+491704471129", False, False),
    ("+49 (0) 30 5540 1182", "DE", "+493055401182", False, False),
    ("+420 731 445 902", "DE", "+420731445902", False, True),  # valid number, wrong country
    ("(+36) 1-445-2210", "HU", "+3614452210", False, False),
])
def test_normalize_phone_valid(text, country, e164, normalized, mismatch):
    r = normalize_phone(text, country)
    assert r.valid and r.e164 == e164 and r.normalized is normalized and r.country_mismatch is mismatch


@pytest.mark.parametrize("text, country", [
    ("", "DE"), ("12345", "DE"), ("30 5540 1182", "DE"), ("0170 4471129", "XX"), ("0170 4471129", ""),
    ("+49 30 +5540", "DE"), ("+49 1234567890123456", "DE"), ("call me", "DE"),
])
def test_normalize_phone_invalid(text, country):
    assert not normalize_phone(text, country).valid


# ---------------------------------------------------------------- rules


def test_clean_row_passes_with_no_issues():
    result = check_quality(frame(base_row()), run_date=RUN)
    assert result.loc[0, "dq_status"] == "PASS" and result.loc[0, "dq_issues"] == []
    assert result.loc[0, "can_contact_email"] and result.loc[0, "can_contact_phone"]
    assert not result.loc[0, "is_excluded"]


@pytest.mark.parametrize("column", [Col.INVOICE_NUMBER, Col.CUSTOMER, Col.COUNTRY, Col.CONTACT_NAME,
                                    Col.DUE_DATE, Col.AMOUNT_OUTSTANDING, Col.CURRENCY])
def test_dq01_each_required_field_blocks_when_blank(column):
    r = check_quality(frame(base_row(**{column: "  "})), run_date=RUN)
    assert r.loc[0, "dq_status"] == "FAIL"
    assert any(i.code == "MISSING_REQUIRED" and column in i.message for i in r.loc[0, "dq_issues"])


def test_dq02_exact_duplicate_blocks_only_later_copies():
    a, b = base_row(), base_row(**{Col.NOTES: "Duplicate export row", Col.LAST_UPDATED: "2026-08-18 9:00"})
    r = check_quality(frame(a, b), run_date=RUN)
    assert r["dq_status"].tolist() == ["PASS", "FAIL"]
    assert "DUPLICATE_INVOICE" in r.loc[1, "dq_issues_text"] and "INV-2026-1000#1" in r.loc[1, "dq_issues"][0].message
    assert r.loc[1, "human_reason"] == "DQ"


def test_dq02_conflicting_duplicates_block_every_copy():
    r = check_quality(frame(base_row(), base_row(**{Col.AMOUNT_OUTSTANDING: "900"})), run_date=RUN)
    assert r["dq_status"].tolist() == ["FAIL", "FAIL"]
    assert all("DUPLICATE_INVOICE_CONFLICT" in t for t in r["dq_issues_text"])


def test_dq02_invoice_numbers_compare_case_insensitively_and_blank_is_not_a_duplicate():
    # same number in different case is found; the case differs in the copy, so it counts as a conflict
    r = check_quality(frame(base_row(**{Col.INVOICE_NUMBER: "inv-2026-1000"}), base_row()), run_date=RUN)
    assert all("DUPLICATE_INVOICE_CONFLICT" in t for t in r["dq_issues_text"])
    blanks = check_quality(frame(base_row(**{Col.INVOICE_NUMBER: ""}), base_row(**{Col.INVOICE_NUMBER: ""})), run_date=RUN)
    assert not blanks["dq_issues_text"].str.contains("DUPLICATE").any()


def test_dq03_credit_note_and_negative_amount_block_and_are_excluded():
    row = base_row(**{Col.INVOICE_NUMBER: "CN-2026-0118", Col.INVOICE_AMOUNT: "-1450", Col.AMOUNT_OUTSTANDING: "-1450"})
    r = check_quality(frame(row), run_date=RUN)
    assert r.loc[0, "dq_status"] == "FAIL" and r.loc[0, "is_excluded"]
    assert r.loc[0, "exclusion_reason"] == "NOT_AN_INVOICE" and r.loc[0, "human_reason"] == "NOT_AN_INVOICE"
    negative = base_row(**{Col.INVOICE_AMOUNT: "-5", Col.AMOUNT_OUTSTANDING: "-5"})
    assert "NOT_A_COLLECTIBLE_INVOICE" in codes(negative)
    assert "NOT_A_COLLECTIBLE_INVOICE" not in codes(base_row(**{Col.INVOICE_NUMBER: "XX-1"}), config=Config(invoice_prefix="XX-"))


def test_dq04_no_usable_channel_blocks():
    r = check_quality(frame(base_row(**{Col.EMAIL: "", Col.PHONE: ""})), run_date=RUN)
    assert r.loc[0, "dq_status"] == "FAIL" and "NO_CONTACT_CHANNEL" in r.loc[0, "dq_issues_text"]
    assert not r.loc[0, "can_contact_email"] and not r.loc[0, "can_contact_phone"]


def test_dq04_is_not_raised_for_rows_that_are_never_contacted_anyway():
    for extra in ({Col.READY_FOR_CONTACT: "No"}, {Col.OUTCOME: "PAID"}, {Col.AMOUNT_OUTSTANDING: "0"}):
        assert "NO_CONTACT_CHANNEL" not in codes(base_row(**{Col.EMAIL: "", Col.PHONE: "", **extra}))


def test_dq05_email_missing_and_invalid_warn_and_keep_the_phone_channel():
    missing = check_quality(frame(base_row(**{Col.EMAIL: ""})), run_date=RUN)
    assert missing.loc[0, "dq_status"] == "WARN" and not missing.loc[0, "can_contact_email"] and missing.loc[0, "can_contact_phone"]
    bad = check_quality(frame(base_row(**{Col.EMAIL: "x@y"})), run_date=RUN)
    assert "EMAIL_INVALID" in bad.loc[0, "dq_issues_text"] and bad.loc[0, "normalized_email"] == ""
    assert check_quality(frame(base_row(**{Col.EMAIL: " Anna@Alpha.DE "})), run_date=RUN).loc[0, "normalized_email"] == "anna@alpha.de"


def test_dq06_phone_missing_invalid_normalized_and_mismatch():
    assert "PHONE_MISSING" in codes(base_row(**{Col.PHONE: ""}))
    assert issue(base_row(**{Col.PHONE: "12345"}), "PHONE_INVALID").severity is Severity.WARN
    n = issue(base_row(**{Col.PHONE: "0170 4471129"}), "PHONE_NORMALIZED")
    assert n.severity is Severity.INFO
    r = check_quality(frame(base_row(**{Col.PHONE: "0170 4471129"})), run_date=RUN)
    assert r.loc[0, "dq_status"] == "PASS" and r.loc[0, "normalized_phone"] == "+491704471129"
    assert issue(base_row(**{Col.PHONE: "+420 731 445 902"}), "PHONE_COUNTRY_MISMATCH").severity is Severity.WARN


def test_dq07_ready_flag_blank_warns_and_is_derived_invalid_blocks():
    r = check_quality(frame(base_row(**{Col.READY_FOR_CONTACT: ""})), run_date=RUN)
    assert r.loc[0, "dq_status"] == "WARN" and r.loc[0, "ready_for_contact_derived"] == "Yes"
    no_channel = check_quality(frame(base_row(**{Col.READY_FOR_CONTACT: "", Col.EMAIL: "", Col.PHONE: ""})), run_date=RUN)
    assert no_channel.loc[0, "ready_for_contact_derived"] == "No"
    maybe = check_quality(frame(base_row(**{Col.READY_FOR_CONTACT: "maybe"})), run_date=RUN)
    assert maybe.loc[0, "dq_status"] == "FAIL" and "READY_FLAG_INVALID" in maybe.loc[0, "dq_issues_text"]
    assert check_quality(frame(base_row(**{Col.READY_FOR_CONTACT: "YES"})), run_date=RUN).loc[0, "dq_status"] == "PASS"


def test_dq08_ready_no_is_info_excluded_and_not_a_failure():
    r = check_quality(frame(base_row(**{Col.READY_FOR_CONTACT: "No", Col.NOTES: "On hold pending contract renegotiation."})), run_date=RUN)
    assert r.loc[0, "dq_status"] == "PASS" and r.loc[0, "is_excluded"]
    assert r.loc[0, "exclusion_reason"] == "READY_NO" and r.loc[0, "human_reason"] == "READY_NO"
    assert issue(base_row(**{Col.READY_FOR_CONTACT: "No", Col.NOTES: "On hold"}), "READY_FLAG_NO").message == "On hold"


@pytest.mark.parametrize("extra", [{Col.OUTCOME: "PAID", Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-11"},
                                   {Col.AMOUNT_OUTSTANDING: "0"}])
def test_dq09_settled_rows_are_excluded_not_failed(extra):
    r = check_quality(frame(base_row(**extra)), run_date=RUN)
    assert r.loc[0, "exclusion_reason"] == "SETTLED" and r.loc[0, "dq_status"] != "FAIL"


def test_settled_takes_precedence_over_ready_no():
    r = check_quality(frame(base_row(**{Col.AMOUNT_OUTSTANDING: "0", Col.READY_FOR_CONTACT: "No"})), run_date=RUN)
    assert r.loc[0, "exclusion_reason"] == "SETTLED"


def test_dq10_amount_problems_block():
    assert "AMOUNT_INVALID" in codes(base_row(**{Col.AMOUNT_OUTSTANDING: "lots"}))
    assert "AMOUNT_INVALID" in codes(base_row(**{Col.INVOICE_AMOUNT: "n/a"}))
    assert "AMOUNT_INVALID" in codes(base_row(**{Col.AMOUNT_OUTSTANDING: "1200"}))  # more than the invoice amount
    assert "AMOUNT_INVALID" not in codes(base_row(**{Col.AMOUNT_OUTSTANDING: "250"}))  # partial payment is fine
    assert "AMOUNT_INVALID" not in codes(base_row(**{Col.INVOICE_AMOUNT: "1000", Col.AMOUNT_OUTSTANDING: "1,000"}))


def test_dq11_amount_below_minimum_warns_and_can_exclude():
    tiny = base_row(**{Col.INVOICE_AMOUNT: "2.5", Col.AMOUNT_OUTSTANDING: "2.5"})
    assert issue(tiny, "AMOUNT_BELOW_MINIMUM").severity is Severity.WARN
    assert not check_row(tiny, run_date=RUN).exclusion_reason
    assert check_row(tiny, run_date=RUN, config=Config(skip_below_minimum=True)).exclusion_reason == "BELOW_MINIMUM"
    assert "AMOUNT_BELOW_MINIMUM" not in codes(base_row(**{Col.AMOUNT_OUTSTANDING: "0"}))  # zero is settled, not tiny
    huf = base_row(**{Col.COUNTRY: "HU", Col.CURRENCY: "HUF", Col.INVOICE_AMOUNT: "3000", Col.AMOUNT_OUTSTANDING: "3000"})
    assert "AMOUNT_BELOW_MINIMUM" in codes(huf)  # 3000 HUF is under 4000


def test_dq12_dates():
    assert "DATE_INVALID" in codes(base_row(**{Col.DUE_DATE: "31/07/2026"}))
    assert "DATE_INVALID" in codes(base_row(**{Col.INVOICE_DATE: "yesterday"}))
    assert issue(base_row(**{Col.DUE_DATE: "2026-05-01"}), "DATE_INVALID").message == "Due Date is before Invoice Date"
    assert "DATE_INVALID" not in codes(base_row(**{Col.INVOICE_DATE: ""}))
    assert "PAYMENT_TERMS_UNUSUAL" not in codes(base_row(**{Col.DUE_DATE: "2026-07-31"}))  # 60 days is allowed
    assert issue(base_row(**{Col.DUE_DATE: "2026-06-03"}), "PAYMENT_TERMS_UNUSUAL").severity is Severity.INFO
    assert "PAYMENT_TERMS_UNUSUAL" in codes(base_row(**{Col.DUE_DATE: "2026-12-01"}))


def test_dq13_country_and_currency():
    assert "COUNTRY_INVALID" in codes(base_row(**{Col.COUNTRY: "FR"}))
    assert "CURRENCY_INVALID" in codes(base_row(**{Col.CURRENCY: "USD"}))
    assert issue(base_row(**{Col.CURRENCY: "CZK"}), "CURRENCY_COUNTRY_MISMATCH").severity is Severity.WARN
    assert not codes(base_row(**{Col.COUNTRY: "de", Col.CURRENCY: "eur"}))  # case-insensitive
    assert check_quality(frame(base_row(**{Col.COUNTRY: "de"})), run_date=RUN).loc[0, "country"] == "DE"


def test_dq14_not_yet_due_is_info_and_excluded():
    row = base_row(**{Col.DUE_DATE: "2026-08-29", Col.INVOICE_DATE: "2026-07-30"})
    r = check_quality(frame(row), run_date=RUN)
    assert r.loc[0, "dq_status"] == "PASS" and r.loc[0, "exclusion_reason"] == "NOT_YET_DUE"
    assert check_quality(frame(row), run_date=dt.date(2026, 8, 29)).loc[0, "exclusion_reason"] == ""  # due today is due


def test_dq15_history_checks():
    assert "HISTORY_INCONSISTENT" in codes(base_row(**{Col.CONTACT_ATTEMPTS: "2", Col.OUTCOME: "NO_ANSWER"}))  # no last contact
    assert "HISTORY_INCONSISTENT" in codes(base_row(**{Col.CONTACT_ATTEMPTS: "4", Col.LAST_CONTACT_DATE: "2026-05-06"}))  # no outcome
    assert "HISTORY_INCONSISTENT" in codes(base_row(**{Col.OUTCOME: "NO_ANSWER"}))  # outcome but 0 attempts
    assert "HISTORY_INCONSISTENT" in codes(base_row(**{Col.CONTACT_ATTEMPTS: "1", Col.OUTCOME: "PAID", Col.LAST_CONTACT_DATE: "2026-08-11",
                                                       Col.LAST_UPDATED: "2026-08-01 9:00"}))
    assert "ATTEMPTS_INVALID" in codes(base_row(**{Col.CONTACT_ATTEMPTS: "two"}))
    ok = base_row(**{Col.CONTACT_ATTEMPTS: "1", Col.OUTCOME: "NO_ANSWER", Col.LAST_CONTACT_DATE: "2026-08-04",
                     Col.LAST_UPDATED: "2026-08-04 10:12", Col.SUMMARY: "Called 4 Aug, no answer."})
    assert "HISTORY_INCONSISTENT" not in codes(ok)
    assert "HISTORY_INCONSISTENT" not in codes({**ok, Col.LAST_UPDATED: "2026-08-04 9:05"})  # hour without a leading zero


def test_unknown_outcome_is_reported_not_raised():
    r = check_quality(frame(base_row(**{Col.OUTCOME: "MAYBE_LATER", Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-04"})), run_date=RUN)
    assert "OUTCOME_UNKNOWN" in r.loc[0, "dq_issues_text"] and r.loc[0, "dq_status"] == "WARN"


@pytest.mark.parametrize("summary, email, phone, legacy", [
    ("Called 4 Aug, no answer, no voicemail left.", 0, 3, 0),
    ("Called yesterday, line busy.", 0, 3, 0),
    ("Auto-reply: back 1 Sep 2026.", 3, 0, 0),
    ("Emailed twice and phoned once.", 0, 0, 3),  # both channels mentioned: unknown
    ("Asked to be called back after 15 Aug.", 0, 0, 3),  # "called back" is the contact's request
    ("Two attempts, both unanswered.", 0, 0, 3),
    ("", 0, 0, 3),
])
def test_dq16_legacy_attempts_are_mapped_by_summary(summary, email, phone, legacy):
    r = check_quality(frame(base_row(**{Col.CONTACT_ATTEMPTS: "3", Col.OUTCOME: "NO_ANSWER", Col.LAST_CONTACT_DATE: "2026-08-04", Col.SUMMARY: summary})), run_date=RUN)
    row = r.iloc[0]
    assert (row["suggested_email_attempts"], row["suggested_phone_attempts"], row["suggested_legacy_attempts"]) == (email, phone, legacy)
    assert ("LEGACY_ATTEMPTS_NO_CHANNEL" in row["dq_issues_text"]) == bool(legacy)
    assert row["dq_status"] == ("WARN" if legacy else "PASS")


def test_dq16_five_unknown_channel_attempts_suggest_a_human():
    base = {Col.OUTCOME: "NO_ANSWER", Col.LAST_CONTACT_DATE: "2026-04-18", Col.SUMMARY: "Five attempts since Feb, no contact established."}
    five = check_quality(frame(base_row(**{**base, Col.CONTACT_ATTEMPTS: "5"})), run_date=RUN).iloc[0]
    assert five["suggested_human_reason"] == "ATTEMPT_CAP_LEGACY"
    four = check_quality(frame(base_row(**{**base, Col.CONTACT_ATTEMPTS: "4"})), run_date=RUN).iloc[0]
    assert four["suggested_human_reason"] == ""
    custom = check_quality(frame(base_row(**{**base, Col.CONTACT_ATTEMPTS: "4"})), run_date=RUN, config=Config(attempt_cap=4)).iloc[0]
    assert custom["suggested_human_reason"] == "ATTEMPT_CAP_LEGACY"


def test_dq16_is_skipped_once_the_new_counters_are_filled():
    row = base_row(**{Col.CONTACT_ATTEMPTS: "3", Col.OUTCOME: "NO_ANSWER", Col.LAST_CONTACT_DATE: "2026-08-04", Col.PHONE_ATTEMPTS: "3"})
    assert not {"LEGACY_ATTEMPTS_NO_CHANNEL", "LEGACY_ATTEMPTS_MAPPED"} & codes(row)


def test_dq17_contact_shared_across_rows_is_info_and_counts_only_open_rows():
    shared = [base_row(**{Col.INVOICE_NUMBER: f"INV-{n}"}) for n in (1, 2, 3)]
    r = check_quality(frame(*shared), run_date=RUN)
    assert r["contact_rows"].tolist() == [3, 3, 3] and r["dq_status"].tolist() == ["PASS"] * 3
    assert r["dq_issues_text"].str.contains("CONTACT_SHARED_ACROSS_ROWS").all()
    with_failed = check_quality(frame(*shared[:2], base_row(**{Col.INVOICE_NUMBER: "CN-3", Col.AMOUNT_OUTSTANDING: "-1", Col.INVOICE_AMOUNT: "-1"})), run_date=RUN)
    assert with_failed["contact_rows"].tolist()[:2] == [2, 2] and with_failed.loc[2, "dq_status"] == "FAIL"


def test_contact_key_prefers_email_then_phone_then_name():
    r = check_quality(frame(
        base_row(**{Col.INVOICE_NUMBER: "A"}),
        base_row(**{Col.INVOICE_NUMBER: "B", Col.EMAIL: ""}),
        base_row(**{Col.INVOICE_NUMBER: "C", Col.EMAIL: "", Col.PHONE: ""}),
    ), run_date=RUN)
    assert r["contact_key"].tolist() == ["anna@alpha.de", "+493012345678", "anna alpha|alpha gmbh"]


@pytest.mark.parametrize("customer, country, flagged", [
    ("Silesia Fresh Sp. z o.o.", "CZ", True), ("Alpha GmbH", "DE", False), ("Alpha GmbH", "AT", False),
    ("Alpha GmbH", "HU", True), ("Pramen Distribuce s.r.o.", "CZ", False), ("Ostrava Gastro a.s.", "CZ", False),
    ("Kekes Kereskedelmi Kft.", "HU", False), ("Dunarea Trade SRL", "RO", False), ("Plain Name", "DE", False),
])
def test_dq18_entity_suffix_versus_country(customer, country, flagged):
    cur = {"DE": "EUR", "AT": "EUR", "CZ": "CZK", "HU": "HUF", "RO": "RON"}[country]
    row = base_row(**{Col.CUSTOMER: customer, Col.COUNTRY: country, Col.CURRENCY: cur, Col.PHONE: "", Col.INVOICE_AMOUNT: "100000", Col.AMOUNT_OUTSTANDING: "100000"})
    found = "ENTITY_SUFFIX_MISMATCH" in codes(row)
    assert found is flagged


# ---------------------------------------------------------------- structure


def test_missing_invoice_number_column_raises_a_clear_error():
    with pytest.raises(ValueError, match="Invoice Number"):
        check_quality(pd.DataFrame({"x": ["1"]}), run_date=RUN)


def test_empty_frame_and_row_order_and_index_are_preserved():
    df = frame(base_row(**{Col.INVOICE_NUMBER: "B"}), base_row(**{Col.INVOICE_NUMBER: "A"}))
    df.index = [10, 20]
    r = check_quality(df, run_date=RUN)
    assert list(r.index) == [10, 20] and r["invoice_number"].tolist() == ["B", "A"]
    empty = check_quality(frame(base_row()).iloc[0:0], run_date=RUN)
    assert len(empty) == 0 and summarize(empty).total == 0


def test_summarize_counts_add_up():
    df = frame(base_row(), base_row(**{Col.INVOICE_NUMBER: "INV-X", Col.EMAIL: ""}),
               base_row(**{Col.INVOICE_NUMBER: "INV-Y", Col.EMAIL: "", Col.PHONE: ""}))
    s = summarize(check_quality(df, run_date=RUN))
    assert s.total == 3 and s.by_status == {"PASS": 1, "WARN": 1, "FAIL": 1} and sum(s.by_status.values()) == s.total
    assert set(s.by_country.columns) == {"PASS", "WARN", "FAIL"} and s.by_country.loc["DE"].sum() == 3
    assert {"rule", "code", "severity", "issues", "rows"} <= set(s.issues.columns)
    assert (s.issues["severity"] == "BLOCK").sum() == 1


# ---------------------------------------------------------------- the real file


@pytest.fixture(scope="module")
def real() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, dtype=str, keep_default_na=False)
    df[Col.ROW_KEY] = df[Col.INVOICE_NUMBER] + "#" + (df.groupby(Col.INVOICE_NUMBER).cumcount() + 1).astype(str)
    return check_quality(df, run_date=RUN).set_index("row_key")


def test_real_file_every_row_appears_once_and_counts_add_up_to_35(real):
    assert len(real) == 35 and real.index.is_unique
    s = summarize(real.reset_index())
    assert s.by_status == {"PASS": 16, "WARN": 16, "FAIL": 3}
    assert sum(s.by_status.values()) == 35


def test_real_file_blocked_rows(real):
    assert set(real.index[real["dq_status"] == "FAIL"]) == {"INV-2026-4471#2", "CN-2026-0118#1", "INV-2026-4144#1"}
    assert "DUPLICATE_INVOICE" in real.loc["INV-2026-4471#2", "dq_issues_text"]
    assert real.loc["INV-2026-4471#1", "dq_status"] == "PASS"  # the master copy keeps going
    assert real.loc["CN-2026-0118#1", "human_reason"] == "NOT_AN_INVOICE"
    assert "NO_CONTACT_CHANNEL" in real.loc["INV-2026-4144#1", "dq_issues_text"]


def test_real_file_email_and_phone_findings(real):
    no_email = set(real.index[~real["can_contact_email"]])
    assert no_email == {"INV-2026-4301#1", "INV-2026-4276#1", "INV-2026-4066#1", "INV-2026-4144#1"}
    assert real.loc[["INV-2026-4301#1", "INV-2026-4276#1", "INV-2026-4066#1"], "can_contact_phone"].all()
    assert set(real.index[~real["can_contact_phone"]]) == {"INV-2026-4144#1"}  # only the row with no phone at all
    assert real.loc["INV-2026-4189#1", "normalized_phone"] == "+491704471129"
    assert "PHONE_NORMALIZED" in real.loc["INV-2026-4189#1", "dq_issues_text"] and real.loc["INV-2026-4189#1", "dq_status"] == "PASS"


def test_real_file_warnings_and_exclusions(real):
    assert "READY_FLAG_BLANK" in real.loc["INV-2026-4362#1", "dq_issues_text"]
    assert real.loc["INV-2026-4362#1", "ready_for_contact_derived"] == "Yes"
    assert "AMOUNT_BELOW_MINIMUM" in real.loc["INV-2026-4207#1", "dq_issues_text"]
    assert "ENTITY_SUFFIX_MISMATCH" in real.loc["INV-2026-4276#1", "dq_issues_text"]
    excluded = real[real["is_excluded"]]["exclusion_reason"].to_dict()
    assert excluded == {"INV-2026-4265#1": "SETTLED", "INV-2026-3980#1": "READY_NO", "INV-2026-4415#1": "READY_NO",
                        "INV-2026-4512#1": "NOT_YET_DUE", "CN-2026-0118#1": "NOT_AN_INVOICE"}
    for key in ("INV-2026-4265#1", "INV-2026-3980#1", "INV-2026-4415#1", "INV-2026-4512#1"):
        assert real.loc[key, "dq_status"] != "FAIL"
    assert real.loc["INV-2026-3980#1", "human_reason"] == "READY_NO"


def test_real_file_history_and_legacy_attempts(real):
    assert "HISTORY_INCONSISTENT" in real.loc["INV-2026-3980#1", "dq_issues_text"]  # 4 attempts but no Outcome
    assert real["dq_issues_text"].str.contains("HISTORY_INCONSISTENT").sum() == 1
    r = real.loc["INV-2026-3902#1"]
    assert (r["suggested_legacy_attempts"], r["suggested_human_reason"]) == (5, "ATTEMPT_CAP_LEGACY")
    for key in ("INV-2026-4402#1", "INV-2026-4444#1"):
        assert real.loc[key, "suggested_phone_attempts"] == 1 and real.loc[key, "suggested_legacy_attempts"] == 0
    assert real.loc["INV-2026-4118#1", "suggested_email_attempts"] == 1
    assert real.loc["INV-2026-4054#1", "suggested_legacy_attempts"] == 3  # "called back" is not a call we made
    assert (real["suggested_human_reason"] == "ATTEMPT_CAP_LEGACY").sum() == 1


def test_real_file_contacts_with_several_rows(real):
    veltner = real[real["contact_key"] == "t.berger@veltner-gastro.at"]
    assert len(veltner) == 3 and (veltner["contact_rows"] == 3).all()
    nordbach = real[real["contact_key"] == "k.vogel@nordbach-handels.de"]
    # 3 sheet rows (one is the blocked duplicate) but only 2 open rows; every row reports the open count
    assert len(nordbach) == 3 and set(nordbach["contact_rows"]) == {2}
    assert real.loc["INV-2026-4133#1", "dq_status"] == "PASS"  # 60-day terms are fine


def test_real_file_summary_by_country_adds_up(real):
    s = summarize(real.reset_index())
    assert set(s.by_country.index) == {"AT", "CZ", "DE", "HU", "RO"} and s.by_country.to_numpy().sum() == 35
    assert s.excluded_by_reason == {"READY_NO": 2, "SETTLED": 1, "NOT_AN_INVOICE": 1, "NOT_YET_DUE": 1}


# ---------------------------------------------------------------- stability once the app has worked on rows


def test_a_duplicate_stays_a_plain_duplicate_after_the_first_copy_is_worked_on():
    worked = base_row(**{Col.OUTCOME: "NO_RESPONSE", Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-20",
                         Col.AMOUNT_OUTSTANDING: "750", Col.LAST_UPDATED: "2026-08-20 9:00", Col.EMAIL_ATTEMPTS: "1"})
    copy = base_row(**{Col.OUTCOME: "DQ_FAILED", Col.EMAIL_ATTEMPTS: "0"})
    r = check_quality(frame(worked, copy), run_date=RUN)
    assert r["dq_status"].tolist() == ["PASS", "FAIL"] and "DUPLICATE_INVOICE_CONFLICT" not in r.loc[1, "dq_issues_text"]


def test_a_real_difference_in_a_stable_column_is_still_a_conflict_after_processing():
    worked = base_row(**{Col.OUTCOME: "NO_RESPONSE", Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-20"})
    other = base_row(**{Col.CUSTOMER: "Someone Else GmbH", Col.OUTCOME: "DQ_FAILED"})
    assert check_quality(frame(worked, other), run_date=RUN)["dq_status"].tolist() == ["FAIL", "FAIL"]


def test_history_checks_are_skipped_for_rows_the_app_manages():
    covered = base_row(**{Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-20", Col.EMAIL_ATTEMPTS: "1"})  # no Outcome
    assert "HISTORY_INCONSISTENT" not in codes(covered)
    legacy = base_row(**{Col.CONTACT_ATTEMPTS: "1", Col.LAST_CONTACT_DATE: "2026-08-20"})
    assert "HISTORY_INCONSISTENT" in codes(legacy)  # still reported until the row is migrated
    assert "HISTORY_INCONSISTENT" in codes({**covered, Col.LAST_CONTACT_DATE: "not a date"})  # date sanity always applies
