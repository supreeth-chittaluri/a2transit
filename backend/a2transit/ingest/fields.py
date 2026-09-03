"""Parsers for GTFS field types.

The one that matters is `parse_gtfs_time`. GTFS times are *not* clock times:
they count from noon minus twelve hours on the service day, so a trip that
starts Sunday evening and finishes after midnight keeps counting upward rather
than wrapping. Both Ann Arbor feeds exercise this — TheRide reaches 24:42:00 and
MBus 27:15:00 (7,263 of its stop_times rows are past midnight).

Every value here is stored as an integer count of seconds. Nothing in this
codebase should convert a GTFS time to a wall clock without also knowing the
service date, because 27:15:00 on Saturday's service is 03:15 on Sunday.
"""

from __future__ import annotations

import datetime as dt
import re

# HH may be one or more digits and may exceed 23. MM and SS are always two.
# Seconds are optional: the spec requires HH:MM:SS, but HH:MM appears in the
# wild often enough that rejecting it would be pedantry rather than safety.
_TIME_PATTERN = re.compile(r"^(\d{1,3}):([0-5]\d)(?::([0-5]\d))?$")

_DATE_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


class GtfsFieldError(ValueError):
    """A field could not be parsed. Carries the field name and offending value."""

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field = field
        self.value = value
        super().__init__(f"{field}: {reason} (got {value!r})")


def parse_gtfs_time(value: str | None, *, field: str = "time") -> int | None:
    """Parse "HH:MM:SS" into seconds since service midnight.

    Hours are allowed to exceed 23 and are not wrapped:

        >>> parse_gtfs_time("06:02:00")
        21720
        >>> parse_gtfs_time("24:42:00")   # TheRide's latest
        88920
        >>> parse_gtfs_time("27:15:00")   # MBus's latest
        98100

    Blank means "no time given at all", which GTFS allows for stops a vehicle
    passes without a scheduled time, so it maps to None rather than an error.
    """
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    match = _TIME_PATTERN.match(text)
    if match is None:
        raise GtfsFieldError(field, value, "not a valid GTFS time (expected HH:MM:SS)")

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds or 0)


def format_gtfs_time(seconds: int | None) -> str | None:
    """Inverse of `parse_gtfs_time`, keeping hours past 24 rather than wrapping.

        >>> format_gtfs_time(98100)
        '27:15:00'
    """
    if seconds is None:
        return None
    if seconds < 0:
        raise GtfsFieldError("time", seconds, "seconds may not be negative")

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_gtfs_date(value: str | None, *, field: str = "date") -> dt.date | None:
    """Parse GTFS's YYYYMMDD.

        >>> parse_gtfs_date("20260823")
        datetime.date(2026, 8, 23)
    """
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    match = _DATE_PATTERN.match(text)
    if match is None:
        raise GtfsFieldError(field, value, "not a valid GTFS date (expected YYYYMMDD)")

    year, month, day = (int(part) for part in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise GtfsFieldError(field, value, str(exc)) from exc


def parse_int(value: str | None, *, field: str = "int") -> int | None:
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return int(text)
    except ValueError as exc:
        raise GtfsFieldError(field, value, "not an integer") from exc


def parse_float(value: str | None, *, field: str = "float") -> float | None:
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError as exc:
        raise GtfsFieldError(field, value, "not a number") from exc


def parse_bool(value: str | None, *, field: str = "bool") -> bool | None:
    """GTFS booleans are the strings "0" and "1" (calendar day columns)."""
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None
    if text in ("0", "1"):
        return text == "1"

    raise GtfsFieldError(field, value, 'expected "0" or "1"')


def parse_text(value: str | None) -> str | None:
    """Strip, and treat an empty string as absent.

    GTFS uses empty strings for missing optional values throughout. Keeping them
    as "" would make `stop_desc = ''` and `stop_desc IS NULL` two different
    states meaning the same thing.
    """
    if value is None:
        return None

    text = value.strip()
    return text or None


def require_text(value: str | None, *, field: str) -> str:
    """For fields GTFS marks required — a missing one is a corrupt feed."""
    text = parse_text(value)
    if text is None:
        raise GtfsFieldError(field, value, "required field is empty")
    return text
