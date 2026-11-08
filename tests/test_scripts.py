"""The setup helpers: scripts/make_secrets.py and scripts/check_connection.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHEET_ID = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=drive_link"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make = load("make_secrets")
check = load("check_connection")


@pytest.fixture
def key_file(tmp_path):
    """A throwaway service-account key: a real RSA key made just for this test."""
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": "demo-project", "private_key_id": "abc123", "private_key": pem,
            "client_email": "followup@demo-project.iam.gserviceaccount.com", "client_id": "1234567890",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/followup%40demo-project.iam.gserviceaccount.com",
            "universe_domain": "googleapis.com"}
    path = tmp_path / "key.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path, info


# ---------------------------------------------------------------- make_secrets


def test_the_sheet_id_is_taken_from_a_whole_address_or_given_directly():
    assert make.extract_sheet_id(URL) == SHEET_ID
    assert make.extract_sheet_id(SHEET_ID) == SHEET_ID and make.extract_sheet_id(f"  {SHEET_ID}\n") == SHEET_ID
    assert make.extract_sheet_id(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/") == SHEET_ID
    for bad in ("", "hello", "https://example.com/page", "short-id"):
        with pytest.raises(ValueError, match="does not look like"):
            make.extract_sheet_id(bad)


def test_key_files_that_are_not_service_account_keys_are_explained(tmp_path):
    with pytest.raises(ValueError, match="Cannot find"):
        make.load_key(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON file"):
        make.load_key(bad)
    for content, message in [({"type": "authorized_user"}, "not a service-account key"),
                             ({"type": "service_account", "client_email": "x@y"}, "no private_key"),
                             ({"type": "service_account", "private_key": "abc", "client_email": "x@y"}, "does not look like a private key"),
                             ({"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----x"}, "no client_email")]:
        bad.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            make.load_key(bad)


def test_the_secrets_file_is_valid_toml_and_keeps_the_private_key_exactly(key_file):
    path, info = key_file
    text = make.build_secrets(make.load_key(path), SHEET_ID, password="s3cret \"quoted\" \\ pass", invoices_tab="Rechnungen ä")
    parsed = tomllib.loads(text)
    assert parsed["sheet_id"] == SHEET_ID and parsed["app_password"] == 's3cret "quoted" \\ pass' and parsed["invoices_tab"] == "Rechnungen ä"
    account = parsed["gcp_service_account"]
    assert account["private_key"] == info["private_key"] and account["private_key"].count("\n") == info["private_key"].count("\n")
    assert account["client_email"] == info["client_email"] and account["type"] == "service_account"
    assert set(account) == set(info)  # every field the key file had, nothing invented


def test_password_and_tab_are_optional():
    info = {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n", "client_email": "a@b"}
    parsed = tomllib.loads(make.build_secrets(info, SHEET_ID))
    assert "app_password" not in parsed and "invoices_tab" not in parsed


def test_main_writes_the_file_never_prints_the_key_and_tells_you_the_next_step(key_file, tmp_path, capsys):
    path, info = key_file
    out = tmp_path / "secrets.toml"
    assert make.main([str(path), "--sheet-id", URL, "--generate-password", "--output", str(out)]) == 0
    shown = capsys.readouterr().out
    parsed = tomllib.loads(out.read_text(encoding="utf-8"))
    assert parsed["sheet_id"] == SHEET_ID and len(parsed["app_password"]) >= 12 and parsed["app_password"] in shown
    assert "BEGIN PRIVATE KEY" not in shown and info["private_key"][40:80] not in shown
    assert info["client_email"] in shown and "EDITOR" in shown and "check_connection.py" in shown


def test_main_will_not_overwrite_without_force_and_reports_bad_input(key_file, tmp_path, capsys):
    path, _ = key_file
    out = tmp_path / "secrets.toml"
    out.write_text("keep me", encoding="utf-8")
    assert make.main([str(path), "--sheet-id", SHEET_ID, "--output", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "keep me" and "--force" in capsys.readouterr().err
    assert make.main([str(path), "--sheet-id", SHEET_ID, "--output", str(out), "--force", "--password", "pw"]) == 0
    assert tomllib.loads(out.read_text(encoding="utf-8"))["app_password"] == "pw"
    assert make.main([str(tmp_path / "nope.json"), "--sheet-id", SHEET_ID, "--output", str(tmp_path / "x.toml")]) == 2
    assert make.main([str(path), "--sheet-id", "nonsense", "--output", str(tmp_path / "y.toml")]) == 2
    assert not (tmp_path / "x.toml").exists() and not (tmp_path / "y.toml").exists()


def test_the_generated_file_is_what_the_app_connects_with(key_file, tmp_path):
    pytest.importorskip("gspread")
    from core.sheets import connect

    path, info = key_file
    out = tmp_path / "secrets.toml"
    make.main([str(path), "--sheet-id", URL, "--output", str(out)])
    parsed = tomllib.loads(out.read_text(encoding="utf-8"))
    store = connect(dict(parsed["gcp_service_account"]), parsed["sheet_id"])  # exactly how app_common.py does it
    auth = getattr(store._client, "http_client", store._client).auth
    assert auth.service_account_email == info["client_email"]


def test_the_real_secrets_file_is_never_committed_and_the_key_name_pattern_is_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".streamlit/secrets.toml" in ignore and "*service-account*.json" in ignore


# ---------------------------------------------------------------- check_connection


class FakeSheet:
    def __init__(self, title, rows):
        self.title, self._rows = title, rows
        self.row_count, self.col_count = len(rows) + 100, max(len(r) for r in rows)

    def get_all_values(self):
        return self._rows


class FakeBook:
    title = "Overdue invoices"

    def __init__(self, tabs):
        self._tabs = tabs

    def worksheets(self):
        return self._tabs


def install_fake_gspread(monkeypatch, *, book=None, error=None):
    created = {}

    class Client:
        def open_by_key(self, key):
            created["key"] = key
            if error:
                raise error
            return book

    fake = types.SimpleNamespace(service_account_from_dict=lambda info, scopes: (created.update(scopes=scopes) or Client()))
    monkeypatch.setitem(sys.modules, "gspread", fake)
    return created


@pytest.fixture
def secrets_file(tmp_path, monkeypatch, key_file):
    path, _ = key_file
    out = tmp_path / "secrets.toml"
    make.main([str(path), "--sheet-id", SHEET_ID, "--output", str(out)])
    monkeypatch.setattr(check, "SECRETS", out)
    return out


class GoogleError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.response = types.SimpleNamespace(status_code=status)


def test_with_no_secrets_file_it_says_how_to_make_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "SECRETS", tmp_path / "missing.toml")
    assert check.main() == 2 and "make_secrets.py" in capsys.readouterr().out


def test_a_good_connection_reports_tabs_and_columns_and_changes_nothing(secrets_file, monkeypatch, capsys):
    from core.schema import INVOICES_EXISTING_COLUMNS, INVOICES_NEW_COLUMNS

    rows = [list(INVOICES_EXISTING_COLUMNS) + list(INVOICES_NEW_COLUMNS[:3])] + [["INV-1"] + [""] * 20, ["INV-2"] + [""] * 20, [""] * 21]
    book = FakeBook([FakeSheet("Invoices", rows), FakeSheet("Other", [["x"]])])
    created = install_fake_gspread(monkeypatch, book=book)
    assert check.main() == 0
    out = capsys.readouterr().out
    assert created["key"] == SHEET_ID and created["scopes"] == ["https://www.googleapis.com/auth/spreadsheets"]
    assert "Overdue invoices" in out and "Invoices (" in out and "Other (" in out and "2 data rows" in out
    assert "18 of 18 found" in out and f"3 of {len(INVOICES_NEW_COLUMNS)} already present" in out and "Nothing was changed" in out
    assert "BEGIN PRIVATE KEY" not in out


def test_missing_columns_are_named(secrets_file, monkeypatch, capsys):
    book = FakeBook([FakeSheet("Invoices", [["Invoice Number", "Customer"], ["INV-1", "A"]])])
    install_fake_gspread(monkeypatch, book=book)
    assert check.main() == 1 and "Missing: Country" in capsys.readouterr().out


def test_a_named_invoices_tab_that_does_not_exist_is_reported(secrets_file, monkeypatch, capsys):
    secrets_file.write_text(secrets_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    text = secrets_file.read_text(encoding="utf-8").replace("\n[gcp_service_account]", '\ninvoices_tab = "Nope"\n\n[gcp_service_account]', 1)
    secrets_file.write_text(text, encoding="utf-8")
    install_fake_gspread(monkeypatch, book=FakeBook([FakeSheet("Invoices", [["a"]])]))
    assert check.main() == 1 and "'Nope' is not one of the tabs" in capsys.readouterr().out


@pytest.mark.parametrize("error, expected", [
    (GoogleError(403, "The caller does not have permission"), "not shared with the service account"),
    (GoogleError(403, "Google Sheets API has not been used in project 123 before or it is disabled"), "Sheets API is not turned on"),
    (GoogleError(404, "Requested entity was not found"), "cannot find that sheet"),
    (GoogleError(400, "This operation is not supported for this document"), "uploaded Excel file"),
    (GoogleError(429, "Quota exceeded"), "too many requests"),
    (Exception("invalid_grant: Invalid JWT Signature"), "key was not accepted"),
    (Exception("something odd"), "Unexpected error"),
])
def test_each_google_failure_says_what_to_do(secrets_file, monkeypatch, capsys, error, expected):
    install_fake_gspread(monkeypatch, error=error)
    assert check.main() == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and expected in out and "followup@demo-project.iam.gserviceaccount.com" in out


def test_an_error_message_never_shows_the_key_or_the_sheet_id(secrets_file, monkeypatch, capsys):
    key = tomllib.loads(secrets_file.read_text(encoding="utf-8"))["gcp_service_account"]["private_key"]
    install_fake_gspread(monkeypatch, error=GoogleError(500, f"bad request for {SHEET_ID} using {key}"))
    check.main()
    out = capsys.readouterr().out
    assert SHEET_ID not in out.split("FAILED", 1)[1] and "BEGIN PRIVATE KEY" not in out.split("FAILED", 1)[1]
