"""UTC time helpers.

All timestamps are written in UTC. SQLite has no native timezone-aware datetime
type, so values may come back naive even from a DateTime(timezone=True) column.
`as_utc` restores that lost awareness rather than letting a naive value be
interpreted as local time somewhere downstream.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Return `value` as timezone-aware UTC.

    A naive value is assumed to already be UTC, which holds because every write
    path in this application uses `utcnow()`. An aware value is converted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso_utc(value: datetime) -> str:
    """Serialize as an explicitly UTC ISO-8601 string ending in `Z`."""
    return as_utc(value).isoformat().replace("+00:00", "Z")
