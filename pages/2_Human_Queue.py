"""Human Queue page: decisions waiting, rows with a person, and rows that failed data quality."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd
import streamlit as st

from app_common import esc, secret_values, bump_version, flash, get_store, load_data, run_date, show_flash, sidebar
from core.redact import safe_error
from core.actions import ActionError, decide_extension, resolve_row_action, sync_sheet_edits
from core.queue import QueueView, build_queue

st.set_page_config(page_title="Human queue", page_icon=":material/person_alert:", layout="wide")

store = get_store()
actor = sidebar()
data = load_data(store)
today = run_date(data["config"])

st.title("Human queue")
show_flash()

# ------------------------------------------------------------------ toolbar
t1, t2, t3 = st.columns([1, 1, 4])
if t1.button("Refresh", help="Read the sheet again"):
    bump_version()
    st.rerun()
if t2.button("Sync sheet edits", help="Apply decisions typed straight into the Google Sheet"):
    try:
        result = sync_sheet_edits(store, today)
        st.session_state["sync_issues"] = [dataclasses.asdict(i) for i in result.issues]
        flash("success" if result.changed else "info",
              f"Applied decisions from the sheet to {len(result.updates)} row(s)." if result.changed else "No decisions typed into the sheet were waiting.")
    except ActionError as exc:
        flash("error", str(exc))
    bump_version()
    st.rerun()
with t3:
    with st.expander("How to decide in the Google Sheet instead"):
        st.markdown(
            "Open the invoice sheet and find a row where **status** is `NEEDS_HUMAN` and **human_reason** is "
            "`EXTENSION_APPROVAL_PENDING`. Change **approval_status** from `PENDING` to `APPROVED` or `REJECTED`. "
            "You can edit **pending_approval_date** first to approve a different date. Editing one row is enough: "
            "every invoice of that contact waiting on the same date follows. Then press **Sync sheet edits** here "
            "(the next run also does it). The sheet does not record who edited a cell; the sheet's *Version history* does.")

for issue in st.session_state.get("sync_issues", []):
    st.warning(esc(f"{issue['code']}: {issue['message']} ({', '.join(issue['row_keys'])})"))

view: QueueView = build_queue(data["invoices"], data["audit"], today, data["config"])

m = st.columns(4)
m[0].metric("Decisions waiting", view.counts["pending_contacts"], help="One per contact and requested date")
m[1].metric("Invoices waiting on a decision", view.counts["pending_rows"])
m[2].metric("Needing human follow-up", view.counts["human"])
m[3].metric("Data quality failures", view.counts["dq"])
if not actor:
    st.info("Enter your name in the sidebar to approve, reject or resolve. It is recorded in the audit log.")


def act(fn, *args, **kwargs) -> None:
    try:
        flash("success", fn(*args, **kwargs))
    except ActionError as exc:
        flash("error", esc(str(exc)))
    except Exception as exc:  # noqa: BLE001 - Google being busy, a tab deleted mid-action: say so without a stack trace
        flash("error", esc("That did not complete: " + safe_error(exc, secret_values())
                           + ". Some of it may have been saved. Press Refresh to see where things stand."))
    bump_version()
    st.rerun()


# ------------------------------------------------------------------ section 1: decisions waiting
st.header("Decisions waiting", divider="blue")
st.caption("A contact asked to pay later than 5 days from the run date. A person must approve it, and each contact "
           "can have at most 3 approvals. One promise covering several invoices is decided once.")
if not view.pending:
    st.success("Nothing is waiting for a decision.")
for p in view.pending:
    key = f"{p.contact_key}-{p.pending_date.isoformat()}"
    with st.container(border=True):
        st.markdown(f"**{esc(p.customer)}** · {esc(p.contact_name)}")
        st.write(f"Wants to pay on **{p.pending_date.isoformat()}** ({p.days_out} days from {today.isoformat()}). "
                 f"Approvals used by this contact: **{p.approvals_used} of {p.max_approvals}**.")
        st.dataframe(pd.DataFrame(p.invoices).drop(columns=["row_key"]).rename(
            columns={"invoice_number": "Invoice", "outstanding": "Outstanding", "due_date": "Due"}),
            hide_index=True, width="stretch")
        if p.last_reply:
            with st.expander(f"Their last reply ({p.asked_on})"):
                st.text(p.last_reply)
        a, b, _ = st.columns([1, 1, 4])
        if a.button("Approve", key=f"approve-{key}", type="primary", disabled=not (actor and p.can_approve)):
            act(decide_extension, store, p.contact_key, approve=True, actor=actor, today=today, pending_date=p.pending_date)
        if b.button("Reject", key=f"reject-{key}", disabled=not actor):
            act(decide_extension, store, p.contact_key, approve=False, actor=actor, today=today, pending_date=p.pending_date)
        if not p.can_approve:
            st.warning("This contact has used all their approvals, so this date cannot be approved. Reject it, then decide what to do next.")

# ------------------------------------------------------------------ section A: data quality
st.header("A. Data quality failures", divider="red")
st.caption("These rows could not be processed and are never contacted. Fix the data in the Google Sheet; the next "
           "run checks them again and puts them back in follow-up once they pass.")
if view.dq.empty:
    st.success("No rows failed the data quality checks.")
else:
    st.dataframe(view.dq.rename(columns={"invoice_number": "Invoice", "customer": "Customer", "country": "Country",
                                         "contact_name": "Contact", "problem": "What is wrong", "what_to_do": "Type",
                                         "notes": "Notes"}).drop(columns=["row_key"]),
                 hide_index=True, width="stretch")

# ------------------------------------------------------------------ section B: with a person
st.header("B. Needing human follow-up", divider="orange")
st.caption("The app stopped following up on these. Nothing happens to them until a person resolves them.")
human = view.human
if human.empty:
    st.success("No rows need human follow-up.")
else:
    shown = ["invoice_number", "customer", "country", "contact_name", "outstanding", "due_date", "last_outcome",
             "last_summary", "email_attempts", "phone_attempts", "notes"]
    labels = {"invoice_number": "Invoice", "customer": "Customer", "country": "Country", "contact_name": "Contact",
              "outstanding": "Outstanding", "due_date": "Due", "last_outcome": "Last outcome", "last_summary": "Last summary",
              "email_attempts": "Emails", "phone_attempts": "Calls", "notes": "Notes"}
    for reason, group in human.groupby("reason", sort=True):
        st.subheader(f"{reason} ({len(group)})")
        st.dataframe(group[shown].rename(columns=labels), hide_index=True, width="stretch")

    st.subheader("Resolve a row")
    options = dict(zip(human["row_key"], human["invoice_number"] + " · " + human["customer"] + " · " + human["reason"]))
    picked = st.selectbox("Row", list(options), format_func=options.get, key="resolve_pick")
    row = human[human["row_key"] == picked].iloc[0]
    with st.form("resolve_form", clear_on_submit=True):
        what = st.radio("What now?", ["Return to follow-up", "Close (mark as handled)"], key="resolve_what")
        note = st.text_area("Note (saved with the row, never overwrites the Notes column)", key="resolve_note")
        r1, r2 = st.columns(2)
        reset = r1.checkbox("Give the contact 5 fresh email and 5 fresh phone attempts", key="resolve_reset",
                            help="Contacts reached the limit of 5 per channel. History stays in the audit log.")
        reenable = r2.checkbox("Switch bounced or disabled channels back on", key="resolve_reenable",
                               help="Tick after you fixed the email address or phone number in the sheet.")
        use_date = st.checkbox("Wait until a given date before the next contact", key="resolve_use_date")
        when = st.date_input("Next follow-up date", value=today + dt.timedelta(days=2), key="resolve_date", disabled=not use_date)
        if row["last_summary"]:
            st.caption(esc(f"Last summary: {row['last_summary']}"))
        if st.form_submit_button("Save", type="primary", disabled=not actor):
            act(resolve_row_action, store, picked, actor=actor, today=today, close=what.startswith("Close"),
                reset_attempts=reset, reenable_channels=reenable, note=note.strip(),
                next_followup_date=when if use_date and what.startswith("Return") else None)
