# Invoice Follow-Up Assistant

Follows up with contacts on overdue invoices (email first, then phone), keeps a full audit trail, and hands anything it
cannot or should not decide to a person. Built with Streamlit; **one Google Sheet is the only data store**. Replies are
simulated: no real email is sent and no real call is made.

* **Run**: run the day's follow-ups. Running twice on one date changes nothing.
* **Human Queue**: payment dates waiting for approval, rows needing human follow-up, rows that failed data quality.
* **Dashboard**: attempts, response / success / failure rates, filters by country and contact, drill-down to one contact.
* **About**: the design, the data model, every business rule and the tests that prove it.

The full specification is in [`docs/DESIGN.md`](docs/DESIGN.md); what was found and fixed in the final review is in
[`docs/REVIEW.md`](docs/REVIEW.md); **how to put it online is in [`docs/DEPLOY.md`](docs/DEPLOY.md)**.

## Run it on your computer

You need Python 3.11 or newer and a Google Sheet shaped like the case-study file (18 columns, header in row 1).

**1. Install**

```powershell
# Windows PowerShell                         # macOS / Linux
python -m venv .venv                         python3 -m venv .venv
.venv\Scripts\Activate.ps1                   source .venv/bin/activate
pip install -r requirements.txt
```

**2. Let the app reach your sheet** (once)

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project and turn on the **Google Sheets API**.
2. Create a **service account** and add a **JSON key** for it. Keep the file private.
3. Open your Google Sheet and **share** it with the service account's `client_email` as **Editor**.
   The sheet must be a real Google Sheet (not an uploaded .xlsx: use *File > Save as Google Sheets* first).
4. Build the secrets file from the key you downloaded (it never prints the private key):

   ```powershell
   python scripts/make_secrets.py "C:\path\to\key.json" --sheet-id "<the sheet's address>" --generate-password
   python scripts/check_connection.py     # read-only: tells you what to fix if it cannot connect
   ```

   (Or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in by hand.)

**3. Start the app**

```powershell
streamlit run app.py
```

Open the address it prints (normally http://localhost:8501). The first time, the app adds the columns and tabs it needs
(`Audit_Log`, `Run_Log`, `Config`) and never changes your 18 columns or the `Notes` column.

**4. Use it**

1. Open **Run** and press *Run follow-ups*. Type your name in the sidebar first: it is recorded with every decision.
2. Open **Human Queue** to approve or reject payment dates, resolve rows, and see rows that failed data quality.
3. Open **Dashboard** to see how it is going.

You can also decide a payment date by typing `APPROVED` or `REJECTED` in the `approval_status` cell of the Google Sheet and
pressing *Sync sheet edits* (the next run does it too). The *About* page explains how.

Settings (attempt limit, days, minimum amounts, time zone) are rows in the `Config` tab. Change a value there and the next
run uses it.

## Run the tests

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a fake in-memory sheet, so they need no Google account and change nothing. They include the real 35-row file
(`tests/fixtures/overdue_invoices.csv`).

## What is in the folder

| Path | What it is |
|---|---|
| `app.py`, `pages/`, `app_common.py` | The Streamlit app: landing page, the four pages, shared helpers |
| `core/` | All the logic, with no Streamlit in it: `sheets` (the only code that talks to Google), `quality`, `rules`, `runner`, `actions`, `queue`, `metrics`, `charts`, `taxonomy`, `mock_responses`, `rules_catalog` (the rules text the About page shows) |
| `tests/` | Automated tests, including drift checks that fail if the About page and the code disagree |
| `docs/` | `DESIGN.md` (the specification), `REVIEW.md` (the final review), `DEPLOY.md` (putting it online) |
| `scripts/` | `make_secrets.py` (builds your secrets file), `check_connection.py` (read-only check), `exercise_outcomes.py` (runs every outcome on a scratch sheet) |
| `.streamlit/` | `config.toml` (safe defaults) and `secrets.toml.example` (the template) |

## Keep it safe

* **Never commit** `.streamlit/secrets.toml` or the service-account JSON key. `.gitignore` already excludes them.
* **Set `app_password`.** Without it, anyone who has the app's web address can run follow-ups and approve payment dates.
* **Keep the repository private.** `tests/fixtures/overdue_invoices.csv` holds the case-study customers' names, emails and
  phone numbers. Replace it with anonymised data before making the repository public.
* Share the Google Sheet only with the service account and the people who need it.

## If something goes wrong

| You see | Probably | Do this |
|---|---|---|
| "Could not connect to the Google Sheet" | Secrets missing or the sheet is not shared with the service account | Check `secrets.toml` and the sheet's sharing |
| "This operation is not supported for this document" | The file is an uploaded Excel file, not a Google Sheet | *File > Save as Google Sheets*, use that id |
| "Could not read the Google Sheet" | A tab was deleted or renamed, or Google is busy | Check the tabs, wait a minute, press *Refresh* |
| Every row is in *Data quality failed* with "not a yyyy-mm-dd date" | The sheet shows dates in another format | Format the date columns as yyyy-mm-dd |
| The Run button is disabled | The run date is earlier than rows already processed | Pick that date or a later one |
| "Another run is in progress" | Someone is running, or a run crashed in the last 30 minutes | Wait; a crashed run clears itself after 30 minutes |
| Google "429 quota exceeded" | Too many requests in a minute | The app waits and retries; running again continues where it stopped |
