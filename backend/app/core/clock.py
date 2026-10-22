"""Time helpers. Everything is stored UTC; SQLite (tests) returns naive datetimes, so normalise."""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def iso_z(value: dt.datetime) -> str:
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[union-attr]
