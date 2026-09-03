"""Seeded differential testing: RAPTOR against the M2 Dijkstra reference.

Cases come from a fixed seed (`compare.SEED`), so case N is the same case on
every run and a mismatch reproduces exactly — the failure message carries the
command that replays it. The first N cases of a large run are the same cases as
a small one, so the quick pass below is a genuine prefix of the full one rather
than a different sample.

Two sizes:

  * a 60-case pass in the default suite, for fast feedback
  * a 500-case pass marked `slow`, run with `pytest -m slow`

Both engines are given the same round budget. M2's Dijkstra has no round limit
and will return a nine-ride Saturday journey; capping RAPTOR at its product
default of 6 would make them disagree by configuration and hide real mismatches
behind an expected one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.preprocess.patterns import build_patterns
from a2transit.routing.compare import (
    SEED,
    Comparison,
    compare_cases,
    generate_cases,
    load_servable_stops,
    summarise,
)
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

QUICK_CASES = 60
FULL_CASES = 500

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    for agency, filename in ((THERIDE, "theride.zip"), (MBUS, "mbus.zip")):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)
        build_patterns(db_engine, agency)
    return db_engine


def _dump_cases(comparisons: tuple[Comparison, ...], path: Path) -> None:
    """Write every case out, so a failure at case 387 does not require rerunning
    the first 386 to see what they were."""
    path.write_text(
        json.dumps(
            [
                {
                    "index": comparison.case.index,
                    "origin": f"{comparison.case.origin[0].value}:{comparison.case.origin[1]}",
                    "destination": (
                        f"{comparison.case.destination[0].value}:"
                        f"{comparison.case.destination[1]}"
                    ),
                    "departure": comparison.case.departure.isoformat(),
                    "raptor": comparison.raptor_arrival.isoformat()
                    if comparison.raptor_arrival
                    else None,
                    "dijkstra": comparison.dijkstra_arrival.isoformat()
                    if comparison.dijkstra_arrival
                    else None,
                    "agree": comparison.arrivals_agree,
                }
                for comparison in comparisons
            ],
            indent=2,
        )
    )


def _assert_agreement(comparisons: tuple[Comparison, ...], artefact: Path) -> None:
    summary = summarise(comparisons)
    _dump_cases(comparisons, artefact)

    assert summary.arrival_mismatches == (), (
        f"{len(summary.arrival_mismatches)} of {summary.total} cases disagree.\n"
        + "\n\n".join(
            mismatch.describe() for mismatch in summary.arrival_mismatches[:5]
        )
        + f"\n\nAll cases written to {artefact}"
    )
    assert summary.both_found > 0, "no case produced a journey — the sample is useless"


class TestSeededDifferential:
    def test_the_seed_is_pinned(self) -> None:
        """Changing it resamples every case and invalidates past reproductions."""
        assert SEED == 20260910

    def test_generation_is_reproducible(self, engine: Engine) -> None:
        stops = load_servable_stops(engine)

        first = generate_cases(stops, 50)
        second = generate_cases(stops, 50)

        assert first == second

    def test_a_short_run_is_a_prefix_of_a_long_one(self, engine: Engine) -> None:
        """So the quick pass genuinely covers a subset of the slow one."""
        stops = load_servable_stops(engine)

        short = generate_cases(stops, 20)
        long_run = generate_cases(stops, 100)

        assert long_run[:20] == short

    def test_stop_sampling_order_is_deterministic(self, engine: Engine) -> None:
        assert load_servable_stops(engine) == load_servable_stops(engine)

    def test_quick_pass_agrees(self, engine: Engine, tmp_path: Path) -> None:
        stops = load_servable_stops(engine)
        comparisons = compare_cases(engine, generate_cases(stops, QUICK_CASES))

        _assert_agreement(comparisons, tmp_path / "differential-quick.json")

    @pytest.mark.slow
    def test_full_pass_agrees(self, engine: Engine, tmp_path: Path) -> None:
        stops = load_servable_stops(engine)
        comparisons = compare_cases(
            engine, generate_cases(stops, FULL_CASES), progress_every=100
        )

        _assert_agreement(comparisons, tmp_path / "differential-full.json")


@pytest.fixture(scope="module")
def comparisons(engine: Engine) -> tuple[Comparison, ...]:
    stops = load_servable_stops(engine)
    return compare_cases(engine, generate_cases(stops, QUICK_CASES))


class TestDifferentialProperties:
    def test_the_engines_agree_on_unreachability_too(
        self, comparisons: tuple[Comparison, ...]
    ) -> None:
        """Agreeing that nothing exists is as much a result as agreeing on a time."""
        for comparison in comparisons:
            assert (comparison.raptor_arrival is None) == (
                comparison.dijkstra_arrival is None
            ), comparison.describe()

    def test_trip_level_differences_only_occur_on_equal_arrivals(
        self, comparisons: tuple[Comparison, ...]
    ) -> None:
        """A different trip choice is fine; a different arrival time is not."""
        summary = summarise(comparisons)

        for comparison in summary.trip_mismatches:
            assert comparison.arrivals_agree

    def test_raptor_is_substantially_faster(
        self, comparisons: tuple[Comparison, ...]
    ) -> None:
        summary = summarise(comparisons)
        raptor_p50 = summary._percentile(summary.raptor_times, 0.5)
        dijkstra_p50 = summary._percentile(summary.dijkstra_times, 0.5)

        assert raptor_p50 < dijkstra_p50


class TestPerformanceTarget:
    """M3's acceptance criterion: median query under 50 ms."""

    def test_median_query_is_under_50ms(self, engine: Engine) -> None:
        stops = load_servable_stops(engine)
        comparisons = compare_cases(engine, generate_cases(stops, QUICK_CASES))
        summary = summarise(comparisons)

        median_ms = summary._percentile(summary.raptor_times, 0.5) * 1000
        p95_ms = summary._percentile(summary.raptor_times, 0.95) * 1000

        assert median_ms < 50, f"p50 {median_ms:.2f} ms"
        # Generous: even the tail should sit far inside the target.
        assert p95_ms < 50, f"p95 {p95_ms:.2f} ms"
