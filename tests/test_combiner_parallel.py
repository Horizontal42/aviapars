from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from aviapars.adapters.base import (
    HealthStatus,
    Leg,
    Mode,
    Offer,
    PlaceRef,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
)
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Place
from aviapars.domain.combiner import SearchOpts, _search_leg
from aviapars.domain.dates import DatePlan

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


class _Concurrency:
    """Shared counter every fake adapter bumps on entry — `peak > 1` is the
    direct observation that two adapters were genuinely in flight together,
    which is far less CI-flaky than timing wall-clock against sum(delays)."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.finish_order: list[str] = []


class _SlowAdapter:
    display_name = "Slow"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None
    supports_batch_dates = False
    rate_limit = RateLimitPolicy(min_interval_s=0)
    default_cache_ttl_s = 60
    request_timeout_s = 5
    total_timeout_s = 5

    def __init__(self, source_id: str, delay_s: float, tracker: _Concurrency, *, price_minor: int = 100000) -> None:
        self.id = source_id
        self._delay_s = delay_s
        self._tracker = tracker
        self._price_minor = price_minor

    async def search(self, q: SearchQuery) -> SearchResult:
        self._tracker.live += 1
        self._tracker.peak = max(self._tracker.peak, self._tracker.live)
        try:
            await asyncio.sleep(self._delay_s)
        finally:
            self._tracker.live -= 1
        self._tracker.finish_order.append(self.id)
        return SearchResult(status=ResultStatus.OK, offers=[_offer(self.id, self._price_minor)], partial=False)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True)


class _RaisingAdapter(_SlowAdapter):
    async def search(self, q: SearchQuery) -> SearchResult:
        await asyncio.sleep(self._delay_s)
        raise RuntimeError("adapter contract violated")


def _offer(source_id: str, price_minor: int) -> Offer:
    depart = datetime.now(timezone.utc) + timedelta(days=7)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None),
        to_place=PlaceRef(place_id=2, iata="AER", yandex_code=None, rzd_code=None, city_id=None),
        depart_utc=depart,
        depart_local=depart,
    )
    return Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=None,
        source_id=source_id,
        tier=Tier.CONFIRMATION,
        observed_at=datetime.now(timezone.utc),
        source_cached_at=None,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=f"h-{source_id}",
    )


def _place(place_id: int, iata: str) -> Place:
    return Place(
        id=place_id,
        kind="city",
        name_ru=iata,
        name_en=iata,
        iata=iata,
        yandex_code=None,
        rzd_code=None,
        city_id=None,
        country="RU",
        region=None,
        lat=None,
        lon=None,
        popularity=0,
        is_hub_candidate=False,
    )


@pytest.fixture
async def db(tmp_path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "combiner_parallel.db")
    await conn.executescript(MIGRATION_TABLES)
    yield conn
    await conn.close()


def _registry(db: aiosqlite.Connection) -> AdapterRegistry:
    return AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )


def _plan() -> DatePlan:
    day = (datetime.now(timezone.utc) + timedelta(days=7)).date()
    return DatePlan(batch_dates=(day,), probe_dates=(day,))


def _opts() -> SearchOpts:
    return SearchOpts(tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}), max_hops=0)


async def _run_leg(db: aiosqlite.Connection, adapters: list[_SlowAdapter]):
    registry = _registry(db)
    for adapter in adapters:
        registry.register(adapter)
    return await _search_leg(
        registry,
        _place(1, "SVX"),
        _place(2, "AER"),
        _plan(),
        1,
        Mode.PLANE,
        _opts(),
        datetime.now(timezone.utc),
    )


async def test_adapters_for_one_leg_run_concurrently(db: aiosqlite.Connection) -> None:
    """Regression: _search_leg used to await each adapter in turn, so a leg cost
    the *sum* of every adapter's timeout. The registry's own semaphores/rate
    limiter/circuit breaker are what bound real network concurrency, not this loop."""
    tracker = _Concurrency()
    adapters = [_SlowAdapter("slow_a", 0.10, tracker), _SlowAdapter("slow_b", 0.10, tracker)]

    offers, meta = await _run_leg(db, adapters)

    assert tracker.peak == 2  # both were genuinely in flight at the same moment
    assert sorted(meta.sources_used) == ["slow_a", "slow_b"]
    assert len(offers) == 2


async def test_bookkeeping_stays_in_adapter_order_not_completion_order(db: aiosqlite.Connection) -> None:
    """Requests overlap, but results must be folded into meta in registration
    order so sources_used is reproducible run to run."""
    tracker = _Concurrency()
    # first-registered adapter deliberately finishes *last*
    adapters = [_SlowAdapter("slow_first", 0.15, tracker), _SlowAdapter("fast_second", 0.01, tracker)]

    _offers, meta = await _run_leg(db, adapters)

    assert tracker.finish_order == ["fast_second", "slow_first"]
    assert meta.sources_used == ["slow_first", "fast_second"]


async def test_one_raising_adapter_does_not_lose_the_others_offers(db: aiosqlite.Connection) -> None:
    """An adapter that breaks its no-raise contract is absorbed by the registry
    (UPSTREAM_ERROR), so the sibling still in flight keeps its offers."""
    tracker = _Concurrency()
    adapters = [_RaisingAdapter("broken", 0.01, tracker), _SlowAdapter("healthy", 0.05, tracker)]

    offers, meta = await _run_leg(db, adapters)

    assert [o.source_id for o in offers] == ["healthy"]
    assert meta.sources_skipped == [("broken", "upstream_error")]
    assert meta.partial is True


async def test_exception_escaping_the_registry_does_not_cancel_sibling_adapters(db: aiosqlite.Connection) -> None:
    """The gather() backstop itself: if anything ever raises past registry.search()
    (which contractually can't, but a bare gather() would then cancel every other
    in-flight adapter), the leg must still return what the healthy sources found."""
    tracker = _Concurrency()
    healthy = _SlowAdapter("healthy", 0.05, tracker)
    exploding = _SlowAdapter("exploding", 0.01, tracker)

    class _ExplodingRegistry(AdapterRegistry):
        async def search(self, adapter, query, **kwargs):  # type: ignore[no-untyped-def,override]
            if adapter.id == "exploding":
                raise RuntimeError("registry contract violated")
            return await super().search(adapter, query, **kwargs)

    registry = _ExplodingRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(exploding)
    registry.register(healthy)

    offers, meta = await _search_leg(
        registry, _place(1, "SVX"), _place(2, "AER"), _plan(), 1, Mode.PLANE, _opts(), datetime.now(timezone.utc)
    )

    assert [o.source_id for o in offers] == ["healthy"]
    assert meta.sources_skipped == [("exploding", "upstream_error")]
    assert meta.partial is True
