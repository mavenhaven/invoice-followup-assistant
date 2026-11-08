"""Numbers for the dashboard: contact events, KPIs, status counts, drill-down and reply themes.

Pure pandas, no Streamlit or Plotly. Inputs are the string frames read by SheetStore
(read_invoices and read_audit_log). Amounts are summed only within one currency.

Vocabulary
  contact event  one email or call (a combined contact over several invoices is ONE event)
  attempt        an event that used up one of the contact's attempts (counted_as_attempt = Yes)
  response       an attempt where a person engaged (the Contact made category)
  success        an attempt that ended in PAID, PARTIAL_PAYMENT, PROMISE_TO_PAY or PAYMENT_IN_PROCESS
  failure        an attempt with no contact made (no answer, no reply, out of office, voicemail)
                 or a recipient-side channel error (bounce, busy line, bad number)
  system error   our own failure; it never uses an attempt and is reported on its own
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Iterable

import pandas as pd

from core.quality import parse_amount
from core.schema import Channel, Col
from core.taxonomy import Category, Outcome, category_of, is_real_response, parse_outcome

SUCCESS_OUTCOMES = frozenset({Outcome.PAID, Outcome.PARTIAL_PAYMENT, Outcome.PROMISE_TO_PAY, Outcome.PAYMENT_IN_PROCESS})
FAILURE_CATEGORIES = frozenset({Category.NO_CONTACT, Category.CHANNEL_ERROR})
STATUS_BUCKETS = {"DQ_FAILED": "dq_failed", "NEEDS_HUMAN": "needs_human", "IN_PROGRESS": "in_progress", "NEW": "in_progress",
                  "": "in_progress", "HANDLED": "handled"}
BUCKET_LABELS = {"in_progress": "In progress", "needs_human": "Needing human follow-up", "dq_failed": "Data quality failed",
                 "handled": "Handled", "other": "Unrecognised status"}
EVENT_COLUMNS = ["event_id", "run_date", "contact_key", "channel", "outcome", "category", "counted", "real_response", "success",
                 "failure", "system_error", "attempt_no", "primary_row_key", "row_keys", "country", "customer", "contact_name",
                 "response_text", "promised_date"]


# ---------------------------------------------------------------- contact events


def _to_date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return None


def attempt_events(audit: pd.DataFrame | None, invoices: pd.DataFrame) -> pd.DataFrame:
    """One row per contact event, built from Audit_Log. Covered invoices of a combined contact collapse into it."""
    if audit is None or audit.empty or "contact_event_id" not in audit.columns:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    a = audit[audit["contact_event_id"].astype(str) != ""].copy()
    if a.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    a["_counted"] = a["counted_as_attempt"].astype(str).eq("Yes")
    a = a.sort_values(["contact_event_id", "_counted"], ascending=[True, False], kind="stable")  # the counted row leads
    row_keys = a.groupby("contact_event_id", sort=False)["row_key"].apply(list)
    lead = a.drop_duplicates("contact_event_id").set_index("contact_event_id")
    info = invoices.set_index(Col.ROW_KEY) if len(invoices) else pd.DataFrame()

    def lookup(row_key: str, column: str) -> str:
        return str(info.at[row_key, column]) if len(info) and row_key in info.index and column in info.columns else ""

    records = []
    for event_id, row in lead.iterrows():
        outcome = parse_outcome(str(row["outcome"]), strict=False)
        category = category_of(outcome) if outcome else None
        attempt_no = pd.to_numeric(row["attempt_no_for_channel"], errors="coerce")
        records.append({
            "event_id": event_id, "run_date": _to_date(row["run_date"]), "contact_key": row["contact_key"],
            "channel": row["channel"], "outcome": outcome.value if outcome else str(row["outcome"]),
            "category": category.value if category else "Unknown", "counted": bool(row["_counted"]),
            "real_response": bool(outcome and is_real_response(outcome)),
            "success": bool(outcome in SUCCESS_OUTCOMES), "failure": bool(category in FAILURE_CATEGORIES),
            "system_error": category is Category.SYSTEM_ERROR,
            "attempt_no": None if pd.isna(attempt_no) else int(attempt_no), "primary_row_key": row["row_key"],
            "row_keys": row_keys[event_id], "country": lookup(row["row_key"], Col.COUNTRY),
            "customer": lookup(row["row_key"], Col.CUSTOMER), "contact_name": lookup(row["row_key"], Col.CONTACT_NAME),
            "response_text": str(row["response_text"]), "promised_date": _to_date(row["promised_date"]),
        })
    return pd.DataFrame(records, columns=EVENT_COLUMNS).sort_values(["run_date", "event_id"]).reset_index(drop=True)


def filter_invoices(invoices: pd.DataFrame, countries: Iterable[str] | None = None, search: str = "") -> pd.DataFrame:
    """Keep rows in the chosen countries (None or empty = all) whose customer, contact, email or invoice number contains the text."""
    df = invoices
    chosen = [c for c in (countries or []) if c]
    if chosen and Col.COUNTRY in df.columns:
        df = df[df[Col.COUNTRY].isin(chosen)]
    text = search.strip().lower()
    if text:
        cols = [c for c in (Col.CUSTOMER, Col.CONTACT_NAME, Col.EMAIL, Col.INVOICE_NUMBER) if c in df.columns]
        mask = pd.Series(False, index=df.index)
        for c in cols:
            mask |= df[c].astype(str).str.lower().str.contains(text, regex=False)
        df = df[mask]
    return df


def filter_events(events: pd.DataFrame, row_keys: Iterable[str]) -> pd.DataFrame:
    """Events that cover at least one of the given rows."""
    keep = set(row_keys)
    if events.empty:
        return events
    return events[events["row_keys"].apply(lambda ks: any(k in keep for k in ks))]


# ---------------------------------------------------------------- KPIs and status counts


def _rate(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


@dataclasses.dataclass
class Kpis:
    attempts: int
    responses: int
    successes: int
    failures: int
    system_errors: int
    events: int
    rows: int
    human_flagged: int
    dq_failed: int
    needs_human: int
    response_rate: float | None
    success_rate: float | None
    failure_rate: float | None
    system_error_rate: float | None
    human_rate: float | None


def kpis(events: pd.DataFrame, invoices: pd.DataFrame) -> Kpis:
    counted = events[events["counted"]] if len(events) else events
    attempts = len(counted)
    responses = int(counted["real_response"].sum()) if attempts else 0
    successes = int(counted["success"].sum()) if attempts else 0
    failures = int(counted["failure"].sum()) if attempts else 0
    system_errors = int(events["system_error"].sum()) if len(events) else 0
    status = invoices[Col.STATUS] if Col.STATUS in invoices.columns else pd.Series([], dtype=object)
    dq, human = int((status == "DQ_FAILED").sum()), int((status == "NEEDS_HUMAN").sum())
    rows = len(invoices)
    return Kpis(attempts, responses, successes, failures, system_errors, len(events), rows, dq + human, dq, human,
                _rate(responses, attempts), _rate(successes, attempts), _rate(failures, attempts),
                _rate(system_errors, len(events)), _rate(dq + human, rows))


@dataclasses.dataclass
class StatusCounts:
    total: int
    buckets: dict[str, int]  # in_progress, needs_human, dq_failed, handled, other

    @property
    def reconciled(self) -> bool:
        """The four known groups add up to the number of rows and no row has an unknown status."""
        return self.buckets["other"] == 0 and sum(v for k, v in self.buckets.items() if k != "other") == self.total


def status_counts(invoices: pd.DataFrame) -> StatusCounts:
    buckets = {"in_progress": 0, "needs_human": 0, "dq_failed": 0, "handled": 0, "other": 0}
    statuses = invoices[Col.STATUS].astype(str).str.strip() if Col.STATUS in invoices.columns else pd.Series([""] * len(invoices))
    for value in statuses:
        buckets[STATUS_BUCKETS.get(value, "other")] += 1
    return StatusCounts(len(invoices), buckets)


# ---------------------------------------------------------------- chart tables


def attempts_per_day(events: pd.DataFrame) -> pd.DataFrame:
    """Attempts per run date and channel."""
    counted = events[events["counted"]] if len(events) else events
    if counted.empty:
        return pd.DataFrame(columns=["run_date", "channel", "attempts"])
    return counted.groupby(["run_date", "channel"]).size().rename("attempts").reset_index()


def outcome_mix(events: pd.DataFrame) -> pd.DataFrame:
    """Contact events by outcome and category, most common first (system errors included, as their own category)."""
    if events.empty:
        return pd.DataFrame(columns=["category", "outcome", "events"])
    out = events.groupby(["category", "outcome"]).size().rename("events").reset_index()
    return out.sort_values(["events", "outcome"], ascending=[False, True]).reset_index(drop=True)


def _rates(df: pd.DataFrame, key: str) -> pd.DataFrame:
    g = df.groupby(key).agg(attempts=("counted", "size"), responses=("real_response", "sum"),
                            successes=("success", "sum"), failures=("failure", "sum")).reset_index()
    for col, name in (("responses", "response_rate"), ("successes", "success_rate"), ("failures", "failure_rate")):
        g[col] = g[col].astype(int)
        g[name] = g[col] / g["attempts"]
    return g


def channel_comparison(events: pd.DataFrame) -> pd.DataFrame:
    """One row per channel (both always present) with attempts and the three rates."""
    counted = events[events["counted"]] if len(events) else events
    base = pd.DataFrame({"channel": [c.value for c in Channel]})
    if counted.empty:
        for c in ("attempts", "responses", "successes", "failures"):
            base[c] = 0
        for c in ("response_rate", "success_rate", "failure_rate"):
            base[c] = float("nan")
        return base
    out = base.merge(_rates(counted, "channel"), on="channel", how="left")
    for c in ("attempts", "responses", "successes", "failures"):
        out[c] = out[c].fillna(0).astype(int)
    return out


def success_by_country(events: pd.DataFrame) -> pd.DataFrame:
    """Attempts and rates per country, highest success rate first."""
    counted = events[events["counted"] & (events["country"] != "")] if len(events) else events
    if counted.empty:
        return pd.DataFrame(columns=["country", "attempts", "responses", "successes", "failures", "response_rate", "success_rate", "failure_rate"])
    return _rates(counted, "country").sort_values(["success_rate", "attempts", "country"], ascending=[False, False, True]).reset_index(drop=True)


def outstanding_by_currency(invoices: pd.DataFrame) -> pd.DataFrame:
    """Rows and outstanding amount per currency and status group. Never adds across currencies."""
    if invoices.empty or Col.CURRENCY not in invoices.columns:
        return pd.DataFrame(columns=["currency", "status", "rows", "outstanding"])
    df = pd.DataFrame({
        "currency": invoices[Col.CURRENCY].astype(str).str.upper(),
        "status": [BUCKET_LABELS[STATUS_BUCKETS.get(str(s).strip(), "other")] for s in invoices.get(Col.STATUS, [""] * len(invoices))],
        "amount": [parse_amount(str(v)) or 0.0 for v in invoices[Col.AMOUNT_OUTSTANDING]],
    })
    out = df.groupby(["currency", "status"]).agg(rows=("amount", "size"), outstanding=("amount", "sum")).reset_index()
    return out.sort_values(["currency", "status"]).reset_index(drop=True)


# ---------------------------------------------------------------- drill-down


def contact_options(invoices: pd.DataFrame) -> pd.DataFrame:
    """One row per contact for a picker: contact_key and a readable label."""
    if invoices.empty:
        return pd.DataFrame(columns=["contact_key", "label"])
    df = invoices.assign(_key=invoices[Col.CONTACT_KEY].where(invoices[Col.CONTACT_KEY] != "", invoices[Col.ROW_KEY]))
    g = df.groupby("_key").agg(customer=(Col.CUSTOMER, "first"), contact=(Col.CONTACT_NAME, "first"), n=(Col.ROW_KEY, "size")).reset_index()
    g["label"] = g["customer"] + " · " + g["contact"] + " (" + g["n"].astype(str) + " invoice" + g["n"].map(lambda n: "" if n == 1 else "s") + ")"
    return g.rename(columns={"_key": "contact_key"})[["contact_key", "label"]].sort_values("label").reset_index(drop=True)


def contact_history(events: pd.DataFrame, contact_key: str) -> pd.DataFrame:
    """Every contact event for one contact, oldest first: when, how, what came back, which invoices."""
    cols = ["date", "channel", "attempt", "counted", "outcome", "category", "invoices", "promised_date", "reply"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    e = events[events["contact_key"] == contact_key].sort_values(["run_date", "event_id"])
    return pd.DataFrame({
        "date": e["run_date"], "channel": e["channel"], "attempt": e["attempt_no"], "counted": e["counted"],
        "outcome": e["outcome"], "category": e["category"],
        "invoices": e["row_keys"].apply(lambda ks: ", ".join(k.split("#")[0] for k in ks)),
        "promised_date": e["promised_date"], "reply": e["response_text"]}).reset_index(drop=True)[cols]


# ---------------------------------------------------------------- reply themes

THEMES: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("Will pay on a date", r"payment run|will pay|will be paid|pay .*\bby\b|release the payment|paid by"),
    ("Says it was already paid", r"already (?:paid|settled)|paid yesterday|we transferred|released (?:on|this)|bank confirmation"),
    ("Payment on its way", r"in process|initiated|waiting for bank|should arrive"),
    ("Part payment", r"part payment|remaining|balance of"),
    ("Wants a call back", r"call me|call back|phone me|call .* back"),
    ("Needs the invoice again", r"resend|send a copy|copy of|cannot find|do not have that invoice"),
    ("Cash flow, wants instalments", r"instalment|cash position|monthly"),
    ("Disputes the invoice", r"dispute|never delivered|damaged|credit note|returned|charged twice|price"),
    ("Wrong person", r"not responsible|wrong person|reception|does not handle|finance department"),
    ("Refuses to pay", r"will not pay|lawyer|reject this claim|do not call again"),
    ("Away or out of office", r"out of the office|on leave|automatic reply|auto-reply|back on"),
    ("Could not reach", r"no answer|voicemail|line busy|rang \d+ times|not in service|disconnected"),
    ("Email did not arrive", r"undeliverable|does not exist|mailbox full|not delivered|rejected|not a valid email|temporary failure"),
]]
STOPWORDS = frozenset("""about above after again also another been before being between both cannot could does done each from
have here hello into just kindly like make more must much only other please regards same some such than that their them then
there these they this those very want what when where which while will with would your thank thanks yours dear good morning
agent contact subject best kind invoice invoices open balance overdue payment reminder today yesterday sorry apologies
""".split())
_GREETING = re.compile(r"^(hello|hi|good morning|dear colleague)[,.!]*$", re.I)


def reply_text(text: str, channel: str) -> str:
    """Only what the contact said: phone lines from 'Contact:', email body without subject, greeting and sign-off."""
    t = (text or "").strip()
    if not t or t.startswith("No reply received") or t.startswith("Covered by"):
        return ""
    if channel == Channel.PHONE.value:
        return " ".join(line.split(":", 1)[1].strip() for line in t.splitlines() if line.startswith("Contact:"))
    blocks = [b.strip() for b in re.split(r"\n\s*\n", t) if b.strip()]
    blocks = [b for b in blocks if not b.startswith("Subject:") and not _GREETING.match(b)]
    if len(blocks) > 1 and "\n" in blocks[-1]:  # the sign-off: "Regards,\nName\nCompany"
        blocks = blocks[:-1]
    return " ".join(blocks)


def theme_counts(history: pd.DataFrame) -> pd.DataFrame:
    """How many replies touched each theme (a reply can touch several)."""
    texts = [reply_text(r, c) for r, c in zip(history.get("reply", []), history.get("channel", []))]
    rows = [(name, sum(1 for t in texts if t and rx.search(t))) for name, rx in THEMES]
    out = pd.DataFrame(rows, columns=["theme", "replies"])
    return out[out["replies"] > 0].sort_values(["replies", "theme"], ascending=[False, True]).reset_index(drop=True)


def top_words(history: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Most frequent meaningful words across a contact's replies."""
    counts: dict[str, int] = {}
    for reply, channel in zip(history.get("reply", []), history.get("channel", [])):
        for word in re.findall(r"[a-zA-Z]{4,}", reply_text(reply, channel).lower()):
            if word not in STOPWORDS:
                counts[word] = counts.get(word, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return pd.DataFrame(rows, columns=["word", "count"])
