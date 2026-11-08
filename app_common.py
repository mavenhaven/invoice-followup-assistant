"""Small helpers shared by the Streamlit pages. Everything Streamlit-specific lives here and in pages/."""
from __future__ import annotations

import datetime as dt
import hmac
import re
import time
from typing import Any

import pandas as pd
import streamlit as st

from core.clock import DEFAULT_TIMEZONE, today_in
from core.redact import safe_error
from core.schema import Config
from core.sheets import SheetStore, connect

SETUP_HELP = """
**The app is not connected to a Google Sheet yet.** Add these to the app's secrets
(Streamlit Community Cloud: *Settings > Secrets*; locally: `.streamlit/secrets.toml`):

```toml
sheet_id = "the long id from the sheet's URL"
[gcp_service_account]
# the contents of your service account JSON key
```

See `.streamlit/secrets.toml.example`. The sheet must be shared with the service account's
`client_email` as **Editor**.
"""


@st.cache_resource(show_spinner="Connecting to Google Sheets...")
def _connect() -> SheetStore:
    return connect(dict(st.secrets["gcp_service_account"]), st.secrets["sheet_id"],
                   invoices_tab=st.secrets.get("invoices_tab"))


def secret_values() -> list[str]:
    """Values from the app's secrets that must never appear on screen or in the sheet."""
    out: list[str] = []
    try:
        account = dict(st.secrets.get("gcp_service_account", {}))
        out += [str(account.get(k, "")) for k in ("private_key", "private_key_id", "client_id")]
        out += [str(st.secrets.get("sheet_id", "")), str(st.secrets.get("app_password", ""))]
    except Exception:  # noqa: BLE001 - no secrets file at all
        pass
    return [v for v in out if v]


def esc(text: object) -> str:
    """Make text from the sheet safe to show through Streamlit's markdown (no links, formatting or $math$)."""
    return re.sub(r"([\\`*_{}\[\]()#+!|<>~$:])", r"\\\1", str(text))


def _app_password() -> str:
    try:
        return str(st.secrets.get("app_password", "") or "")
    except Exception:  # noqa: BLE001 - no secrets file
        return ""


def access_ok() -> bool:
    """True when no app password is set, or this session has entered it."""
    return not _app_password() or bool(st.session_state.get("access_granted"))


def require_access() -> None:
    """If the secrets contain app_password, ask for it before showing anything and stop until it is right."""
    expected = _app_password()
    if not expected or st.session_state.get("access_granted"):
        return
    st.title("Invoice Follow-Up Assistant")
    st.write("This app is protected. Enter the access password to continue.")
    with st.form("access_form"):
        typed = st.text_input("Password", type="password", key="access_password")
        if st.form_submit_button("Continue", type="primary"):
            if hmac.compare_digest(typed.encode(), expected.encode()):
                st.session_state["access_granted"] = True
                st.session_state.pop("access_password", None)
                st.rerun()
            time.sleep(1)  # slows down guessing
            st.error("That password is not right.")
    st.stop()


def show_error(message: str, exc: BaseException | None = None, *, stop: bool = False) -> None:
    """An error box with a plain message, and the technical detail (secrets removed) behind an expander."""
    st.error(esc(message))
    if exc is not None:
        with st.expander("Technical detail"):
            st.code(safe_error(exc, secret_values()))
    if stop:
        st.stop()


def get_store() -> SheetStore:
    """The sheet connection, with the layout checked now and then (tabs and columns are created if missing).

    Asks for the app password first when one is set. Tests put a store in session_state['store_override'].
    """
    require_access()
    try:
        store = st.session_state.get("store_override") or _connect()
        store.ensure_schema_once()  # at most every 10 minutes for the whole app; makes no writes when nothing is missing
        return store
    except Exception as exc:  # noqa: BLE001 - show setup help instead of a stack trace
        st.error("Could not connect to the Google Sheet.")
        st.markdown(SETUP_HELP)
        with st.expander("Technical detail"):
            st.code(safe_error(exc, secret_values()))
        st.stop()
        raise  # unreachable; keeps type checkers happy


def signed_in_email() -> str | None:
    """The signed-in user's email, only when the app really has sign-in and the user is logged in.

    Some Streamlit setups return a stand-in address (for example when run locally), so is_logged_in
    must be true before the email is trusted.
    """
    try:
        if getattr(st.user, "is_logged_in", False) and getattr(st.user, "email", None):
            return str(st.user.email)
    except Exception:  # noqa: BLE001 - st.user is unavailable when the app has no sign-in
        pass
    return None


def current_actor() -> str:
    """Who is acting: the signed-in user's email, otherwise the name typed in the sidebar."""
    return signed_in_email() or str(st.session_state.get("actor_name", "")).strip()


def sidebar() -> str:
    """Render the sidebar identity box and return the actor's name."""
    with st.sidebar:
        st.subheader("Who is working?")
        signed_in = signed_in_email()
        if signed_in:
            st.write(f"Signed in as **{signed_in}**")
        else:
            st.text_input("Your name", key="actor_name", help="Recorded in the audit log next to every decision you make.")
    return current_actor()


def run_date(config: Config | None = None) -> dt.date:
    """The date the Run page is set to (a simulated date lets you test several days).

    Until someone picks a date, it is today in the time zone from the Config sheet, not the server's clock.
    """
    return st.session_state.get("run_date") or today_in(config.timezone if config else DEFAULT_TIMEZONE)


def data_version() -> int:
    return int(st.session_state.get("data_version", 0))


def bump_version() -> None:
    """Make the next page load read the sheet again."""
    st.session_state["data_version"] = data_version() + 1


@st.cache_data(ttl=30, show_spinner="Reading the sheet...")
def read_all(_store: SheetStore, version: int) -> dict[str, Any]:
    """Invoices, audit log, run log and config in one go. Cached for 30 seconds or until the version changes."""
    return {"invoices": _store.read_invoices(), "audit": _store.read_audit_log(), "runs": _store.read_run_log(),
            "config": _store.load_config()}


def try_load_config() -> Config | None:
    """The live Config sheet values, or None when the app is not connected. Never stops the page (used by About)."""
    if not access_ok():
        return None  # nothing is read from the sheet for someone who has not entered the password
    try:
        store = st.session_state.get("store_override") or _connect()
        return read_all(store, data_version())["config"]
    except Exception:  # noqa: BLE001 - About must work before the sheet is set up
        return None


def load_data(store: SheetStore) -> dict[str, Any]:
    """read_all(), but a failure (a missing tab, Google being busy) becomes a clear message instead of a stack trace."""
    try:
        return read_all(store, data_version())
    except Exception as exc:  # noqa: BLE001
        show_error("Could not read the Google Sheet. Check that its tabs are still there and that it is shared with the "
                   "service account, then press Refresh or reload the page. If Google is busy, wait a minute and try again.", exc, stop=True)
        raise  # unreachable


def flash(kind: str, message: str) -> None:
    """Show a message on the next page load (after an action that reruns the page)."""
    st.session_state.setdefault("flash", []).append((kind, message))


def show_flash() -> None:
    for kind, message in st.session_state.pop("flash", []):
        {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


def empty_frame_note(df: pd.DataFrame, text: str) -> bool:
    """Show a friendly note and return True when there is nothing to display."""
    if df is None or df.empty:
        st.info(text)
        return True
    return False


__all__ = ["Config", "access_ok", "bump_version", "esc", "load_data", "require_access", "show_error", "current_actor", "data_version", "empty_frame_note", "flash", "get_store", "read_all",
           "run_date", "show_flash", "sidebar"]
