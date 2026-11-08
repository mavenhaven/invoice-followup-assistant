"""Run page: start the day's follow-ups and see what happened."""
from __future__ import annotations

import random

import streamlit as st

from app_common import esc, show_error, bump_version, get_store, load_data, run_date, show_flash, sidebar
from core.quality import parse_date
from core.runner import RunDateError, RunInProgressError, RunResult, run_daily
from core.schema import Col

st.set_page_config(page_title="Run follow-ups", page_icon=":material/play_circle:", layout="wide")

store = get_store()
sidebar_actor = sidebar()
st.title("Run follow-ups")
show_flash()

st.write("Each run checks data quality, applies decisions typed into the sheet, then contacts everyone who is due. "
         "Email comes first, then phone, at most once per contact per day. **Running again on the same date is safe: "
         "rows already processed that day are skipped.**")

data = load_data(store)
c1, c2 = st.columns([1, 2])
with c1:
    chosen = st.date_input("Run date", value=run_date(data["config"]), key="run_date",
                           help=f"Today in {data['config'].timezone}. Use a later date to simulate following days; "
                                "contacts only get one attempt per date.")
with c2:
    with st.expander("Mock responses"):
        st.caption("No real email or calls are made. Every contact gets a simulated reply.")
        fixed = st.checkbox("Use a fixed random seed (same replies every time)", key="use_seed")
        seed = st.number_input("Seed", min_value=0, value=7, step=1, disabled=not fixed, key="seed")

df = data["invoices"]
latest = None
if not df.empty and Col.LAST_PROCESSED_DATE in df.columns:
    processed = [parse_date(v) for v in df[Col.LAST_PROCESSED_DATE] if v]
    latest = max((p for p in processed if p), default=None)
too_early = latest is not None and chosen < latest
if too_early:
    st.error(f"Rows were already processed on {latest.isoformat()}, so {chosen.isoformat()} is too early. "
             "Pick that date or a later one: running an earlier date would contact people out of order.")
if not df.empty:
    done = int((df[Col.LAST_PROCESSED_DATE] == chosen.isoformat()).sum()) if Col.LAST_PROCESSED_DATE in df.columns else 0
    m1, m2, m3 = st.columns(3)
    m1.metric("Rows in the sheet", len(df))
    m2.metric(f"Already processed on {chosen.isoformat()}", done)
    m3.metric("Still to decide on this date", len(df) - done)
    if done == len(df) > 0:
        st.success("Every row has already been processed for this date. A run now would change nothing.")


def show_result(result: RunResult) -> None:
    if result.state.value != "DONE":
        st.error(f"The run ended as {result.state.value}.")
    elif not result.reconciled:
        st.error("The four status groups do not add up to the number of rows. Please check the sheet.")
    else:
        st.success(f"Run {result.run_id} finished. Every row is accounted for.")
    cols = st.columns(6)
    cols[0].metric("Rows decided", result.rows_processed)
    cols[1].metric("Skipped: done today", result.rows_skipped_already_processed)
    cols[2].metric("Skipped: not due yet", result.rows_skipped_not_due)
    cols[3].metric("Emails sent", result.attempts_email)
    cols[4].metric("Calls made", result.attempts_phone)
    cols[5].metric("Sent to a person", result.rows_escalated)

    st.subheader("Where every row stands")
    names = [("in_progress", "In progress"), ("needs_human", "Needing human follow-up"),
             ("dq_failed", "Data quality failed"), ("handled", "Handled")]
    b = st.columns(5)
    for col, (code, label) in zip(b, names):
        col.metric(label, result.buckets.get(code, 0))
    b[4].metric("Total rows", result.rows_total, delta="adds up" if result.reconciled else "does not add up",
                delta_color="normal" if result.reconciled else "inverse")

    if result.dq_summary is not None:
        st.subheader("Data quality")
        s = result.dq_summary
        q = st.columns(3)
        q[0].metric("Passed", s.by_status["PASS"])
        q[1].metric("Passed with warnings", s.by_status["WARN"])
        q[2].metric("Failed", s.by_status["FAIL"])
        if not s.issues.empty:
            st.dataframe(s.issues.rename(columns={"issues": "occurrences"}), hide_index=True, width="stretch")
        if s.excluded_by_reason:
            st.caption("Not contacted, and not a data quality failure: "
                       + ", ".join(f"{n} {reason.replace('_', ' ').lower()}" for reason, n in s.excluded_by_reason.items()))

    problems = [e for e in result.errors if e.get("type") in ("error", "fatal", "reconciliation")]
    notices = [e for e in result.errors if e.get("type") == "sheet_edit"]
    if problems:
        with st.expander(f"{len(problems)} problem(s) during the run", expanded=True):
            for e in problems:
                st.error(esc(f"{', '.join(e.get('row_keys', [])) or 'Run'}: {e['message']}"))
    if notices:
        with st.expander(f"{len(notices)} note(s) about edits made in the sheet"):
            for e in notices:
                st.warning(esc(f"{e['code']}: {e['message']} ({', '.join(e['row_keys'])})"))
    if result.rows_escalated or result.buckets.get("needs_human") or result.buckets.get("dq_failed"):
        try:
            st.page_link("pages/2_Human_Queue.py", label="Open the Human Queue", icon=":material/arrow_forward:")
        except Exception:  # noqa: BLE001 - page links need the multipage app
            st.caption("Open the Human Queue page to see them.")


if st.button("Run follow-ups", type="primary", disabled=too_early):
    bar = st.progress(0.0, text="Starting...")

    def progress(done: int, total: int, label: str) -> None:
        bar.progress(min(done / total, 1.0) if total else 1.0, text=label)

    rng = random.Random(int(seed)) if fixed else random.Random()
    try:
        with st.spinner("Running..."):
            result = run_daily(store, chosen, rng=rng, progress=progress, started_by=sidebar_actor)
        bar.progress(1.0, text="Done")
        st.session_state["last_result"] = result
        bump_version()
    except RunInProgressError as exc:
        bar.empty()
        st.warning(esc(f"Another run is in progress: {exc}"))
    except RunDateError as exc:
        bar.empty()
        st.error(esc(str(exc)))
    except Exception as exc:  # noqa: BLE001 - the run is already logged as FAILED in Run_Log
        bar.empty()
        show_error("The run failed. Rows already processed are saved, so running again continues where it stopped.", exc)

if "last_result" in st.session_state:
    show_result(st.session_state["last_result"])

st.subheader("Recent runs")
runs = load_data(store)["runs"]
if runs.empty:
    st.info("No runs yet.")
else:
    shown = runs.iloc[::-1].head(10)[["run_id", "run_date", "state", "rows_total", "rows_processed",
                                      "rows_skipped_already_processed", "attempts_email", "attempts_phone",
                                      "rows_escalated", "finished_at"]]
    st.dataframe(shown, hide_index=True, width="stretch")
