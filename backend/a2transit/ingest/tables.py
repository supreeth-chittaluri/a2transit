"""Mapping from GTFS files to database rows.

One table per GTFS file, listed parents-first. The row factories return plain
tuples in column order because they feed straight into COPY — this is the hot
path for 214,724 stop_times rows, and building ORM instances for each would cost
far more than the parsing itself.

Both feeds ship identical column sets (both are Clever Devices exports), so
there is exactly one mapping rather than one per agency. `_get` still tolerates
a missing column so a future feed dropping an optional field degrades to NULL
instead of raising KeyError.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from a2transit.db.models import AgencySource
from a2transit.ingest.fields import (
    parse_bool,
    parse_float,
    parse_gtfs_date,
    parse_gtfs_time,
    parse_int,
    parse_text,
    require_text,
)

Row = Mapping[str, str | None]
RowFactory = Callable[[Row, AgencySource], tuple]


def _get(row: Row, column: str) -> str | None:
    return row.get(column)


@dataclass(frozen=True)
class TableSpec:
    """How one GTFS file becomes rows in one table."""

    gtfs_file: str
    table: str
    columns: tuple[str, ...]
    row_factory: RowFactory
    #: A feed missing a required file is corrupt; a missing optional file is fine.
    required: bool = True
    #: Rows the loader should drop rather than insert, by returning None.
    skip_if: Callable[[Row], bool] | None = field(default=None)


def _agency_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "agency_id"), field="agency_id"),
        require_text(_get(row, "agency_name"), field="agency_name"),
        parse_text(_get(row, "agency_url")),
        require_text(_get(row, "agency_timezone"), field="agency_timezone"),
        parse_text(_get(row, "agency_lang")),
        parse_text(_get(row, "agency_phone")),
        parse_text(_get(row, "agency_fare_url")),
        parse_text(_get(row, "agency_email")),
    )


def _stop_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "stop_id"), field="stop_id"),
        parse_text(_get(row, "stop_code")),
        require_text(_get(row, "stop_name"), field="stop_name"),
        parse_text(_get(row, "stop_desc")),
        parse_float(_get(row, "stop_lat"), field="stop_lat"),
        parse_float(_get(row, "stop_lon"), field="stop_lon"),
        parse_text(_get(row, "zone_id")),
        parse_text(_get(row, "stop_url")),
        parse_int(_get(row, "location_type"), field="location_type"),
        parse_text(_get(row, "parent_station")),
        parse_text(_get(row, "stop_timezone")),
        parse_int(_get(row, "wheelchair_boarding"), field="wheelchair_boarding"),
    )


def _route_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "route_id"), field="route_id"),
        parse_text(_get(row, "agency_id")),
        parse_text(_get(row, "route_short_name")),
        parse_text(_get(row, "route_long_name")),
        parse_text(_get(row, "route_desc")),
        parse_int(_get(row, "route_type"), field="route_type"),
        parse_text(_get(row, "route_url")),
        parse_text(_get(row, "route_color")),
        parse_text(_get(row, "route_text_color")),
    )


def _calendar_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "service_id"), field="service_id"),
        parse_bool(_get(row, "monday"), field="monday"),
        parse_bool(_get(row, "tuesday"), field="tuesday"),
        parse_bool(_get(row, "wednesday"), field="wednesday"),
        parse_bool(_get(row, "thursday"), field="thursday"),
        parse_bool(_get(row, "friday"), field="friday"),
        parse_bool(_get(row, "saturday"), field="saturday"),
        parse_bool(_get(row, "sunday"), field="sunday"),
        parse_gtfs_date(_get(row, "start_date"), field="start_date"),
        parse_gtfs_date(_get(row, "end_date"), field="end_date"),
    )


def _calendar_date_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "service_id"), field="service_id"),
        parse_gtfs_date(_get(row, "date"), field="date"),
        parse_int(_get(row, "exception_type"), field="exception_type"),
    )


def _shape_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "shape_id"), field="shape_id"),
        parse_int(_get(row, "shape_pt_sequence"), field="shape_pt_sequence"),
        parse_float(_get(row, "shape_pt_lat"), field="shape_pt_lat"),
        parse_float(_get(row, "shape_pt_lon"), field="shape_pt_lon"),
        parse_float(_get(row, "shape_dist_traveled"), field="shape_dist_traveled"),
    )


def _trip_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "trip_id"), field="trip_id"),
        require_text(_get(row, "route_id"), field="route_id"),
        require_text(_get(row, "service_id"), field="service_id"),
        parse_text(_get(row, "trip_headsign")),
        parse_text(_get(row, "trip_short_name")),
        parse_int(_get(row, "direction_id"), field="direction_id"),
        parse_text(_get(row, "block_id")),
        parse_text(_get(row, "shape_id")),
        parse_int(_get(row, "wheelchair_accessible"), field="wheelchair_accessible"),
        parse_int(_get(row, "bikes_allowed"), field="bikes_allowed"),
    )


def _stop_time_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "trip_id"), field="trip_id"),
        parse_int(_get(row, "stop_sequence"), field="stop_sequence"),
        parse_gtfs_time(_get(row, "arrival_time"), field="arrival_time"),
        parse_gtfs_time(_get(row, "departure_time"), field="departure_time"),
        require_text(_get(row, "stop_id"), field="stop_id"),
        parse_text(_get(row, "stop_headsign")),
        parse_int(_get(row, "pickup_type"), field="pickup_type"),
        parse_int(_get(row, "drop_off_type"), field="drop_off_type"),
        parse_float(_get(row, "shape_dist_traveled"), field="shape_dist_traveled"),
        parse_int(_get(row, "timepoint"), field="timepoint"),
    )


def _transfer_row(row: Row, source: AgencySource) -> tuple:
    return (
        source.value,
        require_text(_get(row, "from_stop_id"), field="from_stop_id"),
        require_text(_get(row, "to_stop_id"), field="to_stop_id"),
        parse_int(_get(row, "transfer_type"), field="transfer_type"),
        parse_int(_get(row, "min_transfer_time"), field="min_transfer_time"),
    )


#: Parents first — the order COPY runs in, so foreign keys resolve as we go.
TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        gtfs_file="agency.txt",
        table="agencies",
        columns=(
            "agency_source",
            "agency_id",
            "agency_name",
            "agency_url",
            "agency_timezone",
            "agency_lang",
            "agency_phone",
            "agency_fare_url",
            "agency_email",
        ),
        row_factory=_agency_row,
    ),
    TableSpec(
        gtfs_file="stops.txt",
        table="stops",
        columns=(
            "agency_source",
            "stop_id",
            "stop_code",
            "stop_name",
            "stop_desc",
            "stop_lat",
            "stop_lon",
            "zone_id",
            "stop_url",
            "location_type",
            "parent_station",
            "stop_timezone",
            "wheelchair_boarding",
        ),
        row_factory=_stop_row,
    ),
    TableSpec(
        gtfs_file="routes.txt",
        table="routes",
        columns=(
            "agency_source",
            "route_id",
            "agency_id",
            "route_short_name",
            "route_long_name",
            "route_desc",
            "route_type",
            "route_url",
            "route_color",
            "route_text_color",
        ),
        row_factory=_route_row,
    ),
    TableSpec(
        gtfs_file="calendar.txt",
        table="calendar",
        columns=(
            "agency_source",
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ),
        row_factory=_calendar_row,
        # GTFS allows a feed to define all service through calendar_dates alone.
        required=False,
    ),
    TableSpec(
        gtfs_file="calendar_dates.txt",
        table="calendar_dates",
        columns=("agency_source", "service_id", "date", "exception_type"),
        row_factory=_calendar_date_row,
        required=False,
    ),
    TableSpec(
        gtfs_file="shapes.txt",
        table="shapes",
        columns=(
            "agency_source",
            "shape_id",
            "shape_pt_sequence",
            "shape_pt_lat",
            "shape_pt_lon",
            "shape_dist_traveled",
        ),
        row_factory=_shape_row,
        required=False,
    ),
    TableSpec(
        gtfs_file="trips.txt",
        table="trips",
        columns=(
            "agency_source",
            "trip_id",
            "route_id",
            "service_id",
            "trip_headsign",
            "trip_short_name",
            "direction_id",
            "block_id",
            "shape_id",
            "wheelchair_accessible",
            "bikes_allowed",
        ),
        row_factory=_trip_row,
    ),
    TableSpec(
        gtfs_file="stop_times.txt",
        table="stop_times",
        columns=(
            "agency_source",
            "trip_id",
            "stop_sequence",
            "arrival_time",
            "departure_time",
            "stop_id",
            "stop_headsign",
            "pickup_type",
            "drop_off_type",
            "shape_dist_traveled",
            "timepoint",
        ),
        row_factory=_stop_time_row,
    ),
    TableSpec(
        gtfs_file="transfers.txt",
        table="transfers",
        columns=(
            "agency_source",
            "from_stop_id",
            "to_stop_id",
            "transfer_type",
            "min_transfer_time",
        ),
        row_factory=_transfer_row,
        required=False,
    ),
)
