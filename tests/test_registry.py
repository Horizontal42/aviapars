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
)
from aviapars.adapters.registry import AdapterRegistry

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
        origin=PlaceRef(place_id=1, iata="GOJ", yandex_code=None, city_id=None),
        dest=PlaceRef(place_id=2, iata="LED", yandex_code=None, city_id=None),
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
