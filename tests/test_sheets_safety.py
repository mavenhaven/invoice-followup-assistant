"""Review fixes in core/sheets.py: writes survive people editing the sheet, API calls are kept low and spaced out."""
from __future__ import annotations

import pytest
from test_sheets import FakeClient, FakeSpreadsheet, FakeWorksheet, invoice_rows

from core.schema import AUDIT_SHEET, Col
from core.sheets import RowNotFoundError, SheetsError, SheetStore


def ready_store(**kw):
    rows = invoice_rows()
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    ss = FakeSpreadsheet([ws])
    store = SheetStore(FakeClient(ss), "id", sleeper=lambda s: None, **kw)
    store.ensure_schema()
    return store, ws, ss


def status_of(ws: FakeWorksheet, invoice: str, occurrence: int = 1) -> str:
    header = ws.row_values(1)
    seen = 0
    for row in ws.grid[1:]:
        if row and row[0] == invoice:
            seen += 1
            if seen == occurrence:
                return row[header.index(Col.STATUS)] if len(row) > header.index(Col.STATUS) else ""
    raise AssertionError(f"{invoice} not found")


# ---------------------------------------------------------------- rows move while the app is working


def test_a_row_inserted_after_the_read_does_not_shift_the_writes_onto_the_wrong_invoice():
    store, ws, _ = ready_store()
    store.read_invoices()  # the app reads the sheet...
    ws.grid.insert(1, [""] * len(ws.grid[0]))  # ...then a person inserts a blank row at the top
    ws.grid.insert(1, ["NEW-1", "Someone new"] + [""] * (len(ws.grid[0]) - 2))  # and another row with a new invoice
    store.update_rows({"INV-2#1": {Col.STATUS: "HANDLED"}, "INV-1#2": {Col.STATUS: "NEEDS_HUMAN"}})
    assert status_of(ws, "INV-2") == "HANDLED"
    assert status_of(ws, "INV-1", 2) == "NEEDS_HUMAN" and status_of(ws, "INV-1", 1) == ""
    assert status_of(ws, "NEW-1") == "" and status_of(ws, "INV-3") == ""  # nobody else was touched


def test_rows_sorted_after_the_read_still_get_their_own_values():
    store, ws, _ = ready_store()
    store.read_invoices()
    ws.grid[1:] = sorted((r for r in ws.grid[1:] if any(r)), key=lambda r: r[0], reverse=True)
    store.update_rows({"INV-3#1": {Col.OUTCOME: "PAID"}})
    header = ws.row_values(1)
    inv3 = next(r for r in ws.grid[1:] if r and r[0] == "INV-3")
    assert inv3[header.index(Col.OUTCOME)] == "PAID"
    assert all(r[header.index(Col.OUTCOME)] == "" for r in ws.grid[1:] if r and r[0] != "INV-3" and len(r) > header.index(Col.OUTCOME))


def test_a_column_inserted_after_the_read_is_found_by_name():
    store, ws, _ = ready_store()
    store.read_invoices()
    for row in ws.grid:
        row.insert(1, "extra" if row is ws.grid[0] else "x")  # a person inserts a column next to the invoice number
    ws.col_count += 1
    store.update_rows({"INV-2#1": {Col.STATUS: "HANDLED"}})
    header = ws.row_values(1)
    row = next(r for r in ws.grid[1:] if r[0] == "INV-2")
    assert row[header.index(Col.STATUS)] == "HANDLED"


def test_a_row_that_was_deleted_is_reported_not_written_somewhere_else():
    store, ws, _ = ready_store()
    store.read_invoices()
    del ws.grid[2]  # INV-2 is deleted in the sheet
    with pytest.raises(RowNotFoundError):
        store.update_rows({"INV-2#1": {Col.STATUS: "HANDLED"}})
    assert status_of(ws, "INV-1", 1) == ""


def test_two_rows_sharing_a_row_key_stop_the_update_instead_of_guessing():
    store, ws, _ = ready_store()
    header = ws.row_values(1)
    copy = list(ws.grid[1])  # a person copies a whole row, row_key included
    ws.grid.append(copy)
    with pytest.raises(SheetsError, match="more than one row"):
        store.update_rows({"INV-1#1": {Col.STATUS: "HANDLED"}})
    store.update_rows({"INV-2#1": {Col.STATUS: "HANDLED"}})  # other rows are not affected by the problem
    assert status_of(ws, "INV-2") == "HANDLED" and header[0] == "Invoice Number"


def test_updates_need_the_row_key_column():
    rows = invoice_rows()
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    store = SheetStore(FakeClient(FakeSpreadsheet([ws])), "id")
    with pytest.raises(SheetsError, match="row_key"):
        store.update_rows({"INV-1#1": {Col.OUTCOME: "PAID"}})


# ---------------------------------------------------------------- fewer API calls


def counting(ss: FakeSpreadsheet) -> dict[str, int]:
    calls = {"worksheets": 0}
    original = ss.worksheets

    def counted():
        calls["worksheets"] += 1
        return original()
    ss.worksheets = counted
    return calls


def test_the_list_of_tabs_is_fetched_once_not_on_every_read():
    store, ws, ss = ready_store()
    calls = counting(ss)
    for _ in range(3):
        store.read_invoices()
        store.read_audit_log()
        store.read_run_log()
        store.read_config()
    assert calls["worksheets"] <= 1  # was one lookup per read


def test_a_tab_created_by_someone_else_is_found_after_one_refresh():
    store, ws, ss = ready_store()
    counting(ss)
    store.read_audit_log()
    ss.add_worksheet("Extra", 10, 5)  # a person adds a tab directly in Google Sheets
    assert store._find_ws("Extra") is not None


def test_a_tab_that_does_not_exist_is_still_reported_clearly():
    store, ws, ss = ready_store()
    ss._sheets[:] = [w for w in ss._sheets if w.title != AUDIT_SHEET]  # the Audit_Log tab is deleted
    with pytest.raises(SheetsError, match="Audit_Log"):
        store.read_audit_log()


def test_schema_check_with_nothing_to_do_makes_no_writes():
    store, ws, ss = ready_store()
    before = [c for c in ws.calls if c[0] in ("batch_update", "append_rows", "add_cols")]
    assert not store.ensure_schema().changed
    assert [c for c in ws.calls if c[0] in ("batch_update", "append_rows", "add_cols")] == before


# ---------------------------------------------------------------- write requests are spaced out


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_write_requests_keep_the_configured_gap_and_reads_are_never_delayed():
    clock = Clock()
    rows = invoice_rows()
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    store = SheetStore(FakeClient(FakeSpreadsheet([ws])), "id", sleeper=clock.sleep, clock=clock.time, write_interval=1.1)
    store.ensure_schema()
    clock.slept.clear()

    store.update_rows({"INV-1#1": {Col.STATUS: "A"}})  # a write straight after the schema writes: must wait
    assert len(clock.slept) == 1 and clock.slept[0] == pytest.approx(1.1)
    clock.slept.clear()

    for _ in range(5):
        store.read_invoices()  # reads do not wait
    assert clock.slept == []

    clock.now += 5  # five quiet seconds pass: the next write does not wait at all
    store.update_rows({"INV-2#1": {Col.STATUS: "B"}})
    assert clock.slept == []

    store.update_rows({"INV-1#1": {Col.STATUS: "C"}})  # immediately again: waits for the rest of the gap
    assert len(clock.slept) == 1 and 0 < clock.slept[0] <= 1.1


def test_a_burst_of_writes_stays_under_sixty_requests_a_minute():
    clock = Clock()
    rows = invoice_rows()
    ws = FakeWorksheet("Invoices", rows, col_count=len(rows[0]))
    store = SheetStore(FakeClient(FakeSpreadsheet([ws])), "id", sleeper=clock.sleep, clock=clock.time, write_interval=1.1, max_cells_per_request=1)
    store.ensure_schema()
    start, before = clock.now, len([c for c in ws.calls if c[0] == "batch_update"])
    store.update_rows({"INV-1#1": {c: "x" for c in (Col.STATUS, Col.HUMAN_REASON, Col.OUTCOME, Col.SUMMARY, Col.DQ_ISSUES, Col.LAST_CHANNEL)}})
    requests = len([c for c in ws.calls if c[0] == "batch_update"]) - before
    assert requests >= 5  # (two neighbouring columns go out as one range)
    assert (clock.now - start) / requests >= 1.05  # at least ~1 second per write request


def test_the_real_connection_defaults_to_spaced_writes():
    from core import sheets
    assert sheets.WRITE_INTERVAL_SECONDS >= 1.0
    assert SheetStore(FakeClient(FakeSpreadsheet([])), "id")._write_interval == 0.0  # tests and fakes are not slowed down


# ---------------------------------------------------------------- the real Google client (no network; skipped if gspread is missing)


def test_connect_builds_a_google_client_from_a_service_account_dict():
    pytest.importorskip("gspread")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    account = {"type": "service_account", "project_id": "p", "private_key_id": "kid", "private_key": pem,
               "client_email": "svc@p.iam.gserviceaccount.com", "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}
    from core.sheets import SCOPES, WRITE_INTERVAL_SECONDS, connect

    store = connect(account, "SHEETID")
    auth = getattr(store._client, "http_client", store._client).auth
    assert list(auth.scopes) == SCOPES == ["https://www.googleapis.com/auth/spreadsheets"]  # only Sheets, not all of Drive
    assert auth.service_account_email == "svc@p.iam.gserviceaccount.com" and store._write_interval == WRITE_INTERVAL_SECONDS


def test_the_gspread_methods_the_app_calls_exist_with_the_arguments_it_passes():
    gspread = pytest.importorskip("gspread")
    import inspect

    ws = gspread.Worksheet
    for name in ("get_all_values", "row_values", "col_values", "batch_update", "append_rows", "add_cols"):
        assert callable(getattr(ws, name)), name
    assert "value_input_option" in inspect.signature(ws.batch_update).parameters
    assert "value_input_option" in inspect.signature(ws.append_rows).parameters
    assert list(inspect.signature(gspread.Spreadsheet.add_worksheet).parameters)[1:4] == ["title", "rows", "cols"]
    assert isinstance(ws.col_count, property) and isinstance(ws.title, property)
    assert callable(gspread.Client.open_by_key) and callable(gspread.service_account_from_dict)
