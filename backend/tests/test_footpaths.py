"""M4: the PostGIS footpaths that make the two networks one graph.

Counts here are pinned to the 2026-08-23 publication of both feeds and were
read out of PostGIS before being written down, not copied from the generator's
own output. They are the same figures docs/feeds.md quotes, so a change in
either place should fail here rather than quietly diverge.

The load-bearing test is `TestTheClosureIsRetired`. Both engines have been
closing TheRide's declared transfers transitively since M2, purely to work
around the feed declaring 103->108 and 108->101 but no 103->101. That workaround
is only safe to delete if the generated set already contains every edge the
closure invents — so that is asserted directly rather than assumed.
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.preprocess.footpaths import build_footpaths, cross_agency_pairs
from a2transit.routing.constants import (
    FOOTPATH_MAX_METRES,
    MIN_TRANSFER_SECONDS,
    close_transfers,
    effective_transfer_seconds,
    walking_seconds,
)
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS

#: Both feeds as published 2026-08-23.
EXPECTED_TOTAL = 8_308
EXPECTED_CROSS_AGENCY = 1_456
EXPECTED_DECLARED = 15


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    for agency, filename in ((THERIDE, "theride.zip"), (MBUS, "mbus.zip")):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)
    build_footpaths(db_engine)
    return db_engine


def _links(engine: Engine) -> list:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT from_agency_source, from_stop_id, to_agency_source, to_stop_id, "
                "metres, seconds, declared_seconds FROM footpaths"
            )
        ).all()


def _keys(engine: Engine) -> set[tuple]:
    return {
        (
            AgencySource(row.from_agency_source),
            row.from_stop_id,
            AgencySource(row.to_agency_source),
            row.to_stop_id,
        )
        for row in _links(engine)
    }


class TestScale:
    def test_the_generated_set_is_the_documented_size(self, engine: Engine) -> None:
        result = build_footpaths(engine)

        assert result.total == EXPECTED_TOTAL
        assert result.cross_agency == EXPECTED_CROSS_AGENCY

    def test_cross_agency_links_are_the_728_documented_pairs(self, engine: Engine) -> None:
        """Directed rows, so twice the unordered count docs/feeds.md quotes."""
        assert EXPECTED_CROSS_AGENCY == 728 * 2

    def test_every_declared_transfer_survives(self, engine: Engine) -> None:
        """15 usable rows of the 17 TheRide publishes; 2 are self-transfers."""
        result = build_footpaths(engine)

        assert result.declared == EXPECTED_DECLARED

    def test_nothing_is_kept_only_because_it_is_declared(self, engine: Engine) -> None:
        """Every declared transfer is also inside the radius, today.

        If a feed ever declares a transfer longer than 400 m the generator keeps
        it — the agency knows something the geometry does not — and this figure
        stops being zero. That is a fact worth noticing rather than a failure.
        """
        result = build_footpaths(engine)

        assert result.beyond_radius == 0
        assert result.max_metres <= FOOTPATH_MAX_METRES

    def test_the_two_networks_now_touch_at_the_documented_corners(
        self, engine: Engine
    ) -> None:
        closest = cross_agency_pairs(engine, limit=3)

        assert [(a, b) for a, b, _ in closest] == [
            ("Bonisteel + Beal", "Cooley Lab Outbound"),
            ("State + Monroe", "Law Quad"),
            ("SB State + Monroe", "South Quad"),
        ]
        assert closest[0][2] < 1.0  # 0.4 m — the same corner, twice named


class TestGeometryIsWellFormed:
    def test_no_stop_walks_to_itself(self, engine: Engine) -> None:
        """The search already models waiting; a self-loop would be a free 60 s."""
        for from_agency, from_stop, to_agency, to_stop in _keys(engine):
            assert (from_agency, from_stop) != (to_agency, to_stop)

    def test_every_link_has_its_reverse(self, engine: Engine) -> None:
        """Straight-line distance is symmetric, so reachability must be too.

        A one-way footpath would mean a rider could reach a stop and not get
        back, which is not a thing geometry can express.
        """
        keys = _keys(engine)
        missing = {
            key for key in keys if (key[2], key[3], key[0], key[1]) not in keys
        }

        assert missing == set()

    def test_no_link_exceeds_the_radius(self, engine: Engine) -> None:
        for row in _links(engine):
            assert row.metres <= FOOTPATH_MAX_METRES

    def test_a_rebuild_is_byte_identical(self, engine: Engine) -> None:
        """Pure function of stops and transfers, so running it twice is a no-op."""
        before = _keys(engine)
        build_footpaths(engine)

        assert _keys(engine) == before


class TestWalkingTime:
    def test_every_link_clears_the_transfer_floor(self, engine: Engine) -> None:
        for row in _links(engine):
            assert row.seconds >= MIN_TRANSFER_SECONDS

    def test_seconds_come_from_the_shared_formula(self, engine: Engine) -> None:
        """One definition of what a transfer costs, in routing.constants.

        Computing it in SQL instead would put a second copy of the floor, the
        speed and the detour allowance somewhere the engines never look.
        """
        for row in _links(engine):
            assert row.seconds == effective_transfer_seconds(
                row.declared_seconds, row.metres
            )

    def test_the_declared_ten_seconds_is_never_honoured(self, engine: Engine) -> None:
        """TheRide declares 10 s across bays up to 70.5 m apart — 25 km/h."""
        declared = [row for row in _links(engine) if row.declared_seconds is not None]

        assert declared, "expected TheRide's declared transfers to be present"
        for row in declared:
            assert row.declared_seconds == 10
            assert row.seconds > row.declared_seconds

    def test_a_four_hundred_metre_walk_is_about_five_minutes(self) -> None:
        """Sanity on the detour allowance: 400 m straight line, not 400 m walked."""
        assert walking_seconds(400) == 400
        assert 300 < walking_seconds(400) <= 420


class TestTheClosureIsRetired:
    """Why `constants.close_transfers` can be deleted.

    It exists because TheRide declares 103->108 and 108->101 and no 103->101,
    though the bays are ~50 m apart, and the two engines chained differently
    without it. If the generated set already contains every edge the closure
    produces, both engines can read footpaths directly and the workaround goes.
    """

    def _closure(self, engine: Engine) -> set[tuple]:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT t.agency_source, t.from_stop_id, t.to_stop_id,
                           t.min_transfer_time, ST_Distance(a.geog, b.geog) AS metres
                      FROM transfers t
                      JOIN stops a ON a.agency_source = t.agency_source
                                  AND a.stop_id = t.from_stop_id
                      JOIN stops b ON b.agency_source = t.agency_source
                                  AND b.stop_id = t.to_stop_id
                     WHERE t.transfer_type <> 3 AND t.from_stop_id <> t.to_stop_id
                    """
                )
            ).all()

        links: dict[tuple, list] = defaultdict(list)
        for row in rows:
            agency = AgencySource(row.agency_source)
            links[(agency, row.from_stop_id)].append(
                (
                    (agency, row.to_stop_id),
                    effective_transfer_seconds(row.min_transfer_time, row.metres),
                )
            )
        closed = close_transfers({stop: tuple(targets) for stop, targets in links.items()})
        return {
            (source[0], source[1], target[0], target[1])
            for source, targets in closed.items()
            for target, _ in targets
        }

    def test_the_closure_adds_the_edge_the_feed_omits(self, engine: Engine) -> None:
        """103->101: the reason the workaround was written in the first place."""
        closure = self._closure(engine)

        assert (THERIDE, "103", THERIDE, "101") in closure

    def test_every_closure_edge_exists_directly_as_a_footpath(
        self, engine: Engine
    ) -> None:
        uncovered = self._closure(engine) - _keys(engine)

        assert uncovered == set(), (
            "close_transfers() invents edges the 400 m footpath set does not "
            f"contain, so it cannot be retired: {sorted(uncovered)[:5]}"
        )

    def test_the_omitted_edge_is_a_short_walk(self, engine: Engine) -> None:
        """Not merely present, but present because the bays really are adjacent."""
        with engine.connect() as connection:
            metres = connection.execute(
                text(
                    "SELECT metres FROM footpaths WHERE from_agency_source = :a "
                    "AND from_stop_id = '103' AND to_agency_source = :a "
                    "AND to_stop_id = '101'"
                ),
                {"a": THERIDE.value},
            ).scalar()

        assert metres is not None
        assert metres < 100


class TestReloadingOneAgency:
    """A footpath is the only row in the schema that names both feeds.

    So the ingest's delete-and-reload cannot select an agency's rows with
    `agency_source = :source` here: reloading TheRide has to drop the
    MBus->TheRide direction too, or the foreign key fails partway through.
    """

    def test_reloading_one_feed_does_not_violate_the_foreign_keys(
        self, engine: Engine
    ) -> None:
        load_from_path(engine, THERIDE, DATA_DIR / "theride.zip")

        with engine.connect() as connection:
            surviving = connection.execute(text("SELECT count(*) FROM footpaths")).scalar()

        # Every link with a TheRide end is gone until footpaths are rebuilt;
        # the purely-MBus ones remain, because nothing they reference moved.
        assert surviving < EXPECTED_TOTAL

        result = build_footpaths(engine)
        assert result.total == EXPECTED_TOTAL
