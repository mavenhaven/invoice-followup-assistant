"""Tests for core/sheets.py and core/schema.py using an in-memory fake of gspread."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from core import schema
from core.schema import AuditEntry, Channel, Col, Config, RunLogEntry, RunState, Status
from core.sheets import RowNotFoundError, SheetsError, SheetStore, a1, col_letter


# ---------------------------------------------------------------- fake gspread


class FakeAPIError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"{code}: quota exceeded" if code == 429 else f"{code}: error")
        self.code = code


def _split_a1(cell: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", cell)
    assert m, f"bad A1 cell {cell!r}"
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


class FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None, col_count: int | None = None) -> None:
        self.title = title
        self.grid: list[list[object]] = [list(r) for r in (rows or [])]
        width = max((len(r) for r in self.grid), default=0)
        self.col_count = col_count if col_count is not None else max(width, 1)
        self.calls: list[tuple[str, object]] = []
        self.fail_next: dict[str, list[Exception]] = {}

    def _enter(self, name: str, detail: object = None) -> None:
        self.calls.append((name, detail))
        queue = self.fail_next.get(name)
        if queue:
            raise queue.pop(0)

    def _set(self, row: int, col: int, value: object) -> None:
        assert col <= self.col_count, f"column {col} beyond col_count {self.col_count}: add_cols was not called"
        while len(self.grid) < row:
            self.grid.append([])
        r = self.grid[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def get_all_values(self) -> list[list[str]]:
        self._enter("get_all_values")
        width = max((len(r) for r in self.grid), default=0)
        rows = [[("" if c is None else str(c)) for c in r] + [""] * (width - len(r)) for r in self.grid]
        while rows and all(c == "" for c in rows[-1]):
            rows.pop()
        return rows

    def row_values(self, row: int) -> list[str]:
        self._enter("row_values")
        if row > len(self.grid):
            return []
        return [str(c) for c in self.grid[row - 1]]

    def col_values(self, col: int) -> list[str]:
        self._enter("col_values")
        out = [str(r[col - 1]) if len(r) >= col else "" for r in self.grid]
        while out and out[-1] == "":
            out.pop()
        return out

    def add_cols(self, n: int) -> None:
        self._enter("add_cols", n)
        self.col_count += n

    def batch_update(self, data: list[dict], value_input_option: str | None = None) -> None:
        self._enter("batch_update", data)
        for item in data:
            start = item["range"].split(":")[0]
            row, col = _split_a1(start)
            for dr, values in enumerate(item["values"]):
                for dc, value in enumerate(values):
                    self._set(row + dr, col + dc, value)

    def append_rows(self, rows: list[list[object]], value_input_option: str | None = None) -> None:
        self._enter("append_rows", rows)
        while self.grid and all(c in ("", None) for c in self.grid[-1]):
            self.grid.pop()
        for r in rows:
            assert len(r) <= self.col_count
            self.grid.append(list(r))


class FakeSpreadsheet:
    def __init__(self, sheets: list[FakeWorksheet]) -> None:
        self._sheets = sheets

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self._sheets)

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        ws = FakeWorksheet(title, col_count=cols)
        self._sheets.append(ws)
        return ws


class FakeClient:
    def __init__(self, spreadsheet: FakeSpreadsheet) -> None:
        self.spreadsheet = spreadsheet
        self.opened: list[str] = []

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        self.opened.append(key)
        return self.spreadsheet


# ---------------------------------------------------------------- fixtures


def invoice_rows() -> list[list[str]]:
    """Header (18 existing columns) plus 4 rows; INV-1 is duplicated and a blank line sits in the middle."""
    header = list(schema.INVOICES_EXISTING_COLUMNS)

    def row(inv: str, customer: str, outstanding: str = "100", notes: str = "") -> list[str]:
        r = [""] * len(header)
        r[header.index(Col.INVOICE_NUMBER)] = inv
        r[header.index(Col.CUSTOMER)] = customer
        r[header.index(Col.AMOUNT_OUTSTANDING)] = outstanding
        r[header.index(Col.NOTES)] = notes
        return r

    return [
        header,
        row("INV-1", "Alpha GmbH"),
        row("INV-2", "Beta SRL", notes="human note"),
        [""] * len(header),
        row("INV-1", "Alpha GmbH", notes="Duplicate export row"),
        ["INV-3", "Short Row Kft."],  # shorter than the header: must be padded
    ]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def invoices() -> FakeWorksheet:
    rows = invoice_rows()
    return FakeWorksheet("Invoices", rows, col_count=len(rows[0]))


@pytest.fixture
def store(invoices: FakeWorksheet, sleeps: list[float]) -> SheetStore:
    client = FakeClient(FakeSpreadsheet([invoices]))
    return SheetStore(client, "sheet-id", sleeper=sleeps.append, max_cells_per_request=500)


# ---------------------------------------------------------------- helpers and schema


def test_col_letter_and_a1():
    assert [col_letter(n) for n in (1, 26, 27, 52, 53)] == ["A", "Z", "AA", "AZ", "BA"]
    assert a1(3, 28) == "AB3"


def test_column_lists_have_no_duplicates_and_keep_existing_order():
    cols = schema.INVOICES_COLUMNS
    assert len(cols) == len(set(cols))
    assert cols[:18] == schema.INVOICES_EXISTING_COLUMNS
    assert len(schema.INVOICES_EXISTING_COLUMNS) == 18
    assert Col.NOTES in schema.PROTECTED_COLUMNS


def test_to_cell_conversions():
    assert schema.to_cell(None) == ""
    assert schema.to_cell(Status.HANDLED) == "HANDLED"
    assert schema.to_cell(True) == "Yes"
    assert schema.to_cell(dt.date(2026, 8, 4)) == "2026-08-04"
    assert schema.to_cell(dt.datetime(2026, 8, 4, 9, 5, 3)) == "2026-08-04 09:05:03"
    assert schema.to_cell(["a", 1]) == '["a", 1]'
    assert schema.to_cell(7) == 7


def test_config_defaults_roundtrip_and_validation():
    cfg = Config.from_mapping(Config.default_rows())
    assert cfg == Config()
    assert Config.from_mapping({}).attempt_cap == 5
    assert Config.from_mapping({"attempt_cap": "7", "min_amount_eur": "12,5"}).min_amount["EUR"] == 12.5
    with pytest.raises(ValueError, match="attempt_cap"):
        Config.from_mapping({"attempt_cap": "five"})
    with pytest.raises(ValueError, match="skip_below_minimum"):
        Config.from_mapping({"skip_below_minimum": "maybe"})


def test_sheets_module_does_not_import_streamlit():
    source = (Path(__file__).parent.parent / "core" / "sheets.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source and "from streamlit" not in source


# ---------------------------------------------------------------- ensure_schema


def test_ensure_schema_creates_sheets_columns_config_and_row_keys(store: SheetStore, invoices: FakeWorksheet):
    report = store.ensure_schema()

    assert report.changed
    assert report.created_sheets == [schema.AUDIT_SHEET, schema.RUN_SHEET, schema.CONFIG_SHEET]
    assert report.added_invoice_columns == list(schema.INVOICES_NEW_COLUMNS)
    assert report.row_keys_filled == 4
    assert set(report.config_keys_added) == set(Config.default_rows())

    header = invoices.row_values(1)
    assert header == list(schema.INVOICES_COLUMNS)  # existing first, new appended in order
    df = store.read_invoices()
    assert df[Col.ROW_KEY].tolist() == ["INV-1#1", "INV-2#1", "INV-1#2", "INV-3#1"]
    assert store.read_config()["attempt_cap"] == "5"


def test_ensure_schema_is_idempotent_and_writes_nothing_the_second_time(store: SheetStore, invoices: FakeWorksheet):
    store.ensure_schema()
    writes_before = [c for c in invoices.calls if c[0] in ("batch_update", "append_rows", "add_cols")]
    snapshot = [list(r) for r in invoices.grid]

    report = store.ensure_schema()

    assert not report.changed
    assert [list(r) for r in invoices.grid] == snapshot
    assert [c for c in invoices.calls if c[0] in ("batch_update", "append_rows", "add_cols")] == writes_before


def test_ensure_schema_does_not_overwrite_user_config(store: SheetStore):
    store.ensure_schema()
    store.set_config("attempt_cap", 4)
    store.ensure_schema()
    assert store.read_config()["attempt_cap"] == "4"
    assert store.load_config().attempt_cap == 4


def test_ensure_schema_adds_only_missing_columns_to_existing_log_sheet(invoices: FakeWorksheet):
    partial = FakeWorksheet(schema.AUDIT_SHEET, [["event_id", "run_id"]], col_count=2)
    ss = FakeSpreadsheet([invoices, partial])
    store = SheetStore(FakeClient(ss), "id")
    report = store.ensure_schema()
    assert schema.AUDIT_SHEET not in report.created_sheets
    assert report.added_columns_other[schema.AUDIT_SHEET] == list(schema.AUDIT_COLUMNS[2:])
    assert partial.row_values(1) == list(schema.AUDIT_COLUMNS)


def test_ensure_schema_respects_invoices_tab_name(invoices: FakeWorksheet):
    other = FakeWorksheet("Scratch", [["x"]])
    store = SheetStore(FakeClient(FakeSpreadsheet([other, invoices])), "id", invoices_tab="Invoices")
    store.ensure_schema()
    assert other.row_values(1) == ["x"]
    assert store.read_invoices().shape[0] == 4
    with pytest.raises(SheetsError, match="Missing"):
        SheetStore(FakeClient(FakeSpreadsheet([other])), "id", invoices_tab="Missing").ensure_schema()


def test_duplicate_header_names_are_rejected():
    ws = FakeWorksheet("Invoices", [["A", "A"], ["1", "2"]])
    with pytest.raises(SheetsError, match="duplicate"):
        SheetStore(FakeClient(FakeSpreadsheet([ws])), "id").ensure_schema()


# ---------------------------------------------------------------- reading


def test_read_invoices_pads_short_rows_skips_blank_rows_and_tracks_sheet_rows(store: SheetStore):
    store.ensure_schema()
    df = store.read_invoices()
    assert len(df) == 4
    assert df["_sheet_row"].tolist() == [2, 3, 5, 6]  # the blank line (row 4) is skipped, numbering kept
    short = df[df[Col.INVOICE_NUMBER] == "INV-3"].iloc[0]
    assert short[Col.CUSTOMER] == "Short Row Kft."
    assert short[Col.EMAIL] == ""
    assert df[Col.NOTES].tolist()[1] == "human note"


def test_existing_row_keys_are_kept_and_new_rows_get_unused_keys(invoices: FakeWorksheet):
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id")
    store.ensure_schema()
    # a new row for INV-1 appended later must become #3 and must not touch existing keys
    width = len(invoices.row_values(1))
    new = [""] * width
    new[0] = "INV-1"
    invoices.append_rows([new])
    df = store.read_invoices()
    assert df[Col.ROW_KEY].tolist()[:4] == ["INV-1#1", "INV-2#1", "INV-1#2", "INV-3#1"]
    assert df[Col.ROW_KEY].tolist()[4] == "INV-1#3"


# ---------------------------------------------------------------- writing


def test_update_rows_writes_only_the_given_cells(store: SheetStore, invoices: FakeWorksheet):
    store.ensure_schema()
    before = [list(r) for r in invoices.grid]

    n = store.update_rows({
        "INV-1#2": {Col.STATUS: Status.DQ_FAILED, Col.DQ_STATUS: "FAIL", Col.HUMAN_FLAG: True,
                    Col.LAST_PROCESSED_DATE: dt.date(2026, 8, 20)},
    })

    assert n == 4
    header = invoices.row_values(1)
    row = invoices.grid[4]  # sheet row 5
    assert row[header.index(Col.STATUS)] == "DQ_FAILED"
    assert row[header.index(Col.HUMAN_FLAG)] == "Yes"
    assert row[header.index(Col.LAST_PROCESSED_DATE)] == "2026-08-20"
    width = max(len(r) for r in invoices.grid)

    def pad(r: list) -> list:
        return list(r) + [""] * (width - len(r))

    changed = [
        (r, c)
        for r, (a, b) in enumerate(zip(before, invoices.grid))
        for c, (x, y) in enumerate(zip(pad(a), pad(b)))
        if x != y
    ]
    assert {r for r, _ in changed} == {4}  # only that one row changed
    assert len(changed) == 4  # and only the four cells we wrote


def test_update_rows_coalesces_adjacent_columns_into_one_range(store: SheetStore, invoices: FakeWorksheet):
    store.ensure_schema()
    invoices.calls.clear()
    store.update_rows({"INV-2#1": {Col.EMAIL_ATTEMPTS: 1, Col.PHONE_ATTEMPTS: 2, Col.LEGACY_ATTEMPTS: 0}})
    (name, data), = [c for c in invoices.calls if c[0] == "batch_update"]
    assert len(data) == 1 and ":" in data[0]["range"]
    assert data[0]["values"] == [[1, 2, 0]]


def test_update_rows_rejects_notes_unknown_column_and_unknown_row(store: SheetStore):
    store.ensure_schema()
    with pytest.raises(SheetsError, match="protected"):
        store.update_rows({"INV-1#1": {Col.NOTES: "overwrite"}})
    with pytest.raises(SheetsError, match="Unknown column"):
        store.update_rows({"INV-1#1": {"not_a_column": 1}})
    with pytest.raises(RowNotFoundError):
        store.update_rows({"INV-404#1": {Col.STATUS: "HANDLED"}})


def test_update_rows_never_changes_notes(store: SheetStore, invoices: FakeWorksheet):
    store.ensure_schema()
    header = invoices.row_values(1)
    notes_before = [r[header.index(Col.NOTES)] if len(r) > header.index(Col.NOTES) else "" for r in invoices.grid]
    store.update_rows({"INV-2#1": {Col.STATUS: "IN_PROGRESS", Col.SUMMARY: "Called, no answer"}})
    notes_after = [r[header.index(Col.NOTES)] if len(r) > header.index(Col.NOTES) else "" for r in invoices.grid]
    assert notes_before == notes_after


def test_update_rows_splits_large_batches(invoices: FakeWorksheet):
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id", max_cells_per_request=2)
    store.ensure_schema()
    invoices.calls.clear()
    # four cells in non-adjacent columns -> four ranges, two per request
    n = store.update_rows({"INV-1#1": {Col.STATUS: "A", Col.DQ_STATUS: "B", Col.HUMAN_REASON: "C", Col.LAST_CHANNEL: "D"}})
    assert n == 4
    assert len([c for c in invoices.calls if c[0] == "batch_update"]) >= 2


def test_update_rows_empty_makes_no_calls(store: SheetStore, invoices: FakeWorksheet):
    store.ensure_schema()
    invoices.calls.clear()
    assert store.update_rows({}) == 0
    assert invoices.calls == []


# ---------------------------------------------------------------- logs


def test_append_audit_uses_sheet_header_order(store: SheetStore):
    store.ensure_schema()
    entry = AuditEntry(
        event_id="e1", run_id="r1", row_key="INV-1#1", timestamp=dt.datetime(2026, 8, 20, 9, 0, 0),
        run_date=dt.date(2026, 8, 20), channel=Channel.EMAIL, attempt_no_for_channel=1,
        counted_as_attempt=True, outcome="NO_RESPONSE", status_before=Status.IN_PROGRESS,
        status_after=Status.IN_PROGRESS,
    )
    assert store.append_audit([entry, {"event_id": "e2", "run_id": "r1", "row_key": "INV-2#1"}]) == 2
    df = store.read_audit_log()
    assert list(df.columns) == list(schema.AUDIT_COLUMNS)
    assert df.loc[0, "channel"] == "email"
    assert df.loc[0, "counted_as_attempt"] == "Yes"
    assert df.loc[0, "run_date"] == "2026-08-20"
    assert df.loc[1, "actor"] == ""  # a plain mapping is written as given; only dataclass defaults fill in
    assert store.append_audit([]) == 0


def test_append_audit_rejects_unknown_columns(store: SheetStore):
    store.ensure_schema()
    with pytest.raises(SheetsError, match="unknown columns"):
        store.append_audit([{"event_id": "e", "nonsense": 1}])


def test_logs_require_ensure_schema(store: SheetStore):
    with pytest.raises(SheetsError, match="ensure_schema"):
        store.append_audit([{"event_id": "e"}])


def test_run_log_append_update_and_lock(store: SheetStore):
    store.ensure_schema()
    day = dt.date(2026, 8, 20)
    store.append_run_log(RunLogEntry(run_id="run-1", run_date=day, started_at=dt.datetime(2026, 8, 20, 9, 0, 0)))
    assert store.has_running_run("2026-08-20")
    assert not store.has_running_run("2026-08-21")

    cells = store.update_run_log("run-1", {"state": RunState.DONE, "rows_total": 35, "errors": [{"row_key": "X", "message": "boom"}]})
    assert cells == 3
    df = store.read_run_log()
    assert df.loc[0, "state"] == "DONE"
    assert df.loc[0, "rows_total"] == "35"
    assert "boom" in df.loc[0, "errors"]
    assert not store.has_running_run("2026-08-20")
    with pytest.raises(RowNotFoundError):
        store.update_run_log("nope", {"state": "DONE"})
    with pytest.raises(SheetsError, match="unknown column"):
        store.update_run_log("run-1", {"bogus": 1})


# ---------------------------------------------------------------- retry


def test_retries_on_429_with_exponential_backoff_then_succeeds(invoices: FakeWorksheet, sleeps: list[float]):
    invoices.fail_next["get_all_values"] = [FakeAPIError(429), FakeAPIError(429)]
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id", base_delay=1.0, sleeper=sleeps.append)
    df = store.read_invoices()
    assert len(df) == 4
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] < 1.3
    assert 2.0 <= sleeps[1] < 2.6  # doubles each time


def test_gives_up_after_max_retries_and_raises_the_original_error(invoices: FakeWorksheet, sleeps: list[float]):
    invoices.fail_next["get_all_values"] = [FakeAPIError(429) for _ in range(10)]
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id", max_retries=3, sleeper=sleeps.append)
    with pytest.raises(FakeAPIError):
        store.read_invoices()
    assert len(sleeps) == 3


def test_non_rate_limit_errors_are_not_retried(invoices: FakeWorksheet, sleeps: list[float]):
    invoices.fail_next["get_all_values"] = [FakeAPIError(403)]
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id", sleeper=sleeps.append)
    with pytest.raises(FakeAPIError):
        store.read_invoices()
    assert sleeps == []


def test_writes_are_retried_too(invoices: FakeWorksheet, sleeps: list[float]):
    store = SheetStore(FakeClient(FakeSpreadsheet([invoices])), "id", sleeper=sleeps.append)
    store.ensure_schema()
    invoices.fail_next["batch_update"] = [FakeAPIError(429)]
    store.update_rows({"INV-1#1": {Col.STATUS: "IN_PROGRESS"}})
    assert len(sleeps) == 1
    header = invoices.row_values(1)
    assert invoices.grid[1][header.index(Col.STATUS)] == "IN_PROGRESS"
