"""About page: what the app does, how it is built, and the rules it follows. Works before the sheet is connected."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app_common import try_load_config
from core.rules_catalog import (
    ARCHITECTURE_DOT, ASSUMPTIONS, COLUMN_DOCS, DQ_RULES, LIFECYCLE_DOT, LIMITATIONS, PAGES, RULES, SETTING_DOCS, STATUS_TABLE, TABS,
)
from core.schema import CONFIG_SHEET, INVOICES_EXISTING_COLUMNS, INVOICES_NEW_COLUMNS, Config
from core.taxonomy import Category, taxonomy_table

st.set_page_config(page_title="About", page_icon=":material/info:", layout="wide")

live = try_load_config()
config = live or Config()

st.title("About the Invoice Follow-Up Assistant")
st.caption("Design specification. Every rule below is read from the same list the app's code is tested against.")

SECTIONS = ["What it is for", "How it works", "The Google Sheet", "Row status", "Outcome taxonomy", "Data quality rules",
            "Business rules implemented", "Settings", "Assumptions", "Limitations"]
st.markdown("**On this page:** " + " · ".join(f"[{s}](#{s.lower().replace(' ', '-')})" for s in SECTIONS))

design = Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md"
if design.exists():
    st.download_button("Download the full design specification (Markdown)", design.read_bytes(), file_name="DESIGN.md", mime="text/markdown")

# ------------------------------------------------------------------ purpose
st.header("What it is for", divider="gray")
st.write("Follow up with contacts about overdue invoices, keep track of every attempt, and get the invoices paid. "
         "An invoice that is paid, or no longer needs follow-up, counts as handled. Anything the app cannot or should not decide "
         "goes to a person, in a list of its own.")
st.markdown("- It contacts by **email first**, then **phone**, because email is cheaper.\n"
            f"- It never makes more than **{config.attempt_cap} attempts** per channel, and never contacts a contact twice in a day.\n"
            f"- It listens to what the contact says: a date within {config.promise_max_days} days is followed, a later date needs a person's approval.\n"
            "- Everything lives in **one Google Sheet**: the data, the audit trail, the run log and the status. No other database.\n"
            "- No real email is sent and no real call is made yet: replies are simulated so every outcome can be tested.")

# ------------------------------------------------------------------ how it works
st.header("How it works", divider="gray")
st.graphviz_chart(ARCHITECTURE_DOT, width="stretch")
st.subheader("A daily run")
st.markdown("1. **Check the sheet** layout and take the run lock.\n"
            "2. **Read every row** once and check data quality. Rows seen for the first time are set up.\n"
            "3. **Pick up decisions** people typed straight into the sheet.\n"
            "4. **For each contact:** decide who is due, choose email or phone, simulate the reply, apply the rules, and write the result and an audit entry.\n"
            "5. **Write the run log** and check that the four status groups add up to the number of rows.")
st.subheader("The pages")
st.dataframe(pd.DataFrame(PAGES, columns=["Page", "What it does"]), hide_index=True, width="stretch")

# ------------------------------------------------------------------ the sheet
st.header("The Google Sheet", divider="gray")
st.dataframe(pd.DataFrame(TABS, columns=["Tab", "What is in it"]), hide_index=True, width="stretch")
st.subheader("Your columns on the Invoices tab")
st.caption("Kept exactly as they are: the app never renames or reorders them, and never writes to Notes.")
st.dataframe(pd.DataFrame([(c, COLUMN_DOCS[c]) for c in INVOICES_EXISTING_COLUMNS], columns=["Column", "How the app uses it"]),
             hide_index=True, width="stretch")
st.subheader("Columns the app adds")
st.dataframe(pd.DataFrame([(c, COLUMN_DOCS[c]) for c in INVOICES_NEW_COLUMNS], columns=["Column", "What it holds"]),
             hide_index=True, width="stretch")

# ------------------------------------------------------------------ status
st.header("Row status", divider="gray")
st.write("Every row is in exactly one of four groups. They always add up to the number of rows.")
st.dataframe(pd.DataFrame(STATUS_TABLE, columns=["Group", "Meaning"]), hide_index=True, width="stretch")
st.graphviz_chart(LIFECYCLE_DOT, width="stretch")

# ------------------------------------------------------------------ taxonomy
st.header("Outcome taxonomy", divider="gray")
table = pd.DataFrame(taxonomy_table())
st.write(f"Every attempt ends in one of {len(table)} outcomes. 'Uses an attempt' says whether it counts towards the limit; "
         "'real response' means a person engaged. Failures of our own never use an attempt.")
chosen = st.multiselect("Show categories", [c.value for c in Category], default=[c.value for c in Category], key="about_categories")
shown = table[table["category"].isin(chosen)].rename(columns={
    "code": "Outcome", "category": "Category", "description": "Meaning", "channels": "Channels", "counts_as_attempt": "Uses an attempt",
    "real_response": "Real response", "next_action": "What happens next"})
st.dataframe(shown, hide_index=True, width="stretch")

# ------------------------------------------------------------------ data quality
st.header("Data quality rules", divider="gray")
st.write("Each row is checked before anything else. **BLOCK** means the row cannot be processed and goes to a person; "
         "**WARN** means it is processed and the issue is shown; **INFO** is noted only.")
st.dataframe(pd.DataFrame([(r.id, r.severity, r.title, r.text) for r in DQ_RULES], columns=["Rule", "Worst case", "Check", "What it looks for"]),
             hide_index=True, width="stretch")

# ------------------------------------------------------------------ business rules
st.header("Business rules implemented", divider="gray")
checks = sum(len(r.tests) for r in RULES)
st.write(f"**{len(RULES)} rules**, each tied to the code that implements it and to the automated tests that prove it ({checks} checks in all). "
         "Numbers in the rules come from your Config sheet." + ("" if live else " (Showing the defaults: the app is not connected to a sheet.)"))
find = st.text_input("Find a rule", key="about_find", placeholder="for example: approval, email, once a day")
matches = [r for r in RULES if not find.strip()
           or find.strip().lower() in f"{r.id} {r.rendered_title(config)} {r.rendered(config)} {r.rendered_example(config)}".lower()]
if not matches:
    st.info("No rule matches that.")
for r in matches:
    with st.container(border=True):
        st.markdown(f"**{r.id} · {r.rendered_title(config)}**")
        st.write(r.rendered(config))
        st.caption("For example: " + r.rendered_example(config))
        with st.expander("Where it lives and how it is tested"):
            st.markdown("**Code:** " + ", ".join(f"`{c}`" for c in r.code))
            st.markdown("**Tests:**\n" + "\n".join(f"- `{t}`" for t in r.tests))

# ------------------------------------------------------------------ settings
st.header("Settings", divider="gray")
st.write(f"Kept in the **{CONFIG_SHEET}** tab of the sheet. Change a value there and the next run uses it. "
         + ("These are your current values." if live else "These are the defaults."))
current = {"attempt_cap": config.attempt_cap, "promise_max_days": config.promise_max_days, "max_approvals": config.max_approvals,
           "default_retry_days": config.default_retry_days, "system_error_limit": config.system_error_limit, "invoice_prefix": config.invoice_prefix,
           "timezone": config.timezone, "skip_below_minimum": "yes" if config.skip_below_minimum else "no",
           "legacy_migration_done": "yes" if config.legacy_migration_done else "no"}
rows = [(k, SETTING_DOCS[k], str(current[k])) for k in SETTING_DOCS]  # all text: a column that mixes numbers and words cannot be shown by every table library version
rows += [(f"min_amount_{cur}", f"Smallest amount worth chasing, in {cur}", f"{amount:g}") for cur, amount in config.min_amount.items()]
st.dataframe(pd.DataFrame(rows, columns=["Setting", "What it controls", "Value"]), hide_index=True, width="stretch")

# ------------------------------------------------------------------ assumptions and limits
st.header("Assumptions", divider="gray")
st.dataframe(pd.DataFrame(ASSUMPTIONS, columns=["#", "Status", "Assumption"]), hide_index=True, width="stretch")

st.header("Limitations", divider="gray")
for item in LIMITATIONS:
    st.markdown(f"- {item}")
