"""SQLAlchemy models mirroring GTFS, with both agencies in one set of tables.

Every table carries `agency_source` as the leading primary-key column. This is
not defensive over-engineering — the two feeds genuinely collide:

    stop_id "161"     TheRide "Tyler + Zephyr"  vs  MBus "TEST STOP 1"  (14.9 km apart)
    service_id "3"    TheRide Mon-Fri           vs  MBus Monday only
    trip_id  800 values shared, none of them the same trip

The stop and trip collisions are loud if you get them wrong. The service_id one
is not: joining a TheRide trip to MBus calendar row "3" yields a *plausible*
schedule that is simply wrong. Composite keys everywhere is the only way to make
that class of bug impossible rather than merely unlikely.

Schema is created with `Base.metadata.create_all` (see a2transit.db.schema).
There is no migration tool yet; the feeds are reloaded wholesale on every
refresh, so there is no data to migrate. Revisit at M8.
"""

from __future__ import annotations

import datetime as dt
import enum

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AgencySource(enum.StrEnum):
    """Which feed a row came from. The leading component of every primary key."""

    THERIDE = "theride"
    MBUS = "mbus"


# native_enum with an explicit name so the PG type is `agency_source`, and
# values_callable so the *values* ("theride") are stored rather than the Python
# member names ("THERIDE") — the values are what appears in API responses.
AgencySourceEnum = Enum(
    AgencySource,
    name="agency_source",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Base(DeclarativeBase):
    pass


def _agency_source_pk() -> Mapped[AgencySource]:
    return mapped_column(AgencySourceEnum, primary_key=True)


class Agency(Base):
    __tablename__ = "agencies"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    agency_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    agency_name: Mapped[str] = mapped_column(Text)
    agency_url: Mapped[str | None] = mapped_column(Text)
    agency_timezone: Mapped[str] = mapped_column(String(64))
    agency_lang: Mapped[str | None] = mapped_column(String(16))
    agency_phone: Mapped[str | None] = mapped_column(String(64))
    agency_fare_url: Mapped[str | None] = mapped_column(Text)
    agency_email: Mapped[str | None] = mapped_column(Text)


class Stop(Base):
    __tablename__ = "stops"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    stop_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    stop_code: Mapped[str | None] = mapped_column(String(64))
    stop_name: Mapped[str] = mapped_column(Text)
    stop_desc: Mapped[str | None] = mapped_column(Text)
    stop_lat: Mapped[float] = mapped_column(Float)
    stop_lon: Mapped[float] = mapped_column(Float)
    zone_id: Mapped[str | None] = mapped_column(String(64))
    stop_url: Mapped[str | None] = mapped_column(Text)
    location_type: Mapped[int | None] = mapped_column(SmallInteger)
    parent_station: Mapped[str | None] = mapped_column(String(64))
    stop_timezone: Mapped[str | None] = mapped_column(String(64))
    wheelchair_boarding: Mapped[int | None] = mapped_column(SmallInteger)

    # Populated at load from stop_lat/stop_lon. Geography (not geometry) so
    # ST_DWithin takes metres directly — M4 generates footpaths at ~400 m and
    # projecting by hand for every pair would be needless work and needless error.
    geog: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        # The index M4's cross-agency footpath generation runs on.
        Index("ix_stops_geog", "geog", postgresql_using="gist"),
        # Trigram index for /stops/search autocomplete in M5.
        Index(
            "ix_stops_name_trgm",
            "stop_name",
            postgresql_using="gin",
            postgresql_ops={"stop_name": "gin_trgm_ops"},
        ),
    )


class Route(Base):
    __tablename__ = "routes"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    route_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    agency_id: Mapped[str | None] = mapped_column(String(64))
    route_short_name: Mapped[str | None] = mapped_column(String(64))
    route_long_name: Mapped[str | None] = mapped_column(Text)
    route_desc: Mapped[str | None] = mapped_column(Text)
    route_type: Mapped[int] = mapped_column(SmallInteger)
    route_url: Mapped[str | None] = mapped_column(Text)
    route_color: Mapped[str | None] = mapped_column(String(8))
    route_text_color: Mapped[str | None] = mapped_column(String(8))

    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_source", "agency_id"],
            ["agencies.agency_source", "agencies.agency_id"],
        ),
        Index("ix_routes_short_name", "agency_source", "route_short_name"),
    )


class Calendar(Base):
    __tablename__ = "calendar"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    monday: Mapped[bool] = mapped_column(Boolean)
    tuesday: Mapped[bool] = mapped_column(Boolean)
    wednesday: Mapped[bool] = mapped_column(Boolean)
    thursday: Mapped[bool] = mapped_column(Boolean)
    friday: Mapped[bool] = mapped_column(Boolean)
    saturday: Mapped[bool] = mapped_column(Boolean)
    sunday: Mapped[bool] = mapped_column(Boolean)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)


class CalendarDate(Base):
    """Service exceptions. exception_type 1 = added, 2 = removed."""

    __tablename__ = "calendar_dates"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    exception_type: Mapped[int] = mapped_column(SmallInteger)


class Shape(Base):
    """Raw shape points. The drawable geometry lives in ShapeGeometry."""

    __tablename__ = "shapes"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    shape_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    shape_pt_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)

    shape_pt_lat: Mapped[float] = mapped_column(Float)
    shape_pt_lon: Mapped[float] = mapped_column(Float)
    shape_dist_traveled: Mapped[float | None] = mapped_column(Float)


class ShapeGeometry(Base):
    """One LineString per shape_id, derived from `shapes` at load time.

    Materialised during ingest rather than assembled per request: M6 draws a
    route's geometry on every plan, and rebuilding a 900-point LineString by
    sorting and aggregating raw points each time is work we can do once.
    """

    __tablename__ = "shape_geometries"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    shape_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False)
    )
    point_count: Mapped[int] = mapped_column(Integer)


class Trip(Base):
    __tablename__ = "trips"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    route_id: Mapped[str] = mapped_column(String(64))
    service_id: Mapped[str] = mapped_column(String(64))
    trip_headsign: Mapped[str | None] = mapped_column(Text)
    trip_short_name: Mapped[str | None] = mapped_column(Text)
    direction_id: Mapped[int | None] = mapped_column(SmallInteger)
    block_id: Mapped[str | None] = mapped_column(String(64))
    shape_id: Mapped[str | None] = mapped_column(String(64))
    wheelchair_accessible: Mapped[int | None] = mapped_column(SmallInteger)
    bikes_allowed: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_source", "route_id"],
            ["routes.agency_source", "routes.route_id"],
        ),
        # Deliberately no FK to calendar: GTFS permits a service_id defined only
        # by calendar_dates entries, with no calendar row at all. Neither feed
        # does that today, but a FK would turn that legal upstream change into a
        # hard ingest failure.
        Index("ix_trips_route", "agency_source", "route_id"),
        Index("ix_trips_service", "agency_source", "service_id"),
    )


class StopTime(Base):
    """The big table — 214,724 rows across both feeds.

    arrival_time and departure_time are INTEGER seconds since *service* midnight,
    not clock times. GTFS times legitimately exceed 24:00:00 for trips running
    past midnight: MBus reaches 27:15:00 and TheRide 24:42:00 in the current
    feeds. A SQL `time` column cannot hold those, and silently wrapping 27:15 to
    03:15 would place a bus 24 hours early in every routing query.
    """

    __tablename__ = "stop_times"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stop_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)

    arrival_time: Mapped[int | None] = mapped_column(Integer)
    departure_time: Mapped[int | None] = mapped_column(Integer)
    stop_id: Mapped[str] = mapped_column(String(64))
    stop_headsign: Mapped[str | None] = mapped_column(Text)
    pickup_type: Mapped[int | None] = mapped_column(SmallInteger)
    drop_off_type: Mapped[int | None] = mapped_column(SmallInteger)
    shape_dist_traveled: Mapped[float | None] = mapped_column(Float)
    timepoint: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_source", "trip_id"],
            ["trips.agency_source", "trips.trip_id"],
        ),
        ForeignKeyConstraint(
            ["agency_source", "stop_id"],
            ["stops.agency_source", "stops.stop_id"],
        ),
        # "What departs this stop, in time order" — the access pattern behind
        # both the RAPTOR stop scan (M3) and the departures board (M5).
        Index("ix_stop_times_stop_departure", "agency_source", "stop_id", "departure_time"),
    )


class Transfer(Base):
    """Agency-declared transfers. Distinct from the M4 PostGIS footpaths.

    TheRide publishes 17, all transfer_type 2 (a minimum transfer time is
    required); MBus publishes none. These are the agencies' own assertions and
    take precedence over generated footpaths where they overlap.
    """

    __tablename__ = "transfers"

    agency_source: Mapped[AgencySource] = _agency_source_pk()
    from_stop_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    to_stop_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    transfer_type: Mapped[int] = mapped_column(SmallInteger)
    min_transfer_time: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_source", "from_stop_id"],
            ["stops.agency_source", "stops.stop_id"],
        ),
        ForeignKeyConstraint(
            ["agency_source", "to_stop_id"],
            ["stops.agency_source", "stops.stop_id"],
        ),
    )


class FeedVersion(Base):
    """One row per successful ingest — the audit trail for the weekly refresh.

    The sha256 of the downloaded ZIP lets a refresh skip a byte-identical feed
    without reloading 200k rows, and row_counts gives a diff when something
    upstream changes shape.
    """

    __tablename__ = "feed_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agency_source: Mapped[AgencySource] = mapped_column(AgencySourceEnum)

    # feed_info.feed_version, when the feed publishes one.
    feed_version: Mapped[str | None] = mapped_column(String(128))
    feed_start_date: Mapped[dt.date | None] = mapped_column(Date)
    feed_end_date: Mapped[dt.date | None] = mapped_column(Date)

    source_url: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_length: Mapped[int] = mapped_column(BigInteger)
    # Upstream Last-Modified, which is how each agency dates a publication.
    last_modified: Mapped[str | None] = mapped_column(String(64))

    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    load_seconds: Mapped[float | None] = mapped_column(Float)
    row_counts: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_feed_versions_agency_loaded", "agency_source", "loaded_at"),)


# Child-to-parent order. Deletes walk this list front to back and loads walk it
# back to front, so foreign keys are satisfied in both directions. The ingest
# depends on this ordering being right — see a2transit.ingest.loader.
TABLES_IN_DEPENDENCY_ORDER: tuple[type[Base], ...] = (
    StopTime,
    Transfer,
    Trip,
    ShapeGeometry,
    Shape,
    CalendarDate,
    Calendar,
    Route,
    Stop,
    Agency,
)
