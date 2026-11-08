"""Invoice Follow-Up Assistant: landing page. Run: streamlit run app.py"""
from __future__ import annotations

import streamlit as st

from app_common import esc, get_store, load_data, run_date, show_flash, sidebar

st.set_page_config(page_title="Invoice Follow-Up Assistant", page_icon=":material/receipt_long:", layout="wide")

store = get_store()
sidebar()

st.title("Invoice Follow-Up Assistant")
st.write("Follows up with contacts on overdue invoices (email first, then phone), keeps a full audit trail, and "
         "hands anything it cannot or should not decide to a person. All data lives in one Google Sheet.")
show_flash()

data = load_data(store)
df, runs = data["invoices"], data["runs"]

STATUS_LABELS = [("IN_PROGRESS", "In progress"), ("NEEDS_HUMAN", "Needing human follow-up"),
                 ("DQ_FAILED", "Data quality failed"), ("HANDLED", "Handled")]
if df.empty:
    st.warning("The invoice sheet has no rows yet.")
else:
    status = df["status"].replace("", "IN_PROGRESS").replace("NEW", "IN_PROGRESS") if "status" in df.columns else None
    cols = st.columns(5)
    cols[0].metric("Total rows", len(df))
    for col, (code, label) in zip(cols[1:], STATUS_LABELS):
        col.metric(label, int((status == code).sum()) if status is not None else 0)
    if status is None or (status == "IN_PROGRESS").all():
        st.info("No follow-ups have been run yet. Open **Run** to start.")

if not runs.empty:
    last = runs.iloc[-1]
    st.caption(esc(f"Last run: {last['run_id']} for {last['run_date']} ({last['state']}), "
                   f"{last['rows_processed']} rows decided, {last['attempts_email']} emails and {last['attempts_phone']} calls."))

st.subheader("Where to go")
c1, c2 = st.columns(2)
with c1:
    st.markdown("**Run**  \nRun the day's follow-ups, see what happened and the data quality results.")
with c2:
    st.markdown("**Human Queue**  \nDecisions waiting for you, rows with a person, and rows that failed data quality.")
st.caption(f"Run date is set to {run_date(data['config']).isoformat()}. Change it on the Run page.")
