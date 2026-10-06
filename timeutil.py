"""Time helpers. Naive-UTC datetimes everywhere, matching how SQLite stores them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_plus(**kwargs) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).replace(tzinfo=None)


def parse_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601 datetime or ``YYYY-MM-DD``; return None when invalid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
