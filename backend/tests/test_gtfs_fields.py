"""GTFS field parsing.

`parse_gtfs_time` gets the most attention here because it is the place a silent
error does the most damage. Every other bug in the ingest produces a missing row
or a hard failure; mis-parsing a time past midnight produces a complete,
plausible schedule that is wrong by 24 hours.
"""

from __future__ import annotations

import datetime as dt

import pytest

from a2transit.ingest.fields import (
    GtfsFieldError,
    format_gtfs_time,
    parse_bool,
    parse_float,
    parse_gtfs_date,
    parse_gtfs_time,
    parse_int,
    parse_text,
    require_text,
)


class TestParseGtfsTime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("00:00:00", 0),
            ("06:02:00", 21720),  # earliest weekday departure on TheRide route 4
            ("12:00:00", 43200),
            ("23:59:59", 86399),
        ],
    )
    def test_ordinary_times(self, value: str, expected: int) -> None:
        assert parse_gtfs_time(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("24:00:00", 86400),
            ("24:42:00", 88920),  # TheRide's latest in the current feed
            ("25:30:00", 91800),
            ("27:15:00", 98100),  # MBus's latest in the current feed
        ],
    )
    def test_times_past_midnight_are_not_wrapped(self, value: str, expected: int) -> None:
        """The whole reason these are integers and not SQL `time` values."""
        assert parse_gtfs_time(value) == expected

    def test_hours_past_midnight_stay_ordered_against_earlier_times(self) -> None:
        """A 00:15 wrap would sort a post-midnight trip before its own start."""
        trip_start = parse_gtfs_time("23:50:00")
        trip_end = parse_gtfs_time("24:15:00")

        assert trip_end > trip_start
        assert trip_end - trip_start == 25 * 60

    def test_single_digit_hour_is_accepted(self) -> None:
        assert parse_gtfs_time("6:02:00") == 21720

    def test_seconds_are_optional(self) -> None:
        assert parse_gtfs_time("06:02") == 21720

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert parse_gtfs_time("  06:02:00  ") == 21720

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_time_is_none_not_zero(self, value: str | None) -> None:
        """GTFS allows a blank time at a non-timepoint stop. Zero would mean midnight."""
        assert parse_gtfs_time(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "abc",
            "06:60:00",  # minutes out of range
            "06:02:60",  # seconds out of range
            "-01:00:00",
            "06",
            "06:2:00",  # minutes must be two digits
            "1:2:3",
            "06:02:00.5",
        ],
    )
    def test_malformed_values_raise(self, value: str) -> None:
        with pytest.raises(GtfsFieldError):
            parse_gtfs_time(value)

    def test_error_names_the_field_and_value(self) -> None:
        with pytest.raises(GtfsFieldError) as excinfo:
            parse_gtfs_time("nonsense", field="arrival_time")

        assert excinfo.value.field == "arrival_time"
        assert excinfo.value.value == "nonsense"
        assert "arrival_time" in str(excinfo.value)


class TestFormatGtfsTime:
    @pytest.mark.parametrize("value", ["00:00:00", "06:02:00", "23:59:59", "24:42:00", "27:15:00"])
    def test_round_trips(self, value: str) -> None:
        assert format_gtfs_time(parse_gtfs_time(value)) == value

    def test_none_round_trips(self) -> None:
        assert format_gtfs_time(None) is None

    def test_negative_seconds_raise(self) -> None:
        with pytest.raises(GtfsFieldError):
            format_gtfs_time(-1)


class TestParseGtfsDate:
    def test_parses_feed_start_date(self) -> None:
        assert parse_gtfs_date("20260823") == dt.date(2026, 8, 23)

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_absent_date_is_none(self, value: str | None) -> None:
        assert parse_gtfs_date(value) is None

    @pytest.mark.parametrize(
        "value",
        ["2026-08-23", "20260832", "20261301", "202608", "abcdefgh"],
    )
    def test_malformed_values_raise(self, value: str) -> None:
        with pytest.raises(GtfsFieldError):
            parse_gtfs_date(value)


class TestScalarParsers:
    @pytest.mark.parametrize(("value", "expected"), [("0", 0), ("3", 3), ("-1", -1), ("", None)])
    def test_parse_int(self, value: str, expected: int | None) -> None:
        assert parse_int(value) == expected

    def test_parse_int_rejects_non_numeric(self) -> None:
        with pytest.raises(GtfsFieldError):
            parse_int("3.5", field="route_type")

    @pytest.mark.parametrize(
        ("value", "expected"), [("42.28", 42.28), ("-83.74", -83.74), ("", None)]
    )
    def test_parse_float(self, value: str, expected: float | None) -> None:
        assert parse_float(value) == expected

    @pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False), ("", None)])
    def test_parse_bool(self, value: str, expected: bool | None) -> None:
        assert parse_bool(value) is expected

    @pytest.mark.parametrize("value", ["true", "yes", "2", "-1"])
    def test_parse_bool_rejects_anything_else(self, value: str) -> None:
        with pytest.raises(GtfsFieldError):
            parse_bool(value, field="monday")

    def test_parse_text_maps_empty_to_none(self) -> None:
        """So `IS NULL` and `= ''` are not two spellings of the same absence."""
        assert parse_text("") is None
        assert parse_text("   ") is None
        assert parse_text("  Blake Transit Center ") == "Blake Transit Center"

    def test_require_text_rejects_empty(self) -> None:
        with pytest.raises(GtfsFieldError):
            require_text("", field="stop_name")

    def test_require_text_returns_stripped_value(self) -> None:
        assert require_text(" YTC - Stop 2 ", field="stop_name") == "YTC - Stop 2"
