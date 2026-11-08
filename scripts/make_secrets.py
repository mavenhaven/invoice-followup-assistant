"""Build .streamlit/secrets.toml from the service-account key file you downloaded from Google.

    python scripts/make_secrets.py "C:/path/to/key.json" --sheet-id "https://docs.google.com/spreadsheets/d/.../edit" --generate-password

Why a script: the private key must keep its line breaks exactly, and copying it by hand into a TOML file is the most
common way to get it wrong. This reads the key file, writes the secrets file, and never prints the private key.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets as pysecrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".streamlit" / "secrets.toml"
KEY_FIELDS = ("type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri",
              "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain")


def extract_sheet_id(text: str) -> str:
    """Accept a whole Google Sheets address or just the id."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_\-]{20,})", text)
    candidate = m.group(1) if m else text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", candidate):
        raise ValueError("That does not look like a Google Sheet address or id. It should contain /spreadsheets/d/<long id>/")
    return candidate


def toml_string(value: str) -> str:
    """A TOML basic string. JSON escaping is valid TOML, and keeps the private key's line breaks as \\n."""
    return json.dumps(value, ensure_ascii=False)


def load_key(path: Path) -> dict[str, str]:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Cannot find {path}") from None
    except json.JSONDecodeError:
        raise ValueError(f"{path} is not a JSON file. Use the key file downloaded from Google Cloud (Keys > Add key > JSON).") from None
    if info.get("type") != "service_account":
        raise ValueError("This JSON is not a service-account key (its type is not 'service_account'). "
                         "Download a key from IAM & Admin > Service Accounts > Keys, not an OAuth client file.")
    for field in ("private_key", "client_email"):
        if not info.get(field):
            raise ValueError(f"The key file has no {field}.")
    if "BEGIN PRIVATE KEY" not in info["private_key"]:
        raise ValueError("The private_key in that file does not look like a private key.")
    return info


def build_secrets(info: dict[str, str], sheet_id: str, password: str = "", invoices_tab: str = "") -> str:
    lines = ["# Written by scripts/make_secrets.py. Never commit this file (it is git-ignored).", "",
             f"sheet_id = {toml_string(sheet_id)}"]
    if invoices_tab:
        lines.append(f"invoices_tab = {toml_string(invoices_tab)}")
    if password:
        lines.append(f"app_password = {toml_string(password)}")
    lines += ["", "[gcp_service_account]"]
    for field in KEY_FIELDS:
        if info.get(field):
            lines.append(f"{field} = {toml_string(str(info[field]))}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("key_file", type=Path, help="the service-account JSON key downloaded from Google Cloud")
    p.add_argument("--sheet-id", required=True, help="the Google Sheet address, or just its id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--password", help="app password to require when opening the app")
    g.add_argument("--generate-password", action="store_true", help="make a random app password and show it once")
    p.add_argument("--invoices-tab", default="", help="name of the tab with the invoices (default: the first tab)")
    p.add_argument("--force", action="store_true", help="replace an existing secrets.toml")
    p.add_argument("--output", type=Path, default=TARGET, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    try:
        info = load_key(args.key_file)
        sheet_id = extract_sheet_id(args.sheet_id)
    except ValueError as exc:
        print(f"Problem: {exc}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print(f"{args.output} already exists. Run again with --force to replace it.", file=sys.stderr)
        return 2

    password = args.password or (pysecrets.token_urlsafe(12) if args.generate_password else "")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_secrets(info, sheet_id, password, args.invoices_tab), encoding="utf-8")

    print(f"Wrote {args.output}")
    print(f"  sheet id        : {sheet_id}")
    print(f"  service account: {info['client_email']}")
    if args.generate_password:
        print(f"  app password    : {password}   <- shown once; it is saved in the file")
    elif not password:
        print("  app password    : none set (fine on your own computer; set one before putting the app on the internet)")
    print("\nNext:")
    print(f"  1. Share the Google Sheet with {info['client_email']} as EDITOR (Share button, top right).")
    print("  2. python scripts/check_connection.py     (read-only check)")
    print("  3. streamlit run app.py")
    try:
        if ROOT in args.key_file.resolve().parents:
            print(f"\nWarning: the key file {args.key_file} is inside the project folder. Move it somewhere private; "
                  "the file name must never be committed.")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
