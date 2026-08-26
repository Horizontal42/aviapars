from __future__ import annotations

from datetime import date

import aiosqlite
import pytest

from aviapars.adapters.base import (
    HealthStatus,
    Mode,
    PlaceRef,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    probe_dates_total_timeout_s,
)
from aviapars.adapters.registry import AdapterRegistry, _resolve_timeout_s
from aviapars.config import settings

MIGRATION_TABLES = """
CREATE TABLE source_health (
    source_id TEXT PRIMARY KEY, status TEXT, consecutive_failures INTEGER,
    circuit_open_until TEXT, last_success_at TEXT, last_error_at TEXT,
    last_error_kind TEXT, last_error_msg TEXT, requests_today INTEGER, notified_admin_at TEXT
);
CREATE TABLE source_cache (
    cache_key TEXT PRIMARY KEY, source_id TEXT, body BLOB, content_type TEXT,
    fetched_at TEXT, expires_at TEXT
);
"""


class AlwaysEmptyAdapter:
    id = "always_empty"
    display_name = "Always Empty"
    modes = frozenset({Mode.PLANE})
    tier = Tier.DISCOVERY
    countries: frozenset[str] | None = None
    supports_batch_dates = True
    rate_limit = RateLimitPolicy(min_interval_s=0)
    default_cache_ttl_s = 60
    request_timeout_s = 5
    total_timeout_s = 5

    async def search(self, q: SearchQuery) -> SearchResult:
        return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True)


@pytest.fixture
async def db(tmp_path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "registry.db")
    await conn.executescript(MIGRATION_TABLES)
    yield conn
    await conn.close()


def _query() -> SearchQuery:
    return SearchQuery(
        origin=PlaceRef(place_id=1, iata="GOJ", yandex_code=None, rzd_code=None, city_id=None),
        dest=PlaceRef(place_id=2, iata="LED", yandex_code=None, rzd_code=None, city_id=None),
        dates=(date(2026, 9, 18),),
        pax=1,
        mode=Mode.PLANE,
    )


async def test_repeated_empty_results_do_not_open_circuit(db: aiosqlite.Connection) -> None:
    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    adapter = AlwaysEmptyAdapter()
    registry.register(adapter)

    # more than FAILURE_THRESHOLD (5) consecutive EMPTY results
    for _ in range(8):
        result = await registry.search(adapter, _query())
        assert result.status == ResultStatus.EMPTY

    async with db.execute("SELECT status, consecutive_failures FROM source_health WHERE source_id = ?", (adapter.id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    status, failures = row
    assert status == "ok"
    assert failures == 0


async def test_empty_results_are_cached(db: aiosqlite.Connection) -> None:
    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    adapter = AlwaysEmptyAdapter()
    registry.register(adapter)

    result = await registry.search(adapter, _query())
    assert result.status == ResultStatus.EMPTY

    async with db.execute("SELECT COUNT(*) FROM source_cache WHERE source_id = ?", (adapter.id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


class _SlowCeilingAdapter(AlwaysEmptyAdapter):
    id = "slow_ceiling"
    total_timeout_s = 270  # e.g. s7_direct: 7 probe dates + 2 lookups + one browser solve


def test_deadline_cannot_starve_an_adapter_below_its_own_ceiling() -> None:
    # regression: `deadline_s or adapter.total_timeout_s` let any truthy caller deadline win
    # outright, so a flat 90s silently discarded the ceiling the adapter itself declared and
    # killed multi-date searches that were working, mid-flight. Applies to both lanes now —
    # interactive used to be a hard ceiling instead (see test_interactive_deadline_no_longer_
    # truncates below), but the egress pool made a source's honest worst case routinely exceed
    # a short interactive deadline, so the floor behaviour is the same in both lanes.
    adapter = _SlowCeilingAdapter()
    assert _resolve_timeout_s(adapter, 90.0) == 270.0


def test_deadline_still_wins_when_it_is_more_generous() -> None:
    adapter = _SlowCeilingAdapter()
    assert _resolve_timeout_s(adapter, 400.0) == 400.0


def test_interactive_deadline_no_longer_truncates_below_the_ceiling() -> None:
    # 2026-08-19: interactive used to hard-cap at deadline_s regardless of the adapter's own
    # total_timeout_s — this asserts that's gone. The `lane` argument itself is gone from
    # _resolve_timeout_s (it no longer affects this decision either way).
    adapter = _SlowCeilingAdapter()
    assert _resolve_timeout_s(adapter, 12.0) == 270.0


def test_no_deadline_falls_back_to_the_adapters_own_ceiling() -> None:
    adapter = _SlowCeilingAdapter()
    assert _resolve_timeout_s(adapter, None) == 270.0


def test_probe_dates_ceiling_tracks_the_configured_cycle_width() -> None:
    # the point of the helper: an adapter's declared worst case and the number of dates a
    # cycle actually probes cannot drift apart, which is how every confirmation adapter ended
    # up claiming a flat 60s for work its own docstring said needed 7 sequential requests.
    assert probe_dates_total_timeout_s(20) == 20 * settings.flex_probe_dates_per_cycle
    assert probe_dates_total_timeout_s(20, setup_s=90) == 20 * settings.flex_probe_dates_per_cycle + 90
