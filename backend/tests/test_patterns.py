"""RAPTOR pattern preprocessing against the real feeds."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.preprocess.patterns import build_patterns, verify_no_overtaking
from tests.conftest import load_real_feeds

pytestmark = pytest.mark.db

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS


@pytest.fixture(scope="module")
def built(db_engine: Engine) -> Engine:
    load_real_feeds(db_engine, patterns=True)
    return db_engine


class TestPatternCounts:
    def test_gtfs_routes_expand_into_patterns(self, built: Engine) -> None:
        """42 GTFS routes with trips become 117 patterns."""
        with built.connect() as connection:
            counts = dict(
                connection.execute(
                    text("SELECT agency_source::text, count(*) FROM route_patterns GROUP BY 1")
                ).all()
            )

        assert counts == {"theride": 86, "mbus": 31}

    def test_pattern_stop_totals(self, built: Engine) -> None:
        with built.connect() as connection:
            counts = dict(
                connection.execute(
                    text("SELECT agency_source::text, count(*) FROM pattern_stops GROUP BY 1")
                ).all()
            )

        assert counts == {"theride": 2128, "mbus": 370}

    def test_route_4_has_one_pattern_per_direction(self, built: Engine) -> None:
        with built.connect() as connection:
            patterns = connection.execute(
                text(
                    "SELECT pattern_id, direction_id, stop_count, trip_count "
                    "FROM route_patterns WHERE agency_source = 'theride' AND route_id = '4' "
                    "ORDER BY pattern_id"
                )
            ).all()

        assert len(patterns) == 2
        assert {pattern.direction_id for pattern in patterns} == {0, 1}
        assert sum(pattern.trip_count for pattern in patterns) == 286

    def test_routes_without_trips_produce_no_patterns(self, built: Engine) -> None:
        """MBus ships 4 such rows: Test Route, Training route, and two charters."""
        with built.connect() as connection:
            orphans = connection.execute(
                text(
                    "SELECT count(*) FROM routes r WHERE NOT EXISTS ("
                    "  SELECT 1 FROM route_patterns p "
                    "   WHERE p.agency_source = r.agency_source AND p.route_id = r.route_id)"
                )
            ).scalar_one()

        assert orphans == 4


class TestPatternIntegrity:
    def test_signatures_are_unique_within_an_agency(self, built: Engine) -> None:
        with built.connect() as connection:
            duplicates = connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT agency_source, signature FROM route_patterns "
                    "   GROUP BY 1, 2 HAVING count(*) > 1) AS d"
                )
            ).scalar_one()

        assert duplicates == 0

    def test_every_trip_belongs_to_exactly_one_pattern(self, built: Engine) -> None:
        with built.connect() as connection:
            trips, assignments = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM trips), "
                    "       (SELECT count(*) FROM trips_by_pattern)"
                )
            ).one()

        assert trips == assignments == 12667

    def test_pattern_positions_are_dense_and_zero_based(self, built: Engine) -> None:
        """RAPTOR indexes patterns by position, so gaps would be silent corruption."""
        with built.connect() as connection:
            bad = connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT pattern_id FROM pattern_stops "
                    "   GROUP BY agency_source, pattern_id "
                    "  HAVING min(position) <> 0 "
                    "      OR max(position) <> count(*) - 1) AS d"
                )
            ).scalar_one()

        assert bad == 0

    def test_trip_order_is_dense_within_each_service_day(self, built: Engine) -> None:
        with built.connect() as connection:
            bad = connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT pattern_id FROM trips_by_pattern "
                    "   GROUP BY agency_source, pattern_id, service_id "
                    "  HAVING min(trip_order) <> 0 "
                    "      OR max(trip_order) <> count(*) - 1) AS d"
                )
            ).scalar_one()

        assert bad == 0

    def test_reverse_index_matches_the_forward_one(self, built: Engine) -> None:
        with built.connect() as connection:
            forward, reverse, mismatched = connection.execute(
                text(
                    """
                    SELECT (SELECT count(*) FROM pattern_stops),
                           (SELECT count(*) FROM stop_patterns),
                           (SELECT count(*) FROM pattern_stops ps
                             WHERE NOT EXISTS (
                               SELECT 1 FROM stop_patterns sp
                                WHERE sp.agency_source = ps.agency_source
                                  AND sp.pattern_id = ps.pattern_id
                                  AND sp.stop_id = ps.stop_id
                                  AND sp.position = ps.position))
                    """
                )
            ).one()

        assert forward == reverse
        assert mismatched == 0

    def test_pattern_ids_are_stable_across_a_rebuild(self, built: Engine) -> None:
        """Ordinals come from sorting by signature, not from iteration order."""
        with built.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT pattern_id, signature FROM route_patterns "
                    "WHERE agency_source = 'theride' ORDER BY pattern_id"
                )
            ).all()

        build_patterns(built, THERIDE)

        with built.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT pattern_id, signature FROM route_patterns "
                    "WHERE agency_source = 'theride' ORDER BY pattern_id"
                )
            ).all()

        assert before == after


class TestOvertaking:
    """RAPTOR's binary search is only sound if trips never overtake.

    Ordering a pattern's trips globally rather than per service day mixes days
    with different running times and invents violations that happen on no real
    day. This is the finding the ordering was changed for, pinned so a
    "simplification" back to a global ordering fails loudly.
    """

    def test_theride_has_no_overtaking_within_a_service_day(self, built: Engine) -> None:
        assert verify_no_overtaking(built, THERIDE) == []

    def test_exactly_one_mbus_pattern_overtakes(self, built: Engine) -> None:
        assert verify_no_overtaking(built, MBUS) == ["mbus:WX:0"]

    def test_the_offending_pattern_is_flagged_for_scanning(self, built: Engine) -> None:
        with built.connect() as connection:
            flagged = connection.execute(
                text("SELECT pattern_id FROM route_patterns WHERE has_overtaking ORDER BY 1")
            ).scalars().all()

        assert flagged == ["mbus:WX:0"]

    def test_a_global_trip_ordering_would_invent_violations(self, built: Engine) -> None:
        """12 patterns look violating when service days are mixed; 11 spuriously."""
        with built.connect() as connection:
            spurious = connection.execute(
                text(
                    """
                    WITH ordered AS (
                        SELECT tbp.agency_source, tbp.pattern_id, ps.position, st.arrival_time,
                               lag(st.arrival_time) OVER (
                                   PARTITION BY tbp.agency_source, tbp.pattern_id, ps.position
                                   ORDER BY tbp.first_departure, tbp.trip_id
                               ) AS previous_arrival
                          FROM trips_by_pattern tbp
                          JOIN pattern_stops ps
                            ON ps.agency_source = tbp.agency_source
                           AND ps.pattern_id = tbp.pattern_id
                          JOIN stop_times st
                            ON st.agency_source = tbp.agency_source
                           AND st.trip_id = tbp.trip_id
                           AND st.stop_id = ps.stop_id
                    )
                    SELECT count(DISTINCT pattern_id) FROM ordered
                     WHERE previous_arrival IS NOT NULL AND arrival_time < previous_arrival
                    """
                )
            ).scalar_one()

        assert spurious == 12
        real = len(verify_no_overtaking(built, THERIDE)) + len(verify_no_overtaking(built, MBUS))
        assert real == 1
