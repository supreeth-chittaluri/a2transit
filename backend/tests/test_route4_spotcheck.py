"""M1 acceptance: verify a known real route survives ingest intact.

TheRide Route 4 "Washtenaw" — the busiest corridor in the system, running
between Blake Transit Center in Ann Arbor and Ypsilanti Transit Center. Every
expected value below was read out of the 2026-08-23 feed before the loader
existed, so these assertions check the ingest against the source rather than
against itself.

Requires the real feed. The whole module skips when data/theride.zip is absent:

    python -m a2transit.ingest --agency theride
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

FEED_PATH = DATA_DIR / "theride.zip"


@pytest.fixture(scope="module")
def loaded_theride(db_engine: Engine) -> Iterator[Engine]:
    if not FEED_PATH.exists():
        pytest.skip(f"{FEED_PATH} not present; run `python -m a2transit.ingest --agency theride`")

    load_from_path(db_engine, AgencySource.THERIDE, FEED_PATH)
    yield db_engine


def test_route_4_metadata(loaded_theride: Engine) -> None:
    with loaded_theride.connect() as connection:
        route = connection.execute(
            text(
                "SELECT route_id, route_long_name, route_type, route_color, agency_id "
                "FROM routes WHERE agency_source = 'theride' AND route_short_name = '4'"
            )
        ).one()

    assert route.route_id == "4"
    assert route.route_long_name == "Washtenaw"
    assert route.route_type == 3  # bus
    assert route.route_color == "993399"
    assert route.agency_id == "1179"  # AAATA


def test_route_4_trip_counts_by_service(loaded_theride: Engine) -> None:
    """164 weekday, 68 Saturday, 54 Sunday — 286 trips in total."""
    with loaded_theride.connect() as connection:
        by_service = dict(
            connection.execute(
                text(
                    "SELECT service_id, count(*) FROM trips "
                    "WHERE agency_source = 'theride' AND route_id = '4' "
                    "GROUP BY service_id"
                )
            ).all()
        )

    assert by_service == {"3": 164, "1": 68, "2": 54}
    assert sum(by_service.values()) == 286


def test_route_4_has_one_stop_pattern_per_direction(loaded_theride: Engine) -> None:
    """Two distinct ordered stop sequences — the seed of M3's RAPTOR patterns."""
    with loaded_theride.connect() as connection:
        patterns = connection.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT DISTINCT string_agg(st.stop_id, '>' ORDER BY st.stop_sequence)
                      FROM trips t
                      JOIN stop_times st
                        ON st.agency_source = t.agency_source AND st.trip_id = t.trip_id
                     WHERE t.agency_source = 'theride' AND t.route_id = '4'
                     GROUP BY t.trip_id
                ) AS distinct_patterns
                """
            )
        ).scalar_one()

    assert patterns == 2


def test_earliest_weekday_trip_matches_the_published_schedule(loaded_theride: Engine) -> None:
    """Trip 3572020: YTC Stop 2 at 06:02, Blake Transit Center at 06:43, 34 stops."""
    with loaded_theride.connect() as connection:
        stops = connection.execute(
            text(
                """
                SELECT st.stop_sequence, st.departure_time, st.stop_id, s.stop_name
                  FROM stop_times st
                  JOIN stops s
                    ON s.agency_source = st.agency_source AND s.stop_id = st.stop_id
                 WHERE st.agency_source = 'theride' AND st.trip_id = '3572020'
                 ORDER BY st.stop_sequence
                """
            )
        ).all()

    assert len(stops) == 34

    first, last = stops[0], stops[-1]
    assert (first.stop_id, first.stop_name) == ("1338", "YTC - Stop 2")
    assert first.departure_time == 6 * 3600 + 2 * 60  # 06:02:00
    assert last.stop_name == "Temp BTC endpt"
    assert last.departure_time == 6 * 3600 + 43 * 60  # 06:43:00

    # A 41-minute end-to-end run, and time never goes backwards along the trip.
    assert last.departure_time - first.departure_time == 41 * 60
    departures = [row.departure_time for row in stops]
    assert departures == sorted(departures)


def test_route_4_service_3_is_weekdays_not_monday_only(loaded_theride: Engine) -> None:
    """Guards the collision that fails silently.

    MBus also publishes a service_id "3", but theirs is Monday-only. If the two
    feeds were ever merged on service_id alone, Route 4's 164 weekday trips
    would collapse to Mondays and every Tuesday-to-Friday plan would come back
    empty — with no error anywhere.
    """
    with loaded_theride.connect() as connection:
        calendar = connection.execute(
            text(
                "SELECT monday, tuesday, wednesday, thursday, friday, saturday, sunday "
                "FROM calendar WHERE agency_source = 'theride' AND service_id = '3'"
            )
        ).one()

    assert tuple(calendar) == (True, True, True, True, True, False, False)


def test_route_4_shape_is_drawable(loaded_theride: Engine) -> None:
    """M6 draws this geometry on the map, so it must be a real multi-point line."""
    with loaded_theride.connect() as connection:
        shapes = connection.execute(
            text(
                """
                SELECT DISTINCT sg.shape_id, sg.point_count,
                       ST_GeometryType(sg.geom) AS geom_type,
                       ST_Length(sg.geom::geography) AS metres
                  FROM trips t
                  JOIN shape_geometries sg
                    ON sg.agency_source = t.agency_source AND sg.shape_id = t.shape_id
                 WHERE t.agency_source = 'theride' AND t.route_id = '4'
                """
            )
        ).all()

    assert shapes, "route 4 trips reference no shape geometry"
    for shape in shapes:
        assert shape.geom_type == "ST_LineString"
        assert shape.point_count > 100
        # The corridor is roughly 12 km each way; anything near zero means the
        # points were assembled in the wrong order or the SRID is wrong.
        assert 8_000 < shape.metres < 30_000
