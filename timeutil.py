"""Time handling: store and compare in UTC, show and accept local time.

Every datetime the app creates is timezone-aware UTC (utc_now()). The
models' datetime fields use SQLModel's UTCDateTime, which stores UTC and
reads naive values (SQLite, or Postgres rows written before the timestamptz
migration in database.py) back as UTC.

Teachers and students see, and teachers type, APP_TIMEZONE wall-clock time
(default America/Guayaquil, UTC-5 all year). A deadline typed as 23:59 is
23:59 in Ecuador, stored as 04:59 UTC the next day.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "America/Guayaquil"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Aware UTC; a naive value is taken to already be UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    dt = as_utc(dt)
    return dt.astimezone(APP_TIMEZONE) if dt else None


def parse_local(value: str) -> Optional[datetime]:
    """A wall-clock time typed in APP_TIMEZONE (e.g. an <input
    type="datetime-local"> value "2026-10-01T23:59") as aware UTC. A value
    that carries its own offset keeps it. Blank -> None; bad -> ValueError."""
    value = (value or "").strip()
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TIMEZONE)
    return dt.astimezone(timezone.utc)


def format_local(dt: Optional[datetime], fmt: str = "%d %b %Y %H:%M") -> str:
    """Jinja filter `local`: a stored (UTC) datetime in APP_TIMEZONE."""
    local = to_local(dt)
    return local.strftime(fmt) if local else ""


def timezone_label() -> str:
    """e.g. "America/Guayaquil, UTC-05:00", shown next to deadline inputs."""
    offset = datetime.now(APP_TIMEZONE).strftime("%z")
    return f"{APP_TIMEZONE.key}, UTC{offset[:3]}:{offset[3:]}"
