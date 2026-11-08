"""Row-by-row data quality engine (rules DQ-01 to DQ-18, see prompt-pack.md and docs/DESIGN.md).

Pure functions on strings and DataFrames. No Google or Streamlit imports.

    result = check_quality(df, run_date=date(2026, 8, 20))   # df from SheetStore.read_invoices()
    summary = summarize(result)

Severities: BLOCK (row cannot be processed: status DQ_FAILED, never contacted), WARN (row still
processed, issue shown), INFO (noted only). dq_status is FAIL if any BLOCK, else WARN if any WARN,
else PASS.

can_contact_email / can_contact_phone only say whether the stored value is *valid*. Channels
switched off by a bounce or a bad number at run time are the rules engine's business.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
from typing import Any, Mapping

import pandas as pd

from core.schema import Col, Config, DqStatus, HumanReason
from core.taxonomy import Outcome, parse_outcome

DIAL_CODES = {"AT": "43", "CZ": "420", "DE": "49", "HU": "36", "RO": "40"}
COUNTRY_CURRENCY = {"AT": "EUR", "DE": "EUR", "CZ": "CZK", "HU": "HUF", "RO": "RON"}
VALID_CURRENCIES = frozenset(COUNTRY_CURRENCY.values())
REQUIRED_COLUMNS = (Col.INVOICE_NUMBER, Col.CUSTOMER, Col.COUNTRY, Col.CONTACT_NAME, Col.DUE_DATE,
                    Col.AMOUNT_OUTSTANDING, Col.CURRENCY)
# Columns compared when deciding whether two rows with one invoice number are exact duplicates. These
# never change once a row is in the sheet. Amount Outstanding is compared only while no copy has been
# worked on, because paying or contacting the first copy changes it, and that must not turn a harmless
# duplicate into a "conflict" on the next day.
DUPLICATE_IDENTITY_COLUMNS = (Col.INVOICE_NUMBER, Col.CUSTOMER, Col.COUNTRY, Col.CONTACT_NAME, Col.EMAIL, Col.PHONE,
                              Col.INVOICE_DATE, Col.DUE_DATE, Col.INVOICE_AMOUNT, Col.CURRENCY)
TERMS_RANGE_DAYS = (15, 90)


class Severity(str, enum.Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


@dataclasses.dataclass(frozen=True)
class Issue:
    rule: str  # "DQ-05"
    code: str  # "EMAIL_INVALID"
    severity: Severity
    message: str

    def short(self) -> str:
        return f"{self.rule}:{self.code}"


# ---------------------------------------------------------------- parsing helpers


def parse_amount(text: str | None) -> float | None:
    """'4820', '12750.5', '1,840,000', '4.820,50' and '-1450' all parse; junk gives None."""
    s = (text or "").strip().replace(" ", "").replace(" ", "")
    if not s or not re.fullmatch(r"[+-]?[\d.,]+", s):
        return None
    sign = -1.0 if s[0] == "-" else 1.0
    s = s.lstrip("+-")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    elif s.count(".") > 1:
        if not re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            return None
        s = s.replace(".", "")
    try:
        return sign * float(s)
    except ValueError:
        return None


def parse_date(text: str | None) -> dt.date | None:
    """ISO yyyy-mm-dd only. A trailing time is ignored."""
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$", text or "")
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def parse_timestamp(text: str | None) -> dt.datetime | None:
    """'2026-08-04 10:12' and '2026-06-15 9:05' (no leading zero on the hour) both parse."""
    s = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)


def validate_email(text: str | None) -> bool:
    """One @, dotted domain with a 2+ letter TLD, no empty labels or consecutive dots."""
    return bool(_EMAIL_RE.match((text or "").strip()))


@dataclasses.dataclass(frozen=True)
class PhoneResult:
    valid: bool
    e164: str = ""
    normalized: bool = False  # a national number was converted using the country dial code
    country_mismatch: bool = False
    reason: str = ""


def normalize_phone(text: str | None, country: str | None) -> PhoneResult:
    """Convert to E.164. '0170 4471129' with country DE becomes +491704471129."""
    raw = (text or "").strip()
    if not raw:
        return PhoneResult(False, reason="blank")
    s = re.sub(r"\(0\)", "", raw)
    s = re.sub(r"[^\d+]", "", s)
    if "+" in s[1:] or s.count("+") > 1:
        return PhoneResult(False, reason="misplaced plus sign")
    normalized = False
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        digits = s[1:]
    elif s.startswith("0"):
        dial = DIAL_CODES.get((country or "").upper())
        if not dial:
            return PhoneResult(False, reason="national number and the country is unknown")
        digits, normalized = dial + s[1:], True
    else:
        return PhoneResult(False, reason="no country code")
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return PhoneResult(False, reason=f"{len(digits)} digits (needs 8 to 15)")
    dial = DIAL_CODES.get((country or "").upper())
    mismatch = bool(dial) and not digits.startswith(dial)
    return PhoneResult(True, "+" + digits, normalized, mismatch)


_ENTITY_SUFFIXES: list[tuple[re.Pattern[str], frozenset[str], str]] = [
    (re.compile(r"\bsp\.?\s*z\s*o\.?\s*o\.?$", re.I), frozenset({"PL"}), "Sp. z o.o."),
    (re.compile(r"\bs\.?\s*r\.?\s*o\.?$", re.I), frozenset({"CZ", "SK"}), "s.r.o."),
    (re.compile(r"\ba\.\s*s\.?$", re.I), frozenset({"CZ", "SK"}), "a.s."),
    (re.compile(r"\bgmbh$", re.I), frozenset({"DE", "AT", "CH"}), "GmbH"),
    (re.compile(r"\bkft\.?$", re.I), frozenset({"HU"}), "Kft."),
    (re.compile(r"\bzrt\.?$", re.I), frozenset({"HU"}), "Zrt."),
    (re.compile(r"\bs\.?\s*r\.?\s*l\.?$", re.I), frozenset({"RO", "IT"}), "SRL"),
]

_PHONE_HISTORY = re.compile(r"\bcalled\b(?!\s+back)|\bphoned\b|\bcall (?:placed|attempt)", re.I)
_EMAIL_HISTORY = re.compile(r"\be-?mail|auto-?reply|\breplied\b", re.I)  # \b so "voicemail" is not email


# ---------------------------------------------------------------- single-row checks


@dataclasses.dataclass
class RowCheck:
    issues: list[Issue]
    email_ok: bool
    phone_ok: bool
    normalized_email: str
    normalized_phone: str
    contact_key: str
    exclusion_reason: str  # "", SETTLED, READY_NO, NOT_AN_INVOICE, BELOW_MINIMUM, NOT_YET_DUE
    ready_raw: str
    amount_outstanding: float | None
    suggested_email_attempts: int
    suggested_phone_attempts: int
    suggested_legacy_attempts: int
    suggested_human_reason: str


def _get(row: Mapping[str, Any], col: str) -> str:
    value = row.get(col, "")
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()


def check_row(row: Mapping[str, Any], *, run_date: dt.date, config: Config | None = None) -> RowCheck:
    """Apply every rule that needs only this row. Cross-row rules (DQ-02, DQ-17) are in check_quality."""
    cfg = config or Config()
    issues: list[Issue] = []

    def add(rule: str, code: str, sev: Severity, msg: str) -> None:
        issues.append(Issue(rule, code, sev, msg))

    country = _get(row, Col.COUNTRY).upper()
    currency = _get(row, Col.CURRENCY).upper()
    inv_no = _get(row, Col.INVOICE_NUMBER)

    # DQ-01 required fields
    for col in REQUIRED_COLUMNS:
        if not _get(row, col):
            add("DQ-01", "MISSING_REQUIRED", Severity.BLOCK, f"{col} is blank")

    # DQ-13 country and currency
    if country and country not in DIAL_CODES:
        add("DQ-13", "COUNTRY_INVALID", Severity.BLOCK, f"Country {country!r} is not one of {', '.join(sorted(DIAL_CODES))}")
    if currency and currency not in VALID_CURRENCIES:
        add("DQ-13", "CURRENCY_INVALID", Severity.BLOCK, f"Currency {currency!r} is not one of {', '.join(sorted(VALID_CURRENCIES))}")
    elif country in COUNTRY_CURRENCY and currency and COUNTRY_CURRENCY[country] != currency:
        add("DQ-13", "CURRENCY_COUNTRY_MISMATCH", Severity.WARN, f"{currency} is unusual for {country} (expected {COUNTRY_CURRENCY[country]})")

    # DQ-10 amounts, DQ-03 credit notes
    out_text, inv_text = _get(row, Col.AMOUNT_OUTSTANDING), _get(row, Col.INVOICE_AMOUNT)
    out_amt, inv_amt = parse_amount(out_text), parse_amount(inv_text)
    if out_text and out_amt is None:
        add("DQ-10", "AMOUNT_INVALID", Severity.BLOCK, f"Amount Outstanding {out_text!r} is not a number")
    if inv_text and inv_amt is None:
        add("DQ-10", "AMOUNT_INVALID", Severity.BLOCK, f"Invoice Amount {inv_text!r} is not a number")
    if out_amt is not None and inv_amt is not None and out_amt > inv_amt + 0.005:
        add("DQ-10", "AMOUNT_INVALID", Severity.BLOCK, f"Amount Outstanding {out_amt:g} is larger than Invoice Amount {inv_amt:g}")
    not_invoice = (bool(inv_no) and not inv_no.upper().startswith(cfg.invoice_prefix.upper())) or (out_amt is not None and out_amt < 0)
    if not_invoice:
        add("DQ-03", "NOT_A_COLLECTIBLE_INVOICE", Severity.BLOCK,
            f"{inv_no or 'row'} is not a collectible invoice (prefix is not {cfg.invoice_prefix!r} or the balance is negative)")

    # DQ-11 tiny amounts
    minimum = cfg.min_amount.get(currency)
    below_min = out_amt is not None and minimum is not None and 0 < out_amt < minimum
    if below_min:
        add("DQ-11", "AMOUNT_BELOW_MINIMUM", Severity.WARN, f"{out_amt:g} {currency} is below the minimum of {minimum:g}")

    # DQ-12 dates and payment terms
    due, invd = parse_date(_get(row, Col.DUE_DATE)), parse_date(_get(row, Col.INVOICE_DATE))
    if _get(row, Col.DUE_DATE) and due is None:
        add("DQ-12", "DATE_INVALID", Severity.BLOCK, f"Due Date {_get(row, Col.DUE_DATE)!r} is not a yyyy-mm-dd date")
    if _get(row, Col.INVOICE_DATE) and invd is None:
        add("DQ-12", "DATE_INVALID", Severity.BLOCK, f"Invoice Date {_get(row, Col.INVOICE_DATE)!r} is not a yyyy-mm-dd date")
    if due and invd:
        if due < invd:
            add("DQ-12", "DATE_INVALID", Severity.BLOCK, "Due Date is before Invoice Date")
        elif not TERMS_RANGE_DAYS[0] <= (due - invd).days <= TERMS_RANGE_DAYS[1]:
            add("DQ-12", "PAYMENT_TERMS_UNUSUAL", Severity.INFO, f"Payment terms of {(due - invd).days} days are unusual")

    # DQ-05 email
    email_raw = _get(row, Col.EMAIL)
    email_ok = validate_email(email_raw)
    if not email_raw:
        add("DQ-05", "EMAIL_MISSING", Severity.WARN, "No email address")
    elif not email_ok:
        add("DQ-05", "EMAIL_INVALID", Severity.WARN, f"Email {email_raw!r} is not a valid address")
    normalized_email = email_raw.lower() if email_ok else ""

    # DQ-06 phone
    phone_raw = _get(row, Col.PHONE)
    phone = normalize_phone(phone_raw, country)
    if not phone_raw:
        add("DQ-06", "PHONE_MISSING", Severity.WARN, "No phone number")
    elif not phone.valid:
        add("DQ-06", "PHONE_INVALID", Severity.WARN, f"Phone {phone_raw!r} is not usable: {phone.reason}")
    else:
        if phone.normalized:
            add("DQ-06", "PHONE_NORMALIZED", Severity.INFO, f"Phone {phone_raw!r} was converted to {phone.e164}")
        if phone.country_mismatch:
            add("DQ-06", "PHONE_COUNTRY_MISMATCH", Severity.WARN, f"Phone {phone.e164} does not start with the +{DIAL_CODES[country]} code for {country}")
    phone_ok = phone.valid

    # DQ-07 / DQ-08 ready flag
    ready_raw = _get(row, Col.READY_FOR_CONTACT)
    ready = ready_raw.lower()
    if not ready:
        add("DQ-07", "READY_FLAG_BLANK", Severity.WARN, "Ready for Contact is blank; it will be derived")
    elif ready not in ("yes", "no"):
        add("DQ-07", "READY_FLAG_INVALID", Severity.BLOCK, f"Ready for Contact {ready_raw!r} must be Yes or No")
    if ready == "no":
        add("DQ-08", "READY_FLAG_NO", Severity.INFO, _get(row, Col.NOTES) or "Marked not ready for contact")

    # DQ-15 history, and the outcome value itself
    outcome_text = _get(row, Col.OUTCOME)
    outcome = parse_outcome(outcome_text, strict=False)
    if outcome_text and outcome is None:
        add("DQ-15", "OUTCOME_UNKNOWN", Severity.WARN, f"Outcome {outcome_text!r} is not in the taxonomy")
    attempts_text = _get(row, Col.CONTACT_ATTEMPTS)
    attempts = 0
    if attempts_text:
        if re.fullmatch(r"\d+(\.0+)?", attempts_text):
            attempts = int(float(attempts_text))
        else:
            add("DQ-15", "ATTEMPTS_INVALID", Severity.WARN, f"Contact Attempts {attempts_text!r} is not a whole number")
    last_contact_text = _get(row, Col.LAST_CONTACT_DATE)
    last_contact = parse_date(last_contact_text)
    counters = [_get(row, c) for c in (Col.EMAIL_ATTEMPTS, Col.PHONE_ATTEMPTS, Col.LEGACY_ATTEMPTS)]
    app_managed = any(counters)  # the app has taken the row over: it writes counters and outcomes itself
    if last_contact_text and last_contact is None:
        add("DQ-15", "HISTORY_INCONSISTENT", Severity.WARN, f"Last Contact Date {last_contact_text!r} is not a yyyy-mm-dd date")
    if not app_managed:  # for rows the app writes, an invoice covered by another invoice's contact has attempts but no outcome
        if attempts > 0 and not last_contact_text:
            add("DQ-15", "HISTORY_INCONSISTENT", Severity.WARN, f"{attempts} attempts recorded but no Last Contact Date")
        if attempts > 0 and not outcome_text:
            add("DQ-15", "HISTORY_INCONSISTENT", Severity.WARN, f"{attempts} attempts recorded but no Outcome")
        if attempts == 0 and (outcome_text or last_contact_text):
            add("DQ-15", "HISTORY_INCONSISTENT", Severity.WARN, "Outcome or Last Contact Date is set but Contact Attempts is 0")
    updated = parse_timestamp(_get(row, Col.LAST_UPDATED))
    if updated and last_contact and updated.date() < last_contact:
        add("DQ-15", "HISTORY_INCONSISTENT", Severity.WARN, "Last Updated is earlier than Last Contact Date")

    # DQ-16 legacy attempts have no channel
    s_email = s_phone = s_legacy = 0
    suggested_reason = ""
    if attempts > 0 and not any(counters):
        summary = _get(row, Col.SUMMARY)
        by_phone, by_email = bool(_PHONE_HISTORY.search(summary)), bool(_EMAIL_HISTORY.search(summary))
        if by_phone and not by_email:
            s_phone = attempts
            add("DQ-16", "LEGACY_ATTEMPTS_MAPPED", Severity.INFO, f"{attempts} legacy attempt(s) will be counted as phone")
        elif by_email and not by_phone:
            s_email = attempts
            add("DQ-16", "LEGACY_ATTEMPTS_MAPPED", Severity.INFO, f"{attempts} legacy attempt(s) will be counted as email")
        else:
            s_legacy = attempts
            add("DQ-16", "LEGACY_ATTEMPTS_NO_CHANNEL", Severity.WARN, f"{attempts} legacy attempt(s) have no known channel")
            if attempts >= cfg.attempt_cap:
                suggested_reason = HumanReason.ATTEMPT_CAP_LEGACY.value

    # DQ-18 legal form versus country
    customer = _get(row, Col.CUSTOMER)
    for pattern, countries, label in _ENTITY_SUFFIXES:
        if pattern.search(customer):
            if country in DIAL_CODES and country not in countries:
                add("DQ-18", "ENTITY_SUFFIX_MISMATCH", Severity.INFO, f"{label} is not a usual legal form in {country}")
            break

    # DQ-09, DQ-14 and the exclusion decision. Order of precedence: invoice type, settled, hold, minimum, not due.
    settled = outcome is Outcome.PAID or (out_amt is not None and out_amt == 0)
    if settled:
        add("DQ-09", "ALREADY_SETTLED", Severity.INFO, "Paid or nothing outstanding")
    if due and due > run_date:
        add("DQ-14", "NOT_YET_DUE", Severity.INFO, f"Due {due.isoformat()}, after the run date {run_date.isoformat()}")
    if not_invoice:
        exclusion = "NOT_AN_INVOICE"
    elif settled:
        exclusion = "SETTLED"
    elif ready == "no":
        exclusion = "READY_NO"
    elif below_min and cfg.skip_below_minimum:
        exclusion = "BELOW_MINIMUM"
    elif due and due > run_date:
        exclusion = "NOT_YET_DUE"
    else:
        exclusion = ""

    # DQ-04 no usable channel (not needed for rows that are never contacted anyway)
    if not email_ok and not phone_ok and exclusion not in ("SETTLED", "READY_NO", "NOT_AN_INVOICE"):
        add("DQ-04", "NO_CONTACT_CHANNEL", Severity.BLOCK, "Neither a valid email nor a valid phone number")

    if email_ok:
        key = normalized_email
    elif phone_ok:
        key = phone.e164
    else:
        key = f"{_get(row, Col.CONTACT_NAME)}|{customer}".lower()

    return RowCheck(
        issues=issues, email_ok=email_ok, phone_ok=phone_ok, normalized_email=normalized_email,
        normalized_phone=phone.e164 if phone_ok else "", contact_key=key, exclusion_reason=exclusion,
        ready_raw=ready_raw, amount_outstanding=out_amt, suggested_email_attempts=s_email,
        suggested_phone_attempts=s_phone, suggested_legacy_attempts=s_legacy,
        suggested_human_reason=suggested_reason,
    )


# ---------------------------------------------------------------- whole-sheet check

RESULT_COLUMNS = [
    "row_key", "invoice_number", "country", "dq_status", "dq_issues", "dq_issues_text",
    "can_contact_email", "can_contact_phone", "normalized_email", "normalized_phone", "contact_key",
    "contact_rows", "ready_for_contact_derived", "is_excluded", "exclusion_reason", "human_reason",
    "amount_outstanding_num", "suggested_email_attempts", "suggested_phone_attempts",
    "suggested_legacy_attempts", "suggested_human_reason",
]


def _status_of(issues: list[Issue]) -> DqStatus:
    if any(i.severity is Severity.BLOCK for i in issues):
        return DqStatus.FAIL
    if any(i.severity is Severity.WARN for i in issues):
        return DqStatus.WARN
    return DqStatus.PASS


def check_quality(df: pd.DataFrame, *, run_date: dt.date, config: Config | None = None) -> pd.DataFrame:
    """Run all rules over every row. One output row per input row, in the same order.

    Output columns: row_key, invoice_number, country, dq_status, dq_issues (list of Issue),
    dq_issues_text, can_contact_email, can_contact_phone, normalized_email, normalized_phone,
    contact_key, contact_rows, ready_for_contact_derived, is_excluded, exclusion_reason,
    human_reason, amount_outstanding_num, suggested_email_attempts, suggested_phone_attempts,
    suggested_legacy_attempts, suggested_human_reason.
    """
    cfg = config or Config()
    missing = [c for c in (Col.INVOICE_NUMBER,) if c not in df.columns]
    if missing:
        raise ValueError(f"Input is missing required column(s): {', '.join(missing)}")

    rows = df.to_dict("records")
    keys = [str(r.get(Col.ROW_KEY) or f"row{i + 2}") for i, r in enumerate(rows)]
    checks = [check_row(r, run_date=run_date, config=cfg) for r in rows]

    # DQ-02 duplicate invoice numbers: the first occurrence is the master, later ones are blocked.
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        number = _get(r, Col.INVOICE_NUMBER).upper()
        if number:
            groups.setdefault(number, []).append(i)
    for number, idxs in groups.items():
        if len(idxs) < 2:
            continue
        untouched = all(not (_get(rows[i], Col.OUTCOME) or _get(rows[i], Col.LAST_CONTACT_DATE)
                             or _get(rows[i], Col.CONTACT_ATTEMPTS).strip("0.")) for i in idxs)
        columns = DUPLICATE_IDENTITY_COLUMNS + ((Col.AMOUNT_OUTSTANDING,) if untouched else ())
        signature = [tuple(_get(rows[i], c) for c in columns) for i in idxs]
        if len(set(signature)) == 1:
            for i in idxs[1:]:
                checks[i].issues.append(Issue("DQ-02", "DUPLICATE_INVOICE", Severity.BLOCK, f"Exact duplicate of {keys[idxs[0]]}"))
        else:
            for i in idxs:
                checks[i].issues.append(Issue("DQ-02", "DUPLICATE_INVOICE_CONFLICT", Severity.BLOCK,
                                              f"{number} appears {len(idxs)} times with different values; all copies need review"))

    statuses = [_status_of(c.issues) for c in checks]

    # DQ-17 several rows for one contact (counted among rows that are not failed)
    contact_counts: dict[str, int] = {}
    for c, s in zip(checks, statuses):
        if s is not DqStatus.FAIL:
            contact_counts[c.contact_key] = contact_counts.get(c.contact_key, 0) + 1
    for c, s in zip(checks, statuses):
        n = contact_counts.get(c.contact_key, 0)
        if s is not DqStatus.FAIL and n > 1:
            c.issues.append(Issue("DQ-17", "CONTACT_SHARED_ACROSS_ROWS", Severity.INFO, f"{n} open rows share this contact"))

    out = []
    for i, (r, c) in enumerate(zip(rows, checks)):
        status = statuses[i]
        issues_sorted = sorted(c.issues, key=lambda x: (x.rule, x.code))
        ready = c.ready_raw.lower()
        if ready in ("yes", "no"):
            derived = ready.capitalize()
        else:
            derived = "Yes" if (c.email_ok or c.phone_ok) and status is not DqStatus.FAIL and not c.exclusion_reason else "No"
        if status is DqStatus.FAIL:
            human = HumanReason.NOT_AN_INVOICE.value if any(x.code == "NOT_A_COLLECTIBLE_INVOICE" for x in c.issues) else HumanReason.DQ.value
        elif c.exclusion_reason == "READY_NO":
            human = HumanReason.READY_NO.value
        else:
            human = ""
        out.append({
            "row_key": keys[i],
            "invoice_number": _get(r, Col.INVOICE_NUMBER),
            "country": _get(r, Col.COUNTRY).upper(),
            "dq_status": status.value,
            "dq_issues": issues_sorted,
            "dq_issues_text": "; ".join(x.short() for x in issues_sorted),
            "can_contact_email": c.email_ok,
            "can_contact_phone": c.phone_ok,
            "normalized_email": c.normalized_email,
            "normalized_phone": c.normalized_phone,
            "contact_key": c.contact_key,
            "contact_rows": contact_counts.get(c.contact_key, 0),
            "ready_for_contact_derived": derived,
            "is_excluded": bool(c.exclusion_reason),
            "exclusion_reason": c.exclusion_reason,
            "human_reason": human,
            "amount_outstanding_num": c.amount_outstanding,
            "suggested_email_attempts": c.suggested_email_attempts,
            "suggested_phone_attempts": c.suggested_phone_attempts,
            "suggested_legacy_attempts": c.suggested_legacy_attempts,
            "suggested_human_reason": c.suggested_human_reason,
        })
    return pd.DataFrame(out, index=df.index, columns=RESULT_COLUMNS)


# ---------------------------------------------------------------- summary for the UI


@dataclasses.dataclass
class DQSummary:
    total: int
    by_status: dict[str, int]  # PASS / WARN / FAIL (always all three keys)
    excluded_by_reason: dict[str, int]
    issues: pd.DataFrame  # rule, code, severity, issues, rows
    by_country: pd.DataFrame  # country x dq_status


def summarize(result: pd.DataFrame) -> DQSummary:
    """Aggregate a check_quality() result: counts by status, issue code, severity and country."""
    by_status = {s.value: int((result["dq_status"] == s.value).sum()) for s in DqStatus}
    excluded = result.loc[result["is_excluded"], "exclusion_reason"].value_counts().to_dict()

    records = [(i.rule, i.code, i.severity.value, key) for key, issues in zip(result["row_key"], result["dq_issues"]) for i in issues]
    if records:
        flat = pd.DataFrame(records, columns=["rule", "code", "severity", "row_key"])
        issues = (flat.groupby(["rule", "code", "severity"])
                  .agg(issues=("row_key", "size"), rows=("row_key", "nunique")).reset_index())
    else:
        issues = pd.DataFrame(columns=["rule", "code", "severity", "issues", "rows"])

    by_country = (pd.crosstab(result["country"], result["dq_status"])
                  .reindex(columns=[s.value for s in DqStatus], fill_value=0))
    return DQSummary(total=len(result), by_status=by_status, excluded_by_reason={k: int(v) for k, v in excluded.items()},
                     issues=issues, by_country=by_country)
