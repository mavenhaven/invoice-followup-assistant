# Prompt Pack: Unpaid Invoice Follow-Up App (Streamlit + Google Sheets)

This pack is tailored to your real sheet: 1 tab, 35 data rows, 18 columns (listed in the Context Block). Only the first rows were read, so Prompt 1 still asks the LLM to profile a pasted sample before finalizing the design.

## How to use
1. Start a **new chat per prompt**. Paste the **Context Block** first, then the prompt. For Prompts 1, 4, 5 and 6 also paste the **Data profile** section, since they rely on its facts and decisions.
2. After each prompt, ask the LLM for a **Handoff Summary** (the last line of every prompt does this). Paste that summary into the next chat instead of the whole code history.
3. Keep one repo; each prompt only touches the files it names.
4. Do the prompts in order. Prompt 1 (design) comes first, as you asked.

## Context Block (paste at the top of every chat)
```
You are a senior Python engineer. We are building "Invoice Follow-Up Assistant":
- Streamlit app, deployed on Streamlit Community Cloud, Python 3.11.
- Only data store: one Google Sheet (read/write via gspread + service account in st.secrets). No other DB.
- Purpose: follow up with contacts on unpaid invoices, track attempts, get invoices paid.
  A row that is paid or no longer needs follow-up counts as handled.
- Contact is simulated: every run generates a mock email reply or phone-call transcript.
- Existing sheet columns (in order): Invoice Number, Customer, Country, Contact Name, Email,
  Phone, Invoice Date, Due Date, Invoice Amount, Amount Outstanding, Currency, Last Contact Date,
  Contact Attempts, Ready for Contact, Outcome, Summary, Last Updated, Notes.
- Data facts: Country in {AT, CZ, DE, HU, RO}; Currency in {EUR, CZK, HUF, RON} (never sum across
  currencies); Ready for Contact is Yes/No/blank; one customer can have several invoices
  (e.g. Veltner Gastro GmbH has 3); INV-2026-4471 appears twice (duplicate).
- Existing Outcome values: CALLBACK_REQUESTED, DISPUTE, NO_ANSWER, OUT_OF_OFFICE, PAID,
  PARTIAL_PAYMENT, PROMISE_TO_PAY, SETTLEMENT_REQUESTED, WRONG_CONTACT, blank.
- "Contact location" in the UI = the Country column. "Ready for Contact" is the row's
  ability-to-contact flag. Do not rename or reorder existing columns; only append new ones.
- Repo layout: app.py, pages/, core/ (sheets.py, schema.py, quality.py, taxonomy.py,
  mock_responses.py, rules.py, runner.py), tests/, docs/, requirements.txt.
- Rules: small modules, type hints, docstrings, pure functions in core/ (no Streamlit imports),
  unit tests with pytest, never print secrets.
Previous handoff summary (if any): <PASTE HERE>
```

---

## Data profile (35 rows, from the CSV)
- **Outcomes already recorded (13 rows):** NO_ANSWER x4, PROMISE_TO_PAY x2, PAID, PARTIAL_PAYMENT, DISPUTE, CALLBACK_REQUESTED, WRONG_CONTACT, SETTLEMENT_REQUESTED, OUT_OF_OFFICE. 22 rows have no history. Prior contact data exists only as one Contact Attempts total with no email/phone split.
- **Data quality problems:** 1 duplicate invoice (INV-2026-4471), 1 credit note (CN-2026-0118, negative), 1 row with no email and no phone (INV-2026-4144), 3 rows with a missing or invalid email (4301, 4276, 4066), 1 phone without a country code (4189), 1 blank Ready for Contact (4362), 1 tiny amount of 2.50 EUR (4207).
- **Not contactable by design:** Ready for Contact = No (3980 insolvency, 4415 on hold); paid (4265).
- **Promise-date cases for the 5-day rule:** INV-2026-4233 promised payment on 31 Aug after contact on 18 Aug (13 days, so it needs human approval); INV-2026-4021 broke a promise of 30 June.
- **Attempt-cap cases:** INV-2026-3902 already has 5 attempts and no contact; 3955 has 4 and 3980 has 4.
- **Status/outcome mismatch:** INV-2026-4444 is recorded NO_ANSWER but its Summary says the line was busy.
- **Several invoices per contact:** Veltner Gastro (3), Nordbach (3 rows, 2 of them the duplicate), Rheinfeld (2).
- **Decisions made (treat as fixed in Prompt 1 and later):**
  1. Ready for Contact = No rows count as Needing human follow-up, with the Notes text as the reason.
  2. A contact with several invoices gets one combined message or call per day covering all their open invoices. The one-contact-per-day rule applies per contact, not per invoice.
  3. Legacy attempt totals: if Summary says "Called", assign the attempts to phone; otherwise keep them in legacy_attempts with an unknown channel. An unknown-channel total of 5 or more sets the human flag (ATTEMPT_CAP_LEGACY).
  4. A blank Ready for Contact is a warning; the app derives the flag and writes it back.
  5. Minimum-amount thresholds (EUR 10, CZK 250, HUF 4000, RON 50) are placeholders held in Config; edit them in the sheet.

## Prompt 1: Design spec (FIRST)
```
Task: produce docs/DESIGN.md only. No app code yet.
Input: the 18 existing columns are in the Context Block; here are 10 sample rows: <PASTE>.
1. Profile the data: for each existing column give type, purpose, nullability. Decide how
   to use the existing columns: Contact Attempts (legacy total; keep it updated as the sum of
   the per-channel counters), Last Contact Date, Ready for Contact (the row-level
   ability-to-contact flag; define blank = not evaluated), Outcome (latest outcome code),
   Summary (latest response summary), Last Updated, Notes (human notes, never overwritten).
   Propose NEW columns to APPEND (do not touch existing ones): email_attempts, phone_attempts,
   email_responses, phone_responses, next_followup_date, status, dq_status, dq_issues,
   can_contact_email, can_contact_phone, human_flag, human_reason,
   extension_approvals_count, pending_approval_date, last_processed_date, row_key.
   State how the contact is identified for the one-contact-per-day rule (recommend Email, else
   Phone, falling back to Contact Name + Customer), given a customer can have several invoices.
   Currency handling: amounts are EUR/CZK/HUF/RON, so never sum across currencies.
2. Propose extra worksheets: Audit_Log (one row per attempt: run_id, timestamp, row key,
   channel, attempt_no, outcome, response_text, promised_date), Run_Log (run_id, start, end,
   rows processed/skipped/failed, errors), Config (limits).
3. Define the Outcome taxonomy (table: code, category, description, next action):
   Keep the codes already in the sheet and map them: PAID, PARTIAL_PAYMENT, PROMISE_TO_PAY,
   SETTLEMENT_REQUESTED, CALLBACK_REQUESTED, DISPUTE, WRONG_CONTACT, OUT_OF_OFFICE, NO_ANSWER.
   Contact made: PAID, PARTIAL_PAYMENT, PROMISE_TO_PAY, PAYMENT_IN_PROCESS, SETTLEMENT_REQUESTED,
   CALLBACK_REQUESTED, DISPUTE, REQUESTS_INVOICE_COPY, WRONG_CONTACT, REFUSED_TO_PAY.
   No contact: NO_ANSWER, OUT_OF_OFFICE, LEFT_VOICEMAIL.
   System/channel errors: EMAIL_BOUNCE_HARD, EMAIL_BOUNCE_SOFT, EMAIL_INVALID_ADDRESS,
   EMAIL_SEND_FAILURE, PHONE_INVALID_NUMBER, PHONE_LINE_BUSY, PHONE_DISCONNECTED,
   PHONE_SYSTEM_ERROR, SYSTEM_ERROR.
   Terminal/escalation: NEEDS_HUMAN_FOLLOW_UP, MAX_ATTEMPTS_REACHED, DQ_FAILED.
   Add or remove codes based on the real data.
4. Write a state machine for a row (e.g. NEW -> IN_PROGRESS -> PAID/HANDLED or
   NEEDS_HUMAN_FOLLOW_UP or DQ_FAILED) and a flowchart in Mermaid.
5. Write the business rules as numbered rules, each with ID (BR-01...), plain-English text,
   and a testable example:
   - Email first, then phone. Max 5 attempts per channel. After 5 email attempts with no
     real response switch to phone. If both channels reach 5 with no response ->
     NEEDING HUMAN FOLLOW UP.
   - Never contact the same contact more than once per calendar day. A row already
     processed today is skipped on any re-run (idempotent).
   - If the contact states a follow-up date: <= 5 days out -> use it as next_followup_date;
     > 5 days out -> requires human approval. Max 3 approvals per contact; the 4th request
     -> NEEDING HUMAN FOLLOW UP.
   - Paid / closed rows are not contacted.
   - Rows failing data quality go to a separate "Human follow-up: Data Quality" queue.
   - Define whether "5 attempts" is a hard cap of 5 (recommended) and state the assumption.
6. Dashboard spec: KPIs, filters (contact location, contact search), drill-down, success
   rate definitions (attempt success, response rate, failure rate), and the reconciliation
   rule: DQ-failed + Needs-human + In-progress + Handled = Total rows.
7. Page map: Run, Human Queue (DQ vs follow-up separated), Dashboard, About.
8. Apply the "Decisions made" listed in the Data profile section as fixed requirements, and
   list any remaining open questions and assumptions.
Finish with a "Handoff Summary" (<=300 words) of the decisions.
```

## Prompt 2: Scaffold + Google Sheets layer
```
Task: create requirements.txt, core/schema.py, core/sheets.py, .streamlit/secrets.toml.example,
and tests/test_sheets.py (with a fake in-memory sheet).
- schema.py: column constants and dataclasses from the design handoff.
- sheets.py: connect with gspread using st.secrets["gcp_service_account"] and
  st.secrets["sheet_id"]; read the main sheet to a DataFrame (with a stable row key);
  batch-update specific cells (not full rewrites); append to Audit_Log and Run_Log; create
  missing worksheets/columns on first run; retry with backoff on 429.
- Keep Streamlit out of sheets.py by accepting a client in the constructor.
End with a Handoff Summary including public function signatures.
```

## Prompt 3: Taxonomy + mock response generator
```
Task: core/taxonomy.py, core/mock_responses.py, tests/test_mock_responses.py.
- taxonomy.py: Enum of outcome codes, category, and helper fns (is_real_response(code),
  is_system_error(code), extracts promised days).
- mock_responses.py: generate_response(channel, contact, invoice, rng) returns
  (outcome_code, response_text, promised_days|None). Email: realistic replies with
  greetings and signatures. Phone: transcripts with Agent:/Contact: turns.
  Weighted random so every outcome code can occur, including promised dates of
  2, 5, 7, 14 and 30 days, bounces, busy lines, voicemail and system errors.
  Accept a seed so tests are reproducible, and a forced_outcome parameter for testing.
- Test: over 5,000 draws, every outcome appears at least once.
Handoff Summary at the end.
```

## Prompt 4: Data quality engine
```
Task: core/quality.py, tests/test_quality.py, tests/fixtures/overdue_invoices.csv (copy of the
real 35-row file). Pure functions on DataFrames. Each check has a rule ID (DQ-01..), a severity
and a stable issue code. Severities: BLOCK (row cannot be processed -> Human Queue A, data
quality), WARN (row still processed, issue logged/shown), INFO.

Rules (derived from profiling the real file):
DQ-01 BLOCK  MISSING_REQUIRED: Invoice Number, Customer, Country, Contact Name, Due Date,
             Amount Outstanding, Currency must be non-blank (trim whitespace).
DQ-02 BLOCK  DUPLICATE_INVOICE: same Invoice Number more than once. Keep the first occurrence
             as the master (it keeps being processed); every later occurrence is BLOCK and
             references the master's row key. If the duplicates differ in any field, escalate
             all occurrences instead. (INV-2026-4471 occurs twice; the second has a Notes
             value "Duplicate export row".)
DQ-03 BLOCK  NOT_A_COLLECTIBLE_INVOICE: Invoice Number prefix is not "INV-" or Amount
             Outstanding < 0 (credit note CN-2026-0118, -1450 EUR). Route to finance review,
             never contact. Make the accepted prefix a Config value.
DQ-04 BLOCK  NO_CONTACT_CHANNEL: neither email nor phone is usable after validation
             (INV-2026-4144 has both blank).
DQ-05 WARN   EMAIL_MISSING / EMAIL_INVALID -> can_contact_email = False. Valid = single @,
             non-empty local part, domain with at least one dot, a 2+ letter TLD, and no
             consecutive dots. Real examples: blank (INV-2026-4301), "t.blaha@silesiafresh"
             no TLD (INV-2026-4276), "r.stan@adriaticprov..ro" double dot (INV-2026-4066).
DQ-06 WARN   PHONE_MISSING / PHONE_INVALID -> can_contact_phone = False. Normalize to E.164:
             strip spaces/dashes/brackets, convert a leading 00 to +, and if there is no
             country code and the number starts with a single 0, apply the Country dial
             code (AT +43, CZ +420, DE +49, HU +36, RO +40) and drop that 0. Real example:
             "0170 4471129" (DE) -> +491704471129, recorded as PHONE_NORMALIZED (INFO).
             After normalization, require 8-15 digits. A phone whose country code does not
             match the Country is WARN PHONE_COUNTRY_MISMATCH but still usable.
DQ-07 WARN   READY_FLAG_BLANK: Ready for Contact is blank (INV-2026-4362). Treat as "not
             evaluated"; the app derives the flag from the channel checks and writes it
             back as Yes/No. Any value other than Yes/No (case-insensitive) is BLOCK
             READY_FLAG_INVALID.
DQ-08 INFO   READY_FLAG_NO: Ready for Contact = No (INV-2026-3980 insolvency, INV-2026-4415
             on hold). Not a DQ failure and never contacted; hand to the rules engine as a
             hard stop with human_reason = Notes text.
DQ-09 INFO   ALREADY_SETTLED: Outcome = PAID or Amount Outstanding = 0 (INV-2026-4265).
             Not a DQ failure; status = HANDLED, never contacted.
DQ-10 BLOCK  AMOUNT_INVALID: Amount Outstanding or Invoice Amount not numeric, or Amount
             Outstanding > Invoice Amount. (Amount Outstanding < Invoice Amount is valid:
             INV-2026-4199 has 1225 of 9800 outstanding after a partial payment.)
DQ-11 WARN   AMOUNT_BELOW_MINIMUM: Amount Outstanding > 0 but below a per-currency minimum in
             Config (default: EUR 10, CZK 250, HUF 4000, RON 50). Real example: INV-2026-4207
             is 2.50 EUR, where the cost of chasing exceeds the debt. Still processed unless
             Config says "skip below minimum".
DQ-12 BLOCK  DATE_INVALID: Invoice Date or Due Date unparseable (ISO yyyy-mm-dd) or
             Due Date < Invoice Date. Payment terms of 30 days are typical, but INV-2026-4133
             has 60 days (contract note); terms outside 15-90 days are only INFO.
DQ-13 BLOCK  CURRENCY_INVALID / COUNTRY_INVALID: Currency not in {EUR, CZK, HUF, RON} or
             Country not in {AT, CZ, DE, HU, RO}. A currency not matching the Country
             (DE/AT=EUR, CZ=CZK, HU=HUF, RO=RON) is WARN CURRENCY_COUNTRY_MISMATCH.
DQ-14 INFO   NOT_YET_DUE: Due Date is after the run date (as-of date is a parameter, not
             hidden "now"). Not contacted until overdue (INV-2026-4512 is due 2026-08-29).
DQ-15 WARN   HISTORY_INCONSISTENT: Contact Attempts > 0 but Last Contact Date blank or
             Outcome blank (or the reverse), or Last Updated earlier than Last Contact
             Date. Parse Last Updated tolerantly: "2026-08-04 10:12" and "2026-06-15 9:05"
             (hour without a leading zero) must both parse.
DQ-16 WARN   LEGACY_ATTEMPTS_NO_CHANNEL: Contact Attempts is a single total with no email/phone
             split (e.g. INV-2026-3902 has 5, INV-2026-3955 has 4). Emit a migration
             suggestion: if Summary says "Called" assign to phone; otherwise mark unknown and
             keep the total in a legacy_attempts column. An unknown-channel total >= 5
             sets human_flag with reason ATTEMPT_CAP_LEGACY (rules engine decides).
DQ-17 INFO   CONTACT_SHARED_ACROSS_ROWS: the same contact (email, else phone, else
             Contact Name + Customer) appears on several rows (Veltner Gastro/Thomas Berger
             x3, Nordbach/Katrin Vogel x3, Rheinfeld/Uwe Lehmann x2). Add a contact_key
             column and rows-per-contact count; this feeds the one-contact-per-day rule.
DQ-18 INFO   ENTITY_SUFFIX_MISMATCH: legal suffix inconsistent with Country (e.g. "Sp. z o.o."
             is Polish but the row is CZ, INV-2026-4276). Informational only.

Output, per row: dq_status (PASS / WARN / FAIL), dq_issues (list of {rule, code, severity,
message}), can_contact_email, can_contact_phone, normalized_email, normalized_phone,
contact_key, row_key, is_excluded (Ready=No / settled / not yet due / credit note), and a reason
code. FAIL = at least one BLOCK. A FAIL row goes to Human Queue A with reason DQ and is not
contacted. Also return an aggregate for the UI: counts by rule/code, by severity, by Country.

Expected result on the real 35 rows (assert these in tests/test_quality.py):
- FAIL (BLOCK): second INV-2026-4471 (DQ-02), CN-2026-0118 (DQ-03), INV-2026-4144 (DQ-04).
- can_contact_email = False but phone OK: INV-2026-4301, INV-2026-4276, INV-2026-4066.
- Phone normalized, not a failure: INV-2026-4189.
- WARN READY_FLAG_BLANK: INV-2026-4362. WARN AMOUNT_BELOW_MINIMUM: INV-2026-4207.
- Excluded without being a DQ failure: INV-2026-3980 and INV-2026-4415 (Ready=No),
  INV-2026-4265 (paid).
- Every row appears exactly once in the results, and the PASS + WARN + FAIL counts equal 35.
Also write unit tests per rule with small synthetic frames, and a test that an unknown
Outcome value is reported rather than crashing.
Handoff Summary at the end.
```

## Prompt 5: Business-rules engine
```
Task: core/rules.py, tests/test_rules.py. Pure functions, no I/O.
Implement BR-01.. from the design:
decide_action(row, history, today) -> Action(skip|email|phone|escalate_human, reason).
apply_outcome(row, outcome, promised_days, today) -> updated row fields (attempt counters
per channel, last_contact_date, next_followup_date, status, human_flag, approvals count).
Cover: channel switch at 5, both channels exhausted, one-contact-per-day, next_followup
not reached yet -> skip, >5-day promise -> pending approval, 3-approval cap, paid/closed,
channel errors counting as attempts, and hard bounce disabling email.
Provide a table-driven test with at least 25 scenarios.
Handoff Summary at the end.
```

## Prompt 6: Run orchestrator (idempotent)
```
Task: core/runner.py, tests/test_runner.py.
run_daily(sheet, today, run_id, rng) -> RunResult:
1. Load rows; run DQ; write DQ results.
2. For each row (row by row): skip if last_processed_date == today; else call
   rules.decide_action, generate mock response, apply outcome, write the row update and an
   Audit_Log entry. A failure in one row is logged to Run_Log and must not stop the run.
3. Write the Run_Log summary. Running it twice on the same day must change nothing the second time.
4. Batch the writes to stay within Google API quotas.
Test with the fake sheet: idempotency, a mid-run exception, and the counts reconciliation.
Handoff Summary at the end.
```

## Prompt 6 addition: sheet edits before every run
Add to Prompt 6: step 0 of run_daily is `reconcile_sheet_edits(states, run_date)` from core/rules.py. Write its
`updates` with SheetStore.update_rows, append its `audits` to Audit_Log (actor "sheet edit"), and put its
`issues` in the Run_Log errors field. Build every contact's updates with `build_event_updates`, never by hand.

## Prompt 7: Streamlit app shell + Run and Human Queue pages
```
Task: app.py, pages/1_Run.py, pages/2_Human_Queue.py.
- Run page: button "Run today's follow-ups", progress bar, results summary, DQ summary
  table, and a note on rows skipped because they were already processed today.
- Human Queue page: two clearly separated sections with distinct headers/colors:
  (A) Data-quality failures (row, issues), (B) Needing human follow-up (reason, channel
  history, last response). Include a pending-extension-approvals section with
  Approve/Reject buttons that write back to the sheet (max 3 approvals per contact).
- Pending approvals are listed per CONTACT (not per row): requested date, "n of 3 approvals used", the invoices
  waiting, and the last reply from Audit_Log. Approve / Reject call `approve_contact_extension` /
  `reject_contact_extension` (core/rules.py) on a FRESH read of the contact's rows, refuse while a run is RUNNING,
  write `decision.updates` with update_rows, and append an Audit_Log entry with the signed-in user (st.user email,
  else a typed name). Show a clear message if the decision was already made (someone else, or in the sheet).
- Humans may also decide by editing `approval_status` in the Google Sheet (see BR-25). Add a "Sync sheet edits"
  button and call `reconcile_sheet_edits` whenever the page loads fresh data; show its `issues` (CONFLICT,
  CAP_REACHED, MISSING_DATE, IGNORED_APPROVAL, STALE_PENDING) in a warnings box, and include a short "How to decide in
  the sheet" help text.
- Cache the sheet connection with st.cache_resource; data with a short TTL.
Handoff Summary at the end.
```

## Prompt 8: Plotly dashboard
```
Task: pages/3_Dashboard.py and core/metrics.py (+ tests).
- KPIs: total attempts, responses received, success rate, failure rate (no contact or
  system error), human-flagged.
- Row-status counts: DQ failed, Needing human follow-up, In progress, Handled. Assert and
  display that they sum to total rows (show a warning if not).
- Filters: Country (the contact location); text search by Customer, Contact Name, Email or
  Invoice Number. Show outstanding amounts grouped by Currency, never summed across currencies.
- Charts (Plotly): attempts per day, outcome distribution by category, channel
  comparison, success by location.
- Drill-down: select a contact -> table of every follow-up date, channel, outcome,
  response text; plus simple themes extracted from the response text (keyword frequency,
  no external API).
Handoff Summary at the end.
```

## Prompt 9: About page
```
Task: pages/4_About.py rendering content from docs/DESIGN.md.
Sections: purpose, architecture diagram, data model (sheets/columns), outcome taxonomy
table, an explicit "Business Rules Implemented" section (BR-01..., read from a single
source of truth in core/rules_catalog.py so the page cannot drift from the code), assumptions,
and limitations. Verify each listed rule maps to a test name.
Handoff Summary at the end.
```

## Prompt 10: Hardening and review
```
Task: review the whole repo against docs/DESIGN.md. List any mismatch between rules and
code, API quota risks, secrets exposure, and error handling gaps; then fix them. Run
pytest and report results. Add a README with local run steps.
```

## Prompt 11: Deployment instructions
```
Task: docs/DEPLOY.md. Step-by-step for a non-expert: create the Google Cloud service
account and enable the Sheets and Drive APIs, share the sheet with the service-account
email as Editor, push to GitHub, create the app on Streamlit Community Cloud, paste
secrets in TOML format (provide the exact template), pin Python version, first-run
checklist, a test script that exercises each outcome, and troubleshooting (429s, auth
errors, app sleeping).
```

## Prompt 12: PowerPoint deck
```
Task: build a simple 8-10 slide .pptx (python-pptx): title; problem and goal; architecture
diagram (Streamlit <-> Google Sheet, runner, rules engine, mock responder); data model;
daily run flow; outcome taxonomy; business rules; dashboard; deployment; next steps.
One idea per slide, minimal text.
```

## Notes on your requirements
- **"More than 5 attempts" vs "cannot exceed 5":** the prompts use a hard cap of 5 per channel. Say so if you want it different.
- **Google Sheets quotas:** about 60 writes per minute per user, so Prompt 6 requires batched writes.
- **Mock responses are random:** Prompt 3 includes a seed so tests are repeatable.
- **Duplicate invoice:** INV-2026-4471 is in the sheet twice; the DQ prompt flags both rows.
- **Multiple invoices per customer:** the one-contact-per-day rule applies per contact, not per invoice (decided in Prompt 1).
- **Currencies:** EUR, CZK, HUF and RON are never summed together.
- **Community Cloud:** it has no persistent disk, which fits your sheet-only design.
