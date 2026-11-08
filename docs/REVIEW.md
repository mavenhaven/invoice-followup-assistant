# Final review

The whole repository was reviewed against `docs/DESIGN.md` for four things: **rules versus code**, **Google API quota
risks**, **secrets and data exposure**, and **error handling**. Every finding below was fixed unless it says otherwise, and
each fix has a test (`tests/test_review_fixes.py`, `tests/test_sheets_safety.py`, and updates elsewhere).

**How it was checked.** The spec was read line by line against the code. The Google requests of each main action were counted
before and after. A linter checked for unused or undefined names. The project was copied to a clean folder, installed from
`requirements-dev.txt` alone, and its tests run there. The app was started for real. Hostile input (markdown in customer
names, a private key inside an error message, rows moved while a run was going) was fed in on purpose.

**Result:** 562 tests pass (518 before the review), lint is clean, and a fresh install passes the same tests.

## What was found

Severity: **High** = could lose or corrupt data, or expose it; **Medium** = wrong or fragile in realistic use; **Low** = polish.

### Data safety

| # | Sev | What was wrong | Fix |
|---|---|---|---|
| D1 | High | Writes found a row by the position remembered from an earlier read. If a person inserted, deleted or sorted rows in the sheet in between, a value could land on the wrong invoice. | Every update now looks the rows and columns up in the sheet itself, by `row_key` and column name, just before writing. A deleted row is reported, never written elsewhere. |
| D2 | Medium | Two rows sharing a `row_key` (a copied row) would have been updated ambiguously. | The update stops with a clear message naming the key. Other rows are unaffected. |
| D3 | High | Two people pressing Run in the same second could both pass the "is a run active?" check and both contact everyone. | A run logs itself, then checks it is the earliest active run for the date. The later one marks itself FAILED and touches no invoice. |
| D4 | High | The Run page allowed a date earlier than rows already processed, which contacts people out of order and breaks the one-contact-per-day rule across dates. | New rule BR-27. The Run page disables the button and explains; the runner also refuses and logs it. |
| D5 | Low | If the run failed and then the run-log update also failed, the log error hid the real one. | The real error is always the one raised. |

### Google API quota

Google allows about 60 reads and 60 writes a minute for the service account, shared by every user of the app.

| # | Sev | What was wrong | Fix |
|---|---|---|---|
| Q1 | Medium | The list of tabs was fetched again on every read: 3 of the 7 requests of a page load, 9 of the 22 of a run. | Fetched once, refreshed only when a name is not found. |
| Q2 | Medium | Every new browser session re-checked the sheet layout (8 reads). | At most once every 10 minutes for the whole app. |
| Q3 | Medium | A large run could burst past 60 write requests a minute and rely on retries. | Writes are spaced at least 1.1 seconds apart on the real connection (reads are never delayed). |
| Q4 | Info | The whole Audit_Log is read on every page load (cached 30 seconds). | Accepted: fine for thousands of rows. Listed on the About page as a limit. |

Measured on the 35-row file:

| Action | Reads before | Reads after |
|---|---|---|
| A page load | 7 | 4 |
| First run | 22 | 18 |
| Same-day rerun | 19 | 13 |
| One approval | 6 | 5 |
| Sheet layout check, per new visitor | 8 | 8 once per 10 minutes, shared |

### Secrets and data exposure

| # | Sev | What was wrong | Fix |
|---|---|---|---|
| S1 | High | On a public address, anyone with the link could run follow-ups, approve payment dates and read customer data. | Optional `app_password` in the secrets: nothing is shown or read from the sheet until it is entered. The README and the secrets template say to set it. |
| S2 | High | Error text (shown on screen, and written into Run_Log) could contain a private key, a token or the sheet's address. | `core/redact.py` removes keys, tokens, bearer headers, sheet ids and the exact secret values from every error before it is shown or stored. |
| S3 | Medium | Text from the sheet (customer names, summaries) was shown through markdown, so `[link](...)`, bold or `$` could change what a person sees. | All sheet text shown in markdown is escaped. |
| S4 | Medium | The case-study export (names, emails, phone numbers) sat in the project folder, and its copy in the tests, headed for GitHub. | The original is git-ignored; the README and `.gitignore` say to keep the repository private or anonymise the fixture first. |
| S5 | Low | A person's name was stored as typed (newlines, control characters, any length). | Cleaned to one line, 80 characters. Formula injection was also checked: every write uses Google's RAW mode, so `=...` is stored as text. |
| S6 | Low | Streamlit defaults show error details and collect usage statistics. | `.streamlit/config.toml` hides stack traces and turns statistics off. |
| S7 | Low | A stand-in identity from Streamlit would have been recorded as the person acting. | An email is trusted only when the user is really logged in (fixed earlier, tested). |

**Checked and clean:** no secret in the repository; the only secrets file is a template with placeholders; no `eval`, `exec`,
`print`, `subprocess` or unsafe HTML anywhere; the Google permission requested is only Sheets (not all of Drive).

### Error handling

| # | Sev | What was wrong | Fix |
|---|---|---|---|
| E1 | Medium | A deleted tab, or Google being busy, showed a stack trace on every page. | A plain message with the technical detail (secrets removed) behind an expander. |
| E2 | Medium | A failed write during an approval or resolution showed a stack trace. | "That did not complete... some of it may have been saved. Press Refresh." |
| E3 | Medium | "Today", and every timestamp in the logs, used the server's clock (UTC on Streamlit Community Cloud), not the team's. | Run date and timestamps follow the time zone in the Config sheet (default Europe/Berlin). |
| E4 | Low | A run did not record who pressed Run. | `started_by` column in Run_Log. |

### Spec versus code

The code was right and the spec was out of date in these places; `docs/DESIGN.md` now matches what was built:

* `can_contact_email/phone` only say whether a value is valid. A bounce is remembered in `email_disabled`/`phone_disabled`.
* No channel left gives the outcome `NEEDS_HUMAN_FOLLOW_UP`; a used-up cap gives `MAX_ATTEMPTS_REACHED` (A-11).
* Earlier attempts are mapped to email as well as phone, and "called back" is not a call (BR-19).
* The run lock, the "decide in the sheet" rule, the four-groups rule (BR-26) and the new BR-27 are written down.
* The status diagram gained "in progress to data quality failed". The chart list says what was actually built.
* Assumptions A-03, A-04, A-09 are marked confirmed and A-11 to A-14 were added.

### Hygiene

* Unused imports and one unused variable removed; one missing import (`dt` in `app_common.py`) fixed. Lint is clean.
* A stray `~$...pptx` Office lock file is now git-ignored. It belongs to a PowerPoint window you have open; it was left alone.

## Not fixed, and why

| Item | Why it stays |
|---|---|
| **Not run against your live Google Sheet.** | I had no credentials. Everything up to the network call is verified: the client builds from a service-account key, requests only Sheets access, and the gspread methods called exist with the arguments used. Real Google behaviour (number and date display formats, empty-cell trimming) is the one thing the fake sheet cannot prove. Run it once on a copy of your sheet first. |
| An audit entry can be lost if the app stops in the fraction of a second between writing the invoice rows and writing the audit rows. | The sheet is the source of truth and stays correct. Writing the audit first would instead create audit entries for changes that did not happen. |
| The app only runs when someone presses Run. | Streamlit Community Cloud has no scheduler. A scheduled run would need another service. |
| The whole Audit_Log is read on each load. | Fine for thousands of rows; archive old rows if it grows far beyond that. |
| Mock replies are English and templated. | By design: they exist to exercise every outcome. |

## Conclusion

No finding blocked the design. The three that mattered most were D1 (writes on the right row), D3 and D4 (runs that cannot overlap
or go back in time) and S1 and S2 (access and secrets), and each is now covered by a test that fails if the protection is removed.
