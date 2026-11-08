"""What 'today' means. The server (Streamlit Community Cloud) runs in UTC, but the team works in one time zone."""
from __future__ import annotations

import datetime as dt

DEFAULT_TIMEZONE = "Europe/Berlin"


def today_in(timezone: str = DEFAULT_TIMEZONE, now: dt.datetime | None = None) -> dt.date:
    """The calendar date in the given time zone. An unknown zone falls back to the machine's own date.

    ``now`` is for tests: an aware datetime to convert instead of the real clock.
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - unknown name, or no time zone database on this machine
        return (now or dt.datetime.now()).date()
    moment = now.astimezone(tz) if now is not None and now.tzinfo else dt.datetime.now(tz)
    return moment.date()


def now_in(timezone: str = DEFAULT_TIMEZONE) -> dt.datetime:
    """The current time in the given time zone as a plain (naive) datetime, to the second.

    Run_Log and Audit_Log timestamps use this, so everyone reading the sheet sees the team's local time
    whether the app runs on a laptop or on Streamlit Community Cloud (UTC).
    """
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(timezone)).replace(microsecond=0, tzinfo=None)
    except Exception:  # noqa: BLE001 - unknown name, or no time zone database on this machine
        return dt.datetime.now().replace(microsecond=0)
