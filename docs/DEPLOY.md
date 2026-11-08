# Deploying the Invoice Follow-Up Assistant to Streamlit Community Cloud

Written for someone who has never deployed an app. Allow about 45 minutes. Everything here is free.

**What you will end up with:** the app running at a web address like `https://your-name-invoice-followup.streamlit.app`,
reading and writing one Google Sheet, protected by a password.

> **Streamlit changes its screens now and then.** The names of buttons below were checked against Streamlit's
> documentation on 8 November 2026. If a screen looks different, look for the same words; the steps stay the same.

## What you need

| You need | Why |
|---|---|
| A Google account | Owns the sheet and the service account |
| A GitHub account | Streamlit deploys from a GitHub repository |
| This project folder, on your computer | It is what gets deployed |
| Git and (optional, easier) the GitHub CLI `gh` | To put the folder on GitHub |
| Python 3.11+ and this project's packages | For the local checks in Part 6 |

**Three decisions to make first**

1. **Which sheet?** Do the first deployment against a **scratch copy** of your sheet (File > Make a copy). The app writes
   simulated follow-ups into whatever sheet it is pointed at. When everything works you switch it to the real sheet
   (Part 7).
2. **Keep the GitHub repository private.** The tests contain the case-study customers' names, emails and phone numbers
   (`tests/fixtures/overdue_invoices.csv`). On Community Cloud, an app from a private repository is private by default,
   which is also what you want. (Streamlit allows **one private app at a time** per account. If you already have a private
   app, you must make it public or delete it first.)
3. **Set an app password.** Even on a private app, a password is a second lock. Without one, anyone who can open the
   address can run follow-ups and approve payment dates.

---

## Part 1. The Google service account

The app signs in to Google as a **service account**: a robot user with its own email address.

> **Already set this up for local testing? Then Part 1 is done: skip to Part 2 (or Part 3).** Use the *same* project, the
> *same* service account and the *same* key file. Nothing in Google needs to change for the cloud. The secrets file you
> made for local use (`.streamlit/secrets.toml`) already contains the key, and Part 5 tells you to paste its contents into
> Streamlit. The only reason to make a second key is if you want to be able to revoke the cloud's access without affecting
> your local testing: in the Google console, open the service account > **Keys > Add key > JSON**, then build a second
> secrets file from it with `make_secrets.py --output`. That is optional.

**If you have not made one yet** (Google Cloud console, <https://console.cloud.google.com/>):

1. Create a project (for example "Invoice Follow-Up") and select it.
2. *APIs & Services > Library*: search **Google Sheets API** and press **Enable**.
3. *IAM & Admin > Service Accounts > Create service account*. Give it a name. Skip the optional role screens.
4. Open the new service account, go to **Keys > Add key > Create new key > JSON**. A file downloads. **That file is a
   password.** Keep it somewhere private, never inside the project folder, never in an email or chat.
5. Note the service account's email address (it ends in `.iam.gserviceaccount.com`).

If you use the Google Cloud command line instead, the same thing is:

```
gcloud projects create invoice-followup-1234 --name="Invoice Follow-Up"
gcloud services enable sheets.googleapis.com --project=invoice-followup-1234
gcloud iam service-accounts create invoice-followup --project=invoice-followup-1234
gcloud iam service-accounts keys create "%USERPROFILE%\.invoice-followup\key.json" ^
    --iam-account=invoice-followup@invoice-followup-1234.iam.gserviceaccount.com
```

## Part 2. Share the sheet with the service account

1. Open the sheet (the scratch copy for now). It must be a real Google Sheet, not an uploaded Excel file
   (*File > Save as Google Sheets* if needed).
2. Press **Share**, paste the service account's email address, choose **Editor**, untick *Notify people*, press **Share**.
3. Copy the sheet's id: the long text between `/d/` and `/edit` in its address.

## Part 3. Make the secrets file and check the connection on your computer

Do this once, locally, before anything goes online. It proves the Google side works, and it writes the exact text you will
paste into Streamlit later.

```
python scripts/make_secrets.py "C:\path\to\key.json" --sheet-id "<the sheet's address>" --generate-password
python scripts/check_connection.py
```

`make_secrets.py` writes `.streamlit/secrets.toml` and shows the generated password **once** (it is also in the file).
`check_connection.py` only reads: it prints the sheet's name, tabs and how many invoice columns it found, or tells you what
to fix. You want to see `OK: the app can read this sheet`.

Then try the app itself: `streamlit run app.py`, open <http://localhost:8501>, enter the password and open **Run**.

## Part 4. Put the code on GitHub (private)

Run these in a terminal in the project folder. If you have the GitHub CLI and are signed in (`gh auth login`):

```
git init -b main
git add .
git status
```

**Stop and read the `git status` list.** It must **not** contain any of these:

| Must not appear | Why |
|---|---|
| `.streamlit/secrets.toml` | Holds the Google key and the app password |
| any `*.json` key file | The service account key |
| `Case Study Data ... .csv` | The original customer data |

(They are already excluded by `.gitignore`, so they should not be there. If one is, stop and tell whoever is helping you.)
A quick automatic check, which should print nothing:

```
git ls-files | findstr /i /r /c:"secrets\.toml$" /c:"key\.json" /c:"service-account" /c:"Case Study"
```

(`.streamlit/secrets.toml.example` is meant to be there: it holds only placeholders.) The one file that *does* contain
customer details and *will* be committed is `tests/fixtures/overdue_invoices.csv`. That is why the repository must be
private, or why you replace it with made-up data before ever making the repository public.

Then:

```
git commit -m "Invoice follow-up assistant"
gh repo create invoice-followup-assistant --private --source . --push
```

Without the GitHub CLI: create an empty **private** repository on github.com (no README, no .gitignore), then run
`git remote add origin <its address>` and `git push -u origin main`.

## Part 5. Create the app on Community Cloud

1. Go to <https://share.streamlit.io> and **Continue with GitHub**. Streamlit creates your account from the email on
   your GitHub account.
2. **Let Streamlit see your private repository.** Click your GitHub username (upper left) > **Settings > Linked accounts**
   and, under *Source control*, **Connect here**, then authorize. Without this, a private repository does not appear.
3. Click **Create app** (upper right). When asked "Do you already have an app?", choose **Yup, I have an app**.
4. Fill in:
   * **Repository:** `<your-github-name>/invoice-followup-assistant`
   * **Branch:** `main`
   * **Main file path:** `app.py`
   * **App URL** (optional): pick a name you like.
5. Click **Advanced settings**:
   * **Python version:** choose **3.13**. It is the version this app was tested on in a clean environment, along with 3.14.
     The default shown is 3.12, which was *not* tested. If 3.13 is not offered, pick the newest listed.
     Streamlit's documentation does not describe changing the version after deployment; plan to delete the app and
     deploy it again if you ever need to.
   * **Secrets:** paste the **entire contents** of your local `.streamlit/secrets.toml`.

     **For the setup already made on this computer** (nothing to create, just copy and paste):

     | What | Where it is |
     |---|---|
     | Google Cloud project | `invoice-followup-4456` |
     | Service account | `invoice-followup@invoice-followup-4456.iam.gserviceaccount.com` |
     | Key file (never leaves this computer) | `C:\Users\rramani\.invoice-followup\key.json` |
     | **The secrets text to paste** | `C:\Users\rramani\rohlik-assignment\.streamlit\secrets.toml` (already contains the key, the app password and the test copy's sheet id) |

     To put that text on the clipboard **without displaying it**, run this in PowerShell, then click into the Secrets box
     and press **Ctrl+V**:

     ```
     Get-Content "C:\Users\rramani\rohlik-assignment\.streamlit\secrets.toml" -Raw | Set-Clipboard
     ```

     Afterwards, clear the clipboard (copy any ordinary word) and do not paste it anywhere else. The key and the password
     are deliberately **not written in this guide**, because this file is stored in GitHub. Do not add them to it.
     Do not run `make_secrets.py` again for this: the file already exists and already points at the test copy.

     For anyone else (or a rebuilt file), the secrets text looks like this, with your own values:

     ```toml
     sheet_id = "1AbC...the-long-id"
     app_password = "the-generated-password"

     [gcp_service_account]
     type = "service_account"
     project_id = "invoice-followup-1234"
     private_key_id = "..."
     private_key = "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n"
     client_email = "invoice-followup@invoice-followup-1234.iam.gserviceaccount.com"
     client_id = "..."
     auth_uri = "https://accounts.google.com/o/oauth2/auth"
     token_uri = "https://oauth2.googleapis.com/token"
     auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
     client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/..."
     universe_domain = "googleapis.com"
     ```

     The private key must stay on **one line** with `\n` where the line breaks are, exactly as `make_secrets.py` writes it.
     Click **Save**.
6. Click **Deploy**. The first start takes a few minutes while Streamlit installs the packages from `requirements.txt`.

**Who can open it.** An app from a private repository starts as private: only people you invite can view it. In the app's
**Settings > Sharing** (or the **Share** button), "Only specific people can view this app" is the private setting; add the
email addresses of the colleagues who should have access. Viewers sign in with Google (or a one-time emailed link).
Everyone then also needs the app password.

## Part 6. First-run checklist (on the scratch copy)

Work through this in order. Tick each box before the next.

- [ ] The address opens and asks for the **password**. A wrong password is refused; the right one opens the app.
- [ ] The **home page** shows *Total rows 35* (for the case-study file) and *No follow-ups have been run yet*.
- [ ] Open the Google Sheet. Next to your original tab there are now **Audit_Log**, **Run_Log** and **Config** tabs, and
      25 new columns on the invoice tab. Your original 18 columns and **Notes** are unchanged.
- [ ] **About** opens, lists 27 business rules, and shows the settings from your Config tab.
- [ ] Type your name in the sidebar. Open **Run**, leave the date as it is, press **Run follow-ups**. It finishes and shows
      *Every row is accounted for*. For the case-study file: **3** rows *Data quality failed*, the paid row *Handled*, at
      least **6** rows *Needing human follow-up*.
- [ ] Press **Run follow-ups** again. Everything says *skipped, done today* and **0** emails and calls. (A second run on one
      date changes nothing.)
- [ ] **Human Queue** shows the three failed rows under *A. Data quality failures*, with reasons in words, and the rows with a
      person under *B*.
- [ ] If a payment date is waiting under *Decisions waiting*, press **Approve**. Check **Audit_Log**: your name is in the
      `actor` column.
- [ ] In the sheet, type `APPROVED` in the `approval_status` cell of another waiting row, then press **Sync sheet edits** in
      the Human Queue. It is picked up and logged as *sheet edit*.
- [ ] **Dashboard** shows attempts, rates and charts. The four status groups add up to the total (it says *adds up*).
- [ ] Set the *Run date* on the Run page to the next day and run again. Contacts get at most one attempt per date.

If any box fails, go to *Troubleshooting* below. The cause is almost always the sharing, the secrets text, or the sheet id.

### Optional: exercise every outcome (on a fresh scratch copy)

This runs on your computer, not on Streamlit (the cloud has no command line), against whichever sheet is in your local
`.streamlit/secrets.toml`. It simulates up to 30 days so that every outcome happens at least once (bounces, busy lines,
disputes, promises, payments, system errors, an attempt limit being reached) and then checks the rules.

```
python scripts/make_secrets.py "C:\path\to\key.json" --sheet-id "<a FRESH copy's address>" --force
python scripts/exercise_outcomes.py --yes
```

It takes about 10 minutes (it pauses between days to stay under Google's request limits) and ends with a checklist.
You want **`RESULT: PASS`**. `NEEDS_HUMAN_FOLLOW_UP` shows `n/a`: it is a safety net that only appears when a person
removes a contact's channels, and it is covered by the automated tests. **Throw that copy away afterwards:** its dates are
moved forward by 30 days, so the app would refuse to run it for an earlier date. The script refuses to run on a sheet that
already has an audit trail.

## Part 7. Going live on the real sheet

1. Share the **real** sheet with the service account (Part 2), as Editor.
2. In Streamlit, open the app's **Settings > Secrets**, change `sheet_id` to the real sheet's id, and **Save**. The app
   restarts.
3. Run the checklist in Part 6 again, up to and including the first run. On the real sheet this contacts nobody (replies are
   simulated), but it writes the results into your data, so make sure you want that.
4. Keep the scratch copy until you are comfortable. Delete it when you are.

## Part 8. Day to day

| I want to... | Do this |
|---|---|
| Change the app | Commit and `git push` to `main`. Streamlit shows the change in the running app within moments. |
| Change a setting (attempt limit, days, minimum amounts, time zone) | Edit the value in the **Config** tab of the sheet. No redeploy. |
| Add or remove a viewer | **Settings > Sharing** |
| Change the password or the sheet | **Settings > Secrets**, edit, **Save** |
| Look for a problem | **Manage app** (lower right of the running app) shows the logs; the overflow menu has **Download log** |
| Restart a stuck app | **Manage app > Reboot**, or the app menu in your Streamlit workspace |
| Wake a sleeping app | An app with no visitors for **12 hours** sleeps and shows *"This app has gone to sleep due to inactivity"*. Click **Yes, get this app back up!** Nothing is lost: everything is in the sheet. |
| Replace the Google key | Make a new key (Part 1), `make_secrets.py --force`, paste the new secrets into Streamlit, then delete the old key in the Google console |
| Remove everything | Delete the app in Streamlit, delete the repository, delete the Google Cloud project (that also kills the key) |

**Remember:** the app only does work when someone opens it and presses **Run**. Community Cloud has no scheduler. If you
need the run to happen every morning by itself, that needs a separate scheduler and is a bigger change.

Streamlit gives each app roughly 2.7 GB of memory and 2 CPU cores (as of its February 2024 notes; they may change). This app
uses a small fraction of that.

---

## Troubleshooting

| What you see | Most likely cause | What to do |
|---|---|---|
| *Could not connect to the Google Sheet* | Secrets missing or badly pasted | Settings > Secrets: compare with your local `secrets.toml`; the `private_key` must be one line with `\n` |
| *The caller does not have permission* / 403 | The sheet is not shared with the service account | Share it with the `client_email` as **Editor** |
| 403 *Google Sheets API has not been used in project... or it is disabled* | The API is not enabled | Google Cloud > APIs & Services > Library > Google Sheets API > Enable; wait a minute |
| 404 *Requested entity was not found* | Wrong `sheet_id` | Use the id between `/d/` and `/edit` |
| *This operation is not supported for this document* | The file is an uploaded Excel file | *File > Save as Google Sheets*, use the new file's id |
| `invalid_grant` or *Invalid JWT* | The key was deleted or damaged | Make a new key (Part 1) and re-paste the secrets |
| 429 *Quota exceeded* | Too many Google requests in a minute (about 60 reads and 60 writes allowed) | The app waits and retries by itself. If a run stops, press Run again: it continues where it stopped |
| The password box does not appear | `app_password` is missing from the Secrets | Add `app_password = "..."` and Save |
| The app is stuck on *Your app is in the oven* | A package failed to install | **Manage app** logs show which; make sure `requirements.txt` was pushed |
| *ModuleNotFoundError* in the logs | `requirements.txt` not at the top of the repository | It must sit next to `app.py` |
| Every row is in *Data quality failed* with "not a yyyy-mm-dd date" | The sheet shows dates in another format | Format the date columns as `yyyy-mm-dd` |
| The Run button is disabled | The date is earlier than rows already processed | Pick that date or a later one |
| *Another run is in progress* | Someone is running, or a run crashed in the last 30 minutes | Wait; a crashed run clears itself after 30 minutes |
| *Could not read the Google Sheet* | A tab was deleted or renamed, or Google is busy | Check the tabs exist (Audit_Log, Run_Log, Config), wait a minute, press **Refresh** |
| The time or date looks wrong | The time zone | Set `timezone` in the Config tab (default `Europe/Berlin`) |
| Dashboard or pages feel slow | A very large Audit_Log | Move old rows to another file |

## Security checklist before you share the address

- [ ] The repository is **private**.
- [ ] `git ls-files` shows no secrets file, no key file, and no `Case Study` file (Part 4).
- [ ] The key file is not in the project folder, and not in an email or chat.
- [ ] `app_password` is set, and only the people who need it have it.
- [ ] The sheet is shared with the service account and the people who need it, and nobody else.
- [ ] The app's Sharing is set to *Only specific people*, if your account allows it.
- [ ] You know where the key can be revoked (Google Cloud > IAM & Admin > Service Accounts > Keys).
