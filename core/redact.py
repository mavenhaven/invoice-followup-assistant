"""Keep secrets out of anything shown on screen or written to the sheet (error messages, run log, audit log)."""
from __future__ import annotations

import re
from typing import Iterable

MASK = "[hidden]"

_PATTERNS = [
    # a whole PEM private key block
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    # "private_key": "...."  or  private_key = "...."  (JSON or TOML)
    re.compile(r"""(["']?(?:private_key|private_key_id|client_secret|password|app_password|token|api_key)["']?\s*[:=]\s*)(["'])(.*?)(\2)""", re.I | re.S),
    # Google access tokens and bearer headers
    re.compile(r"\bya29\.[A-Za-z0-9_\-\.]+"),
    re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9_\-\.=/+]{12,}"),
    # the spreadsheet id inside a Google Sheets address
    re.compile(r"(/spreadsheets/d/)[A-Za-z0-9_\-]{20,}"),
]
_MIN_SECRET_LENGTH = 6  # shorter values would also hide ordinary words


def redact(text: object, secrets: Iterable[object] = ()) -> str:
    """Return text with keys, tokens, addresses of the sheet, and any exact secret values replaced by [hidden]."""
    out = str(text)
    for value in secrets:
        v = str(value or "")
        if len(v) >= _MIN_SECRET_LENGTH:
            out = out.replace(v, MASK).replace(v.replace("\n", "\\n"), MASK)  # also the escaped form seen in JSON and TOML
    out = _PATTERNS[0].sub(MASK, out)
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(4)}", out)
    out = _PATTERNS[2].sub(MASK, out)
    out = _PATTERNS[3].sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _PATTERNS[4].sub(lambda m: f"{m.group(1)}{MASK}", out)
    return out


def safe_error(exc: BaseException, secrets: Iterable[object] = (), limit: int = 400) -> str:
    """'ErrorType: message' with secrets removed, cut to a sensible length. For screens and for the Run_Log."""
    text = redact(f"{type(exc).__name__}: {exc}", secrets)
    return text if len(text) <= limit else text[: limit - 1] + "…"
