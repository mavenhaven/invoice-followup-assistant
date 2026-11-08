"""Dashboard: how the follow-ups are going, with filters and a drill-down to one contact."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app_common import get_store, load_data, show_flash, sidebar
from core import charts
from core.metrics import (
    BUCKET_LABELS, attempt_events, attempts_per_day, channel_comparison, contact_history, contact_options, filter_events,
    filter_invoices, kpis, outcome_mix, outstanding_by_currency, status_counts, success_by_country, theme_counts, top_words,
)
from core.queue import fmt_money
from core.schema import Col

st.set_page_config(page_title="Dashboard", page_icon=":material/monitoring:", layout="wide")

store = get_store()
sidebar()


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001 - older Streamlit or no theme information
        return "light"


MODE = theme_mode()


def show(fig, table: pd.DataFrame, key: str, label: str = "Table view") -> None:
    """A chart and its table twin: every number on the chart can also be read in the table.

    The key is required: two empty charts look identical to Streamlit, which refuses duplicate ids.
    """
    st.plotly_chart(fig, theme=None, config={"displayModeBar": False}, width="stretch", key=f"chart-{key}")
    with st.expander(label):
        st.dataframe(table, hide_index=True, width="stretch")


def pct(value: float | None) -> str:
    return "–" if value is None or pd.isna(value) else f"{value * 100:.0f}%"


st.title("Dashboard")
show_flash()

data = load_data(store)
invoices, audit = data["invoices"], data["audit"]
if invoices.empty:
    st.info("The invoice sheet has no rows yet.")
    st.stop()
events_all = attempt_events(audit, invoices)

# ------------------------------------------------------------------ one filter row above everything
f1, f2 = st.columns([2, 3])
countries = f1.multiselect("Country (contact location)", sorted(c for c in invoices[Col.COUNTRY].unique() if c), key="dash_countries",
                           placeholder="All countries")
search = f2.text_input("Search by customer, contact, email or invoice number", key="dash_search")
rows = filter_invoices(invoices, countries, search)
events = filter_events(events_all, rows[Col.ROW_KEY])
if len(rows) < len(invoices):
    st.caption(f"Showing {len(rows)} of {len(invoices)} rows.")
if rows.empty:
    st.warning("No rows match these filters.")
    st.stop()

# ------------------------------------------------------------------ headline numbers
k = kpis(events, rows)
t = st.columns(5)
t[0].metric("Attempts", k.attempts, help="Emails sent and calls made. A combined contact about several invoices counts once.")
t[1].metric("Response rate", pct(k.response_rate), help="Attempts where a person engaged, out of all attempts.")
t[2].metric("Success rate", pct(k.success_rate), help="Attempts that ended in paid, part paid, a promise to pay or payment in process.")
t[3].metric("Failure rate", pct(k.failure_rate), help="Attempts with no contact made (no answer, no reply, out of office) or a bounce, busy line or bad number.")
t[4].metric("Sent to a person", k.human_flagged, help="Rows needing human follow-up plus rows that failed data quality.")
sub = [f"{k.responses} responses", f"{k.successes} successes", f"{k.failures} failures"]
if k.system_errors:
    sub.append(f"{k.system_errors} system errors ({pct(k.system_error_rate)} of all contact events), which never use an attempt")
st.caption(" · ".join(sub))

# ------------------------------------------------------------------ where every row stands
st.subheader("Where every row stands")
sc = status_counts(rows)
b = st.columns(5)
for col, code in zip(b, ("in_progress", "needs_human", "dq_failed", "handled")):
    col.metric(BUCKET_LABELS[code], sc.buckets[code])
b[4].metric("Total rows", sc.total, delta="adds up" if sc.reconciled else "does not add up", delta_color="normal" if sc.reconciled else "inverse")
if not sc.reconciled:
    st.error(f"The groups add up to {sum(v for kk, v in sc.buckets.items() if kk != 'other')} but there are {sc.total} rows"
             + (f", and {sc.buckets['other']} row(s) have an unrecognised status." if sc.buckets["other"] else "."))

owed = outstanding_by_currency(rows)
with st.expander("Outstanding amounts by currency (never added across currencies)"):
    if not owed.empty:
        pivot = owed.pivot_table(index="currency", columns="status", values="outstanding", aggfunc="sum", fill_value=0.0)
        pivot["All rows"] = pivot.sum(axis=1)
        st.dataframe(pivot.map(lambda v: f"{v:,.2f}"), width="stretch")

if events_all.empty:
    st.info("No follow-ups have been run yet. The charts fill in after the first run.")
    st.stop()

# ------------------------------------------------------------------ charts
st.subheader("Attempts over time")
show(charts.attempts_per_day_figure(attempts_per_day(events), MODE), attempts_per_day(events), "attempts")

c1, c2 = st.columns(2)
with c1:
    st.subheader("What came back")
    mix = outcome_mix(events)
    show(charts.outcomes_figure(mix, MODE), mix, "outcomes")
with c2:
    st.subheader("Email and phone compared")
    cmp = channel_comparison(events)
    show(charts.channel_figure(cmp, MODE), cmp.assign(**{c: (cmp[c] * 100).round(1) for c in ("response_rate", "success_rate", "failure_rate")}), "channels")

st.subheader("Success by country")
by_country = success_by_country(events)
show(charts.country_figure(by_country, MODE), by_country, "country")

# ------------------------------------------------------------------ drill-down
st.header("Drill down to one contact", divider="gray")
options = contact_options(rows)
labels = dict(zip(options["contact_key"], options["label"]))
picked = st.selectbox("Contact", list(labels), format_func=labels.get, key="dash_contact")
mine = rows[rows[Col.CONTACT_KEY].where(rows[Col.CONTACT_KEY] != "", rows[Col.ROW_KEY]) == picked]
history = contact_history(events_all, picked)

m = st.columns(4)
m[0].metric("Invoices", len(mine))
m[1].metric("Followed up on", f"{history['date'].nunique()} dates" if not history.empty else "never")
m[2].metric("Attempts (email / phone)", f"{mine[Col.EMAIL_ATTEMPTS].astype(int).max()} / {mine[Col.PHONE_ATTEMPTS].astype(int).max()}"
            if Col.EMAIL_ATTEMPTS in mine.columns and mine[Col.EMAIL_ATTEMPTS].ne("").all() else "–")
m[3].metric("Approvals used", f"{mine[Col.EXTENSION_APPROVALS_COUNT].astype(int).max()} of {data['config'].max_approvals}"
            if Col.EXTENSION_APPROVALS_COUNT in mine.columns and mine[Col.EXTENSION_APPROVALS_COUNT].ne("").all() else "–")

inv = pd.DataFrame({"Invoice": mine[Col.INVOICE_NUMBER], "Outstanding": [fmt_money(a, c) for a, c in zip(mine[Col.AMOUNT_OUTSTANDING], mine[Col.CURRENCY])],
                    "Due": mine[Col.DUE_DATE], "Status": mine[Col.STATUS], "Next follow-up": mine[Col.NEXT_FOLLOWUP_DATE]})
st.dataframe(inv, hide_index=True, width="stretch")

st.subheader("Every follow-up")
if history.empty:
    st.info("This contact has not been followed up yet.")
else:
    shown = history.rename(columns={"date": "Date", "channel": "Channel", "attempt": "Attempt #", "counted": "Used an attempt",
                                    "outcome": "Outcome", "category": "Category", "invoices": "Invoices", "promised_date": "They said",
                                    "reply": "What came back"})
    st.dataframe(shown, hide_index=True, width="stretch",
                 column_config={"What came back": st.column_config.TextColumn(width="large"), "Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                                "They said": st.column_config.DateColumn(format="DD MMM YYYY")})

    st.subheader("Themes in their replies")
    themes = theme_counts(history)
    show(charts.themes_figure(themes, MODE), themes, "themes", "Table view and most frequent words")
    words = top_words(history)
    if not words.empty:
        st.caption("Most frequent words: " + ", ".join(f"{w} ({n})" for w, n in zip(words["word"], words["count"])))
