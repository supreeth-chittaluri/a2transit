"""Which services run on a given date.

This is the part of M2 most likely to be quietly wrong, and it matters most for
MBus. That feed overlays several service_ids onto each weekday in calendar.txt
and then removes most of them again through calendar_dates.txt, so reading
calendar.txt alone does not merely miss a holiday — it inflates ordinary
weekdays:

    Thu 2026-09-10   calendar only 3,620 trips   correct 1,668   (2.2x over)
    Mon 2026-09-07   calendar only 3,490 trips   correct   366   (9.5x over)
                     (Labor Day)

MBus publishes 289 removals and 25 additions across 133 dates; TheRide publishes
4 removals in total. A planner that skips exceptions routes people onto buses
that are not running, and does it on ordinary Tuesdays rather than only on
holidays where someone might notice.

The resolution rule, from the GTFS spec:

    active(date) = (scheduled_by_calendar(date) OR added_by_exception(date))
                   AND NOT removed_by_exception(date)

An addition applies even outside the calendar's start/end window, which is how a
feed can define a service entirely through exceptions with no calendar row.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from a2transit.db.models import AgencySource

#: GTFS calendar.txt column order, Monday first, matching date.weekday().
_WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

EXCEPTION_ADDED = 1
EXCEPTION_REMOVED = 2


@dataclass(frozen=True)
class ServiceWindow:
    """One calendar.txt row: which weekdays a service runs, over what span."""

    service_id: str
    weekdays: tuple[bool, bool, bool, bool, bool, bool, bool]
    start_date: dt.date
    end_date: dt.date

    def runs_on(self, day: dt.date) -> bool:
        if not (self.start_date <= day <= self.end_date):
            return False
        return self.weekdays[day.weekday()]


@dataclass(frozen=True)
class ServiceException:
    """One calendar_dates.txt row."""

    service_id: str
    date: dt.date
    exception_type: int


def resolve_active_services(
    windows: Iterable[ServiceWindow],
    exceptions: Iterable[ServiceException],
    day: dt.date,
) -> frozenset[str]:
    """Service ids running on `day`. Pure function — the DB never enters here."""
    active = {window.service_id for window in windows if window.runs_on(day)}

    added: set[str] = set()
    removed: set[str] = set()
    for exception in exceptions:
        if exception.date != day:
            continue
        if exception.exception_type == EXCEPTION_ADDED:
            added.add(exception.service_id)
        elif exception.exception_type == EXCEPTION_REMOVED:
            removed.add(exception.service_id)

    # Removal wins if a feed ever says both for one service on one date. The
    # spec does not define that case; refusing to run is the safe reading.
    return frozenset((active | added) - removed)


def load_service_windows(
    connection: Connection, agency: AgencySource
) -> tuple[ServiceWindow, ...]:
    rows = connection.execute(
        text(
            f"SELECT service_id, {', '.join(_WEEKDAY_COLUMNS)}, start_date, end_date "  # noqa: S608
            "FROM calendar WHERE agency_source = :agency"
        ),
        {"agency": agency.value},
    ).all()
    return tuple(
        ServiceWindow(
            service_id=row.service_id,
            weekdays=tuple(getattr(row, column) for column in _WEEKDAY_COLUMNS),  # type: ignore[arg-type]
            start_date=row.start_date,
            end_date=row.end_date,
        )
        for row in rows
    )


def load_service_exceptions(
    connection: Connection, agency: AgencySource
) -> tuple[ServiceException, ...]:
    rows = connection.execute(
        text(
            "SELECT service_id, date, exception_type FROM calendar_dates "
            "WHERE agency_source = :agency"
        ),
        {"agency": agency.value},
    ).all()
    return tuple(
        ServiceException(
            service_id=row.service_id, date=row.date, exception_type=row.exception_type
        )
        for row in rows
    )


@dataclass(frozen=True)
class AgencyCalendar:
    """Everything needed to answer "what runs on date D" for one agency."""

    agency: AgencySource
    windows: tuple[ServiceWindow, ...]
    exceptions: tuple[ServiceException, ...]

    def active_on(self, day: dt.date) -> frozenset[str]:
        return resolve_active_services(self.windows, self.exceptions, day)


def load_agency_calendar(engine: Engine, agency: AgencySource) -> AgencyCalendar:
    with engine.connect() as connection:
        return AgencyCalendar(
            agency=agency,
            windows=load_service_windows(connection, agency),
            exceptions=load_service_exceptions(connection, agency),
        )


def load_calendars(
    engine: Engine, agencies: Sequence[AgencySource] | None = None
) -> dict[AgencySource, AgencyCalendar]:
    agencies = agencies if agencies is not None else tuple(AgencySource)
    with engine.connect() as connection:
        return {
            agency: AgencyCalendar(
                agency=agency,
                windows=load_service_windows(connection, agency),
                exceptions=load_service_exceptions(connection, agency),
            )
            for agency in agencies
        }
