"""Google Sheets storage layer. The sheet is the app's only data store.

This module has no Streamlit import. The caller passes in a gspread-style client (or uses
``connect``), so tests can pass an in-memory fake.

All reads return strings exactly as the sheet displays them (formatted values), with blank
cells as "". Parsing numbers and dates is the job of core/quality.py.
"""
from __future__ import annotations

import dataclasses
import random
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from core.schema import (
    AUDIT_COLUMNS,
    AUDIT_SHEET,
    CONFIG_COLUMNS,
    CONFIG_SHEET,
    INVOICES_COLUMNS,
    PROTECTED_COLUMNS,
    RUN_COLUMNS,
    RUN_SHEET,
    AuditEntry,
    Col,
    Config,
    RunLogEntry,
    to_cell,
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
RETRYABLE_STATUS = {429, 503}
WRITE_INTERVAL_SECONDS = 1.1  # Google allows about 60 write requests a minute per user
SHEET_ROW_COLUMN = "_sheet_row"  # 1-based row number in the worksheet, added to read_invoices()


class SheetsError(Exception):
    """A problem with the sheet's structure or a request to it."""


class RowNotFoundError(SheetsError, KeyError):
    """A row_key was not found in the Invoices sheet."""


@dataclasses.dataclass
class SchemaReport:
    created_sheets: list[str] = dataclasses.field(default_factory=list)
    added_invoice_columns: list[str] = dataclasses.field(default_factory=list)
    added_columns_other: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    row_keys_filled: int = 0
    config_keys_added: list[str] = dataclasses.field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.created_sheets or self.added_invoice_columns or self.added_columns_other
            or self.row_keys_filled or self.config_keys_added
        )


def col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    if n < 1:
        raise ValueError("column number must be >= 1")
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def a1(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def connect(service_account_info: Mapping[str, Any], sheet_id: str, **kwargs: Any) -> "SheetStore":
    """Build a SheetStore from a service-account dict (e.g. ``dict(st.secrets["gcp_service_account"])``)."""
    import gspread  # imported here so tests and tooling do not need gspread installed

    client = gspread.service_account_from_dict(dict(service_account_info), scopes=SCOPES)
    kwargs.setdefault("write_interval", WRITE_INTERVAL_SECONDS)
    return SheetStore(client, sheet_id, **kwargs)


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if code in RETRYABLE_STATUS or status in RETRYABLE_STATUS:
        return True
    text = str(exc).lower()
    return "429" in text and ("quota" in text or "rate" in text)


class SheetStore:
    """Reads and writes the Google Sheet. One instance per session."""

    def __init__(
        self,
        client: Any,
        sheet_id: str,
        *,
        invoices_tab: str | None = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
        max_cells_per_request: int = 500,
        write_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._sheet_id = sheet_id
        self._invoices_tab = invoices_tab
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._sleep = sleeper
        self._max_cells = max_cells_per_request
        self._write_interval = write_interval
        self._clock = clock
        self._last_write: float | None = None
        self._schema_checked_at: float | None = None
        self._ss: Any = None
        self._ws_list: list[Any] | None = None  # worksheet handles, fetched once instead of on every call
        self._inv_ws: Any = None
        self._headers: dict[str, list[str]] = {}
        self._row_index: dict[str, int] = {}

    # ------------------------------------------------------------------ plumbing

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call fn, retrying with exponential backoff and jitter on rate limits (HTTP 429/503)."""
        for attempt in range(self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
                if attempt >= self._max_retries or not _is_retryable(exc):
                    raise
                delay = self._base_delay * (2 ** attempt) + random.uniform(0, 0.25 * self._base_delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _write(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """A write request: kept at least write_interval seconds after the previous one (Google allows ~60 a minute)."""
        if self._write_interval > 0 and self._last_write is not None:
            wait = self._write_interval - (self._clock() - self._last_write)
            if wait > 0:
                self._sleep(wait)
        try:
            return self._call(fn, *args, **kwargs)
        finally:
            self._last_write = self._clock()

    def _spreadsheet(self) -> Any:
        if self._ss is None:
            self._ss = self._call(self._client.open_by_key, self._sheet_id)
        return self._ss

    def _worksheets(self, refresh: bool = False) -> list[Any]:
        if refresh or self._ws_list is None:
            self._ws_list = list(self._call(self._spreadsheet().worksheets))
        return self._ws_list

    def _find_ws(self, title: str) -> Any | None:
        """A worksheet by name. The list of tabs is fetched once and refreshed only when a name is not found."""
        for refresh in (False, True):
            for ws in self._worksheets(refresh):
                if ws.title == title:
                    return ws
        return None

    def _invoices_ws(self) -> Any:
        if self._inv_ws is not None:
            return self._inv_ws
        if self._invoices_tab:
            ws = self._find_ws(self._invoices_tab)
            if ws is None:
                raise SheetsError(f"Worksheet {self._invoices_tab!r} not found in the sheet")
        else:
            sheets = self._worksheets()
            if not sheets:
                raise SheetsError("The spreadsheet has no worksheets")
            ws = sheets[0]
        self._inv_ws = ws
        return ws

    def _require_ws(self, title: str) -> Any:
        ws = self._find_ws(title)
        if ws is None:
            raise SheetsError(f"Worksheet {title!r} is missing. Run ensure_schema() first.")
        return ws

    def _header_of(self, ws: Any) -> list[str]:
        header = [str(c).strip() for c in self._call(ws.row_values, 1)]
        while header and header[-1] == "":
            header.pop()
        if len(set(header)) != len(header):
            dupes = sorted({h for h in header if header.count(h) > 1})
            raise SheetsError(f"Worksheet {ws.title!r} has duplicate column names: {dupes}")
        self._headers[ws.title] = header
        return header

    def _ensure_header(self, ws: Any, required: Sequence[str]) -> list[str]:
        """Append any required columns missing from row 1. Returns the columns added."""
        header = self._header_of(ws)
        missing = [c for c in required if c not in header]
        if not missing:
            return []
        needed = len(header) + len(missing)
        if ws.col_count < needed:
            self._write(ws.add_cols, needed - ws.col_count)
        self._write(
            ws.batch_update,
            [{"range": a1(1, len(header) + 1), "values": [missing]}],
            value_input_option="RAW",
        )
        self._headers[ws.title] = header + missing
        return missing

    def _get_or_create(self, title: str, columns: Sequence[str], report: SchemaReport) -> Any:
        ws = self._find_ws(title)
        if ws is None:
            ws = self._write(
                self._spreadsheet().add_worksheet, title=title, rows=1000, cols=max(len(columns), 10)
            )
            report.created_sheets.append(title)
            self._ws_list = None
        added = self._ensure_header(ws, columns)
        if added and title not in report.created_sheets:
            report.added_columns_other[title] = added
        return ws

    # ------------------------------------------------------------------ schema

    def ensure_schema_once(self, ttl_seconds: float = 600.0) -> SchemaReport:
        """ensure_schema(), but at most once per ttl_seconds for this connection (shared by every visitor of the app).

        The check costs about 8 read requests, so it should not run for every new browser session.
        """
        now = self._clock()
        if self._schema_checked_at is not None and now - self._schema_checked_at < ttl_seconds:
            return SchemaReport()
        report = self.ensure_schema()
        self._schema_checked_at = now
        return report

    def ensure_schema(self) -> SchemaReport:
        """Create missing worksheets, append missing columns, seed Config, and fill row keys.

        Safe to call on every start: when nothing is missing it makes no writes.
        """
        report = SchemaReport()
        inv = self._invoices_ws()
        if not self._header_of(inv):
            raise SheetsError(f"Worksheet {inv.title!r} has no header row")
        report.added_invoice_columns = self._ensure_header(inv, INVOICES_COLUMNS)

        self._get_or_create(AUDIT_SHEET, AUDIT_COLUMNS, report)
        self._get_or_create(RUN_SHEET, RUN_COLUMNS, report)
        cfg = self._get_or_create(CONFIG_SHEET, CONFIG_COLUMNS, report)

        existing = {r[0] for r in self._call(cfg.get_all_values)[1:] if r and r[0]}
        new_rows = [[k, v] for k, v in Config.default_rows().items() if k not in existing]
        if new_rows:
            self._write(cfg.append_rows, new_rows, value_input_option="RAW")
            report.config_keys_added = [r[0] for r in new_rows]

        report.row_keys_filled = self._fill_row_keys()
        return report

    # ------------------------------------------------------------------ invoices

    def _read_invoices_raw(self) -> tuple[pd.DataFrame, list[int]]:
        ws = self._invoices_ws()
        values = self._call(ws.get_all_values)
        if not values:
            raise SheetsError(f"Worksheet {ws.title!r} is empty")
        header = [str(c).strip() for c in values[0]]
        while header and header[-1] == "":
            header.pop()
        self._headers[ws.title] = header
        width = len(header)

        records: list[list[str]] = []
        sheet_rows: list[int] = []
        for offset, row in enumerate(values[1:], start=2):
            cells = [str(c) for c in row[:width]] + [""] * max(0, width - len(row))
            if all(c.strip() == "" for c in cells):
                continue  # fully blank row: skip but keep the numbering of the rest
            records.append(cells)
            sheet_rows.append(offset)

        df = pd.DataFrame(records, columns=header, dtype=object)
        df[SHEET_ROW_COLUMN] = sheet_rows

        blank_positions: list[int] = []
        if Col.ROW_KEY not in df.columns:
            df[Col.ROW_KEY] = ""
        keys = df[Col.ROW_KEY].astype(str).str.strip().tolist()
        taken = {k for k in keys if k}
        seen: dict[str, int] = {}
        inv_numbers = df[Col.INVOICE_NUMBER].astype(str).str.strip().tolist() if Col.INVOICE_NUMBER in df.columns else [""] * len(df)
        for pos, (key, inv_no) in enumerate(zip(keys, inv_numbers)):
            base = inv_no or f"ROW{sheet_rows[pos]}"
            seen[base] = seen.get(base, 0) + 1
            if key:
                continue
            n = seen[base]
            candidate = f"{base}#{n}"
            while candidate in taken:
                n += 1
                candidate = f"{base}#{n}"
            keys[pos] = candidate
            taken.add(candidate)
            blank_positions.append(pos)
        df[Col.ROW_KEY] = keys
        self._row_index = dict(zip(keys, sheet_rows))
        return df, blank_positions

    def read_invoices(self) -> pd.DataFrame:
        """All data rows as strings. Adds ``row_key`` (computed if not yet stored) and ``_sheet_row``."""
        df, _ = self._read_invoices_raw()
        return df

    def _fill_row_keys(self) -> int:
        """Persist computed row keys for rows whose row_key cell is blank. Returns how many."""
        ws = self._invoices_ws()
        df, blank = self._read_invoices_raw()
        header = self._headers[ws.title]
        if not blank or Col.ROW_KEY not in header:
            return 0
        key_col = header.index(Col.ROW_KEY) + 1
        self._write_cells(
            ws, [(int(df.at[pos, SHEET_ROW_COLUMN]), key_col, df.at[pos, Col.ROW_KEY]) for pos in blank]
        )
        return len(blank)

    def update_rows(self, updates: Mapping[str, Mapping[str, Any]]) -> int:
        """Write only the listed cells. ``updates`` maps row_key -> {column: value}.

        Rows and columns are looked up in the sheet itself just before writing (two small reads), never
        from a position remembered earlier: if a person inserted, deleted or sorted rows since the last
        read, the values still land on the right invoices. Returns the number of cells written. Raises
        RowNotFoundError for an unknown row_key, and SheetsError for an unknown or protected column
        (Notes is never written), or when two rows share a row_key.
        """
        if not updates:
            return 0
        ws = self._invoices_ws()
        header = self._header_of(ws)
        if Col.ROW_KEY not in header:
            raise SheetsError("The invoice sheet has no row_key column. Run ensure_schema() first.")
        keys = [str(k).strip() for k in self._call(ws.col_values, header.index(Col.ROW_KEY) + 1)]
        where: dict[str, int] = {}
        duplicated: set[str] = set()
        for row_number, key in enumerate(keys[1:], start=2):
            if key:
                if key in where:
                    duplicated.add(key)
                where.setdefault(key, row_number)
        self._row_index = dict(where)

        cells: list[tuple[int, int, Any]] = []
        for row_key, columns in updates.items():
            if row_key not in where:
                raise RowNotFoundError(row_key)
            if row_key in duplicated:
                raise SheetsError(f"row_key {row_key!r} appears on more than one row; remove the copy so the app knows which to update")
            for column, value in columns.items():
                if column in PROTECTED_COLUMNS:
                    raise SheetsError(f"Column {column!r} is protected and cannot be written by the app")
                if column not in header:
                    raise SheetsError(f"Unknown column {column!r}. Run ensure_schema() to add missing columns.")
                cells.append((where[row_key], header.index(column) + 1, value))
        return self._write_cells(ws, cells)

    def _write_cells(self, ws: Any, cells: Iterable[tuple[int, int, Any]]) -> int:
        """Write cells as per-row runs of adjacent columns, in requests of at most max_cells cells."""
        by_row: dict[int, dict[int, Any]] = {}
        for row, col, value in cells:
            by_row.setdefault(row, {})[col] = to_cell(value)

        ranges: list[dict[str, Any]] = []
        for row in sorted(by_row):
            cols = sorted(by_row[row])
            run = [cols[0]]
            for c in cols[1:] + [None]:  # type: ignore[list-item]
                if c is not None and c == run[-1] + 1:
                    run.append(c)
                    continue
                rng = a1(row, run[0]) if len(run) == 1 else f"{a1(row, run[0])}:{a1(row, run[-1])}"
                ranges.append({"range": rng, "values": [[by_row[row][x] for x in run]]})
                run = [c] if c is not None else []

        total = 0
        batch: list[dict[str, Any]] = []
        batch_cells = 0
        for item in ranges:
            n = len(item["values"][0])
            if batch and batch_cells + n > self._max_cells:
                self._write(ws.batch_update, batch, value_input_option="RAW")
                batch, batch_cells = [], 0
            batch.append(item)
            batch_cells += n
            total += n
        if batch:
            self._write(ws.batch_update, batch, value_input_option="RAW")
        return total

    # ------------------------------------------------------------------ logs

    @staticmethod
    def _as_dict(entry: Any) -> dict[str, Any]:
        if hasattr(entry, "to_dict"):
            return entry.to_dict()
        return {k: to_cell(v) for k, v in dict(entry).items()}

    def _append(self, title: str, columns: Sequence[str], entries: Sequence[Any]) -> int:
        if not entries:
            return 0
        ws = self._require_ws(title)
        header = self._headers.get(title) or self._header_of(ws)
        if any(c not in header for c in columns):
            header = self._header_of(ws)
        rows = []
        for entry in entries:
            data = self._as_dict(entry)
            unknown = set(data) - set(header)
            if unknown:
                raise SheetsError(f"{title}: unknown columns {sorted(unknown)}")
            rows.append([data.get(c, "") for c in header])
        self._write(ws.append_rows, rows, value_input_option="RAW")
        return len(rows)

    def append_audit(self, entries: Sequence[AuditEntry | Mapping[str, Any]]) -> int:
        """Append audit rows (append only). Returns the number appended."""
        return self._append(AUDIT_SHEET, AUDIT_COLUMNS, entries)

    def append_run_log(self, entry: RunLogEntry | Mapping[str, Any]) -> int:
        """Append the Run_Log row for a run (state RUNNING at start)."""
        return self._append(RUN_SHEET, RUN_COLUMNS, [entry])

    def update_run_log(self, run_id: str, fields: Mapping[str, Any]) -> int:
        """Update cells of an existing Run_Log row found by run_id. Returns cells written."""
        ws = self._require_ws(RUN_SHEET)
        header = self._header_of(ws)
        if "run_id" not in header:
            raise SheetsError("Run_Log has no run_id column")
        ids = self._call(ws.col_values, header.index("run_id") + 1)
        try:
            row = ids.index(run_id) + 1
        except ValueError:
            raise RowNotFoundError(run_id) from None
        cells = []
        for column, value in fields.items():
            if column not in header:
                raise SheetsError(f"Run_Log: unknown column {column!r}")
            cells.append((row, header.index(column) + 1, value))
        return self._write_cells(ws, cells)

    def read_run_log(self) -> pd.DataFrame:
        return self._read_table(RUN_SHEET)

    def read_audit_log(self) -> pd.DataFrame:
        return self._read_table(AUDIT_SHEET)

    def _read_table(self, title: str) -> pd.DataFrame:
        ws = self._require_ws(title)
        values = self._call(ws.get_all_values)
        header = [str(c).strip() for c in (values[0] if values else [])]
        rows = [[str(c) for c in r[: len(header)]] + [""] * (len(header) - len(r)) for r in values[1:]]
        rows = [r for r in rows if any(c.strip() for c in r)]
        return pd.DataFrame(rows, columns=header, dtype=object)

    def has_running_run(self, run_date: str) -> bool:
        """True if Run_Log holds a RUNNING row for run_date (BR-24 lock)."""
        df = self.read_run_log()
        if df.empty:
            return False
        return bool(((df["run_date"] == str(run_date)) & (df["state"] == "RUNNING")).any())

    # ------------------------------------------------------------------ config

    def read_config(self) -> dict[str, str]:
        """Config worksheet as {key: value} strings."""
        ws = self._require_ws(CONFIG_SHEET)
        return {r[0]: (r[1] if len(r) > 1 else "") for r in self._call(ws.get_all_values)[1:] if r and r[0]}

    def load_config(self) -> Config:
        return Config.from_mapping(self.read_config())

    def set_config(self, key: str, value: Any) -> None:
        """Set one Config value, adding the key if it is missing."""
        if isinstance(value, bool):  # Config rows use true / false, not the Yes / No used in invoice columns
            value = "true" if value else "false"
        ws = self._require_ws(CONFIG_SHEET)
        keys = self._call(ws.col_values, 1)
        if key in keys:
            self._write_cells(ws, [(keys.index(key) + 1, 2, value)])
        else:
            self._write(ws.append_rows, [[key, to_cell(value)]], value_input_option="RAW")
