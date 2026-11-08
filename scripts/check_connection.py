"""Check, read-only, that the app can reach your Google Sheet. Nothing is written or changed.

    python scripts/check_connection.py

It reads .streamlit/secrets.toml, opens the sheet, and reports the tabs, the invoice columns found and how many rows there
are. If something is wrong it says what to do about it.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.redact import safe_error  # noqa: E402
from core.schema import INVOICES_EXISTING_COLUMNS, INVOICES_NEW_COLUMNS  # noqa: E402

SECRETS = ROOT / ".streamlit" / "secrets.toml"


def explain(exc: Exception, email: str) -> str:
    """Turn a Google error into what to do next."""
    text = str(exc)
    low = text.lower()
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "code", None)
    if status == 403 and ("has not been used" in low or "disabled" in low or "service_disabled" in low):
        return "The Google Sheets API is not turned on for this project. Turn it on (APIs & Services > Library > Google Sheets API > Enable), wait a minute, try again."
    if status == 403 or "permission" in low:
        return f"The sheet is not shared with the service account. Open the sheet, press Share, add {email} as Editor, try again."
    if status == 404 or "not found" in low:
        return "Google cannot find that sheet. Check sheet_id in secrets.toml (the long id between /d/ and /edit in the address)."
    if "not supported for this document" in low or "failedprecondition" in low.replace(" ", ""):
        return "That file is an uploaded Excel file, not a Google Sheet. In Google Sheets use File > Save as Google Sheets, and use the new file's id."
    if status == 429:
        return "Google says too many requests. Wait a minute and try again."
    if "invalid_grant" in low or "jwt" in low or "private key" in low:
        return "The key was not accepted. Recreate it with scripts/make_secrets.py from a freshly downloaded JSON key (also check your computer's clock)."
    return "Unexpected error. Details below (secrets removed)."


def main() -> int:
    if not SECRETS.exists():
        print("No .streamlit/secrets.toml yet. Create it with:\n  python scripts/make_secrets.py <key.json> --sheet-id <sheet address>")
        return 2
    cfg = tomllib.loads(SECRETS.read_text(encoding="utf-8"))
    account, sheet_id = cfg.get("gcp_service_account"), cfg.get("sheet_id")
    if not account or not sheet_id or "PASTE" in str(sheet_id):
        print("secrets.toml is missing sheet_id or [gcp_service_account]. Re-create it with scripts/make_secrets.py.")
        return 2
    email = account.get("client_email", "(unknown)")
    try:
        import gspread
    except ImportError:
        print("gspread is not installed. Run: pip install -r requirements.txt")
        return 2

    print(f"Service account : {email}")
    print(f"Sheet id        : {sheet_id}")
    try:
        client = gspread.service_account_from_dict(dict(account), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        book = client.open_by_key(sheet_id)
        tabs = book.worksheets()
        print(f"\nConnected. The sheet is called: {book.title!r}")
        print("Tabs            : " + ", ".join(f"{t.title} ({t.row_count} rows x {t.col_count} cols)" for t in tabs))
        first = cfg.get("invoices_tab")
        ws = next((t for t in tabs if t.title == first), None) if first else tabs[0]
        if ws is None:
            print(f"\nProblem: invoices_tab {first!r} is not one of the tabs above.")
            return 1
        values = ws.get_all_values()
    except Exception as exc:  # noqa: BLE001 - every failure gets an explanation
        print(f"\nFAILED: {explain(exc, email)}\n  ({safe_error(exc, [account.get('private_key', ''), sheet_id])})")
        return 1

    header = [c.strip() for c in (values[0] if values else [])]
    data_rows = [r for r in values[1:] if any(c.strip() for c in r)]
    print(f"\nInvoice tab     : {ws.title!r}, {len(data_rows)} data rows")
    have = [c for c in INVOICES_EXISTING_COLUMNS if c in header]
    missing = [c for c in INVOICES_EXISTING_COLUMNS if c not in header]
    print(f"Your columns    : {len(have)} of {len(INVOICES_EXISTING_COLUMNS)} found")
    if missing:
        print(f"  Missing: {', '.join(missing)}\n  The app needs these exactly as named in row 1.")
    added = [c for c in INVOICES_NEW_COLUMNS if c in header]
    print(f"App columns     : {len(added)} of {len(INVOICES_NEW_COLUMNS)} already present"
          + ("  (the first run adds the rest)" if len(added) < len(INVOICES_NEW_COLUMNS) else ""))
    print("\nOK: the app can read this sheet. Nothing was changed." if not missing else "\nConnected, but fix the missing columns first.")
    print("The FIRST time you open the app it adds columns and the tabs Audit_Log, Run_Log and Config to this sheet.")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
