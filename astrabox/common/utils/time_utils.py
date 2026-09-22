from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def plus_seconds_iso(seconds: int) -> str:
    return (utcnow() + timedelta(seconds=seconds)).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO timestamp and return the same instant in UTC."""
    return parse_iso(s).astimezone(timezone.utc)
