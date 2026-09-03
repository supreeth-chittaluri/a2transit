"""Service-date resolution.

Split in two: pure-function tests of the resolution rule, and db-marked tests
against the real feeds where the numbers were measured by hand before this
module existed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.routing.service_calendar import (
    AgencyCalendar,
    ServiceException,
    ServiceWindow,
    load_agency_calendar,
    resolve_active_services,
)
from tests.conftest import DATA_DIR

WEEKDAYS = (True, True, True, True, True, False, False)
WEEKENDS = (False, False, False, False, False, True, True)
EVERY_DAY = (True,) * 7

SEPT = dt.date(2026, 9, 1)  # a Tuesday
TERM = (dt.date(2026, 8, 23), dt.date(2027, 1, 30))


def _window(service_id: str, weekdays: tuple[bool, ...] = WEEKDAYS) -> ServiceWindow:
    return ServiceWindow(service_id, weekdays, *TERM)  # type: ignore[arg-type]


class TestResolutionRule:
    def test_weekday_service_runs_on_a_weekday(self) -> None:
        assert resolve_active_services([_window("3")], [], SEPT) == {"3"}

    def test_weekday_service_does_not_run_at_the_weekend(self) -> None:
        saturday = dt.date(2026, 9, 5)

        assert resolve_active_services([_window("3")], [], saturday) == frozenset()

    def test_date_outside_the_calendar_window_is_inactive(self) -> None:
        before_term = dt.date(2026, 8, 1)

        assert resolve_active_services([_window("3")], [], before_term) == frozenset()

    def test_removal_exception_overrides_the_calendar(self) -> None:
        """Labor Day: the calendar says Monday, the exception says no."""
        labor_day = dt.date(2026, 9, 7)
        exceptions = [ServiceException("3", labor_day, 2)]

        assert resolve_active_services([_window("3")], exceptions, labor_day) == frozenset()
        # The next Monday is unaffected.
        assert resolve_active_services([_window("3")], exceptions, dt.date(2026, 9, 14)) == {"3"}

    def test_addition_exception_adds_service_the_calendar_excludes(self) -> None:
        sunday = dt.date(2026, 9, 6)
        exceptions = [ServiceException("3", sunday, 1)]

        assert resolve_active_services([_window("3")], exceptions, sunday) == {"3"}

    def test_addition_applies_outside_the_calendar_window(self) -> None:
        """GTFS allows a service defined only by exceptions, with no calendar row."""
        outside = dt.date(2027, 6, 1)
        exceptions = [ServiceException("special", outside, 1)]

        assert resolve_active_services([], exceptions, outside) == {"special"}

    def test_removal_wins_when_a_feed_says_both(self) -> None:
        """Undefined in the spec; refusing to run is the safe reading."""
        exceptions = [ServiceException("3", SEPT, 1), ServiceException("3", SEPT, 2)]

        assert resolve_active_services([_window("3")], exceptions, SEPT) == frozenset()

    def test_exceptions_for_other_dates_are_ignored(self) -> None:
        exceptions = [ServiceException("3", dt.date(2026, 9, 8), 2)]

        assert resolve_active_services([_window("3")], exceptions, SEPT) == {"3"}

    def test_several_services_resolve_independently(self) -> None:
        windows = [_window("weekday"), _window("weekend", WEEKENDS), _window("daily", EVERY_DAY)]
        exceptions = [ServiceException("daily", SEPT, 2)]

        assert resolve_active_services(windows, exceptions, SEPT) == {"weekday"}


@pytest.fixture(scope="module")
def calendars(db_engine: Engine) -> dict[AgencySource, AgencyCalendar]:
    loaded = {}
    for agency, filename in (
        (AgencySource.THERIDE, "theride.zip"),
        (AgencySource.MBUS, "mbus.zip"),
    ):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)
        loaded[agency] = load_agency_calendar(db_engine, agency)
    return loaded


def _trip_count(engine: Engine, agency: AgencySource, services: frozenset[str]) -> int:
    if not services:
        return 0
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM trips "
                "WHERE agency_source = :agency AND service_id = ANY(:services)"
            ),
            {"agency": agency.value, "services": list(services)},
        ).scalar_one()


@pytest.mark.db
class TestAgainstRealFeeds:
    """Counts here were read out of the feeds by hand before this module existed."""

    def test_theride_weekday_is_service_3(
        self, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        thursday = dt.date(2026, 9, 10)

        assert calendars[AgencySource.THERIDE].active_on(thursday) == {"3"}

    def test_theride_weekend_services(
        self, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        assert calendars[AgencySource.THERIDE].active_on(dt.date(2026, 9, 5)) == {"1"}  # Sat
        assert calendars[AgencySource.THERIDE].active_on(dt.date(2026, 9, 6)) == {"2"}  # Sun

    def test_mbus_ordinary_thursday_is_1668_trips_not_3620(
        self, db_engine: Engine, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        """Exceptions cut an ordinary weekday by more than half, not just holidays."""
        thursday = dt.date(2026, 9, 10)
        services = calendars[AgencySource.MBUS].active_on(thursday)

        assert _trip_count(db_engine, AgencySource.MBUS, services) == 1668

    def test_mbus_labor_day_collapses_to_366_trips(
        self, db_engine: Engine, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        labor_day = dt.date(2026, 9, 7)
        services = calendars[AgencySource.MBUS].active_on(labor_day)

        assert _trip_count(db_engine, AgencySource.MBUS, services) == 366

    def test_ignoring_exceptions_would_change_the_answer(
        self, db_engine: Engine, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        """Pins the regression this module exists to prevent."""
        thursday = dt.date(2026, 9, 10)
        calendar = calendars[AgencySource.MBUS]

        correct = calendar.active_on(thursday)
        without_exceptions = resolve_active_services(calendar.windows, [], thursday)

        assert without_exceptions > correct
        assert _trip_count(db_engine, AgencySource.MBUS, without_exceptions) == 3620

    def test_every_date_in_the_feed_window_resolves(
        self, calendars: dict[AgencySource, AgencyCalendar]
    ) -> None:
        """No date in service should come back with nothing running at all."""
        day = dt.date(2026, 8, 24)
        end = dt.date(2026, 12, 31)
        empty_days = []
        while day <= end:
            if not calendars[AgencySource.THERIDE].active_on(day):
                empty_days.append(day)
            day += dt.timedelta(days=1)

        # TheRide publishes 4 removals; those are the only days without service.
        assert len(empty_days) <= 4
