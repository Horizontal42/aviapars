from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

import aiosqlite

from aviapars.adapters import circuit
from aviapars.adapters.base import (
    HealthStatus,
    Leg,
    Mode,
    Offer,
    PlaceRef,
    ResultStatus,
    SearchQuery,
    SearchResult,
    SourceAdapter,
    Tier,
)
from aviapars.adapters.cache import ResponseCache
from aviapars.adapters.ratelimit import RateLimiter
from aviapars.db.repo_health import HealthRepo

Lane = Literal["scheduler", "interactive"]


class AdapterRegistry:
    """Uniform rate-limit/circuit-breaker/timeout/cache wrapper applied to every
    adapter (§5 rule 5) — this is what keeps the 1 vCPU/1GB budget enforceable
    and what a new adapter gets for free just by registering.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        http_concurrency_global: int,
        http_concurrency_interactive: int,
        http_concurrency_per_source: int,
    ) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        self._rate_limiter = RateLimiter()
        self._health_repo = HealthRepo(db)
        self._cache = ResponseCache(db)
        self._global_sem = asyncio.Semaphore(http_concurrency_global)
        self._interactive_sem = asyncio.Semaphore(http_concurrency_interactive)
        self._per_source_concurrency = http_concurrency_per_source
        self._per_source_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_source_concurrency)
        )
        # get_state -> compute -> save_state is a read-modify-write with an `await` gap in
        # the middle — a scheduler cycle and a concurrent /search can both read the same
        # consecutive_failures, then both write back the same +1, silently losing one
        # failure's worth of count. One lock per source serializes that sequence.
        self._health_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.id] = adapter

    def all(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def for_query(self, mode: Mode, origin_country: str, dest_country: str) -> list[SourceAdapter]:
        """Filters by modes + countries (§5) — the two levers that bound how
        many adapters get asked per route as the roster grows."""
        matches = []
        for adapter in self._adapters.values():
            if mode not in adapter.modes:
                continue
            if adapter.countries is not None and not ({origin_country, dest_country} <= adapter.countries):
                continue
            matches.append(adapter)
        return matches

    async def search(
        self,
        adapter: SourceAdapter,
        query: SearchQuery,
        *,
        lane: Lane = "scheduler",
        allow_network: bool = True,
        deadline_s: float | None = None,
        bucket_wait_s: float | None = None,
    ) -> SearchResult:
        cache_key = self._cache_key(adapter.id, query)

        if not allow_network:
            cached = await self._cache.get(cache_key)
            if cached is None:
                return SearchResult(status=ResultStatus.CACHE_MISS, offers=[], partial=True)
            return _deserialize_result(cached)

        now = circuit.utcnow()
        async with self._health_locks[adapter.id]:
            state = await self._health_repo.get_state(adapter.id)
        if circuit.is_open(state, now):
            return SearchResult(status=ResultStatus.BLOCKED, offers=[], partial=True, error="circuit open")

        acquired = await self._rate_limiter.acquire(adapter.id, adapter.rate_limit, max_wait_s=bucket_wait_s)
        if not acquired:
            return SearchResult(status=ResultStatus.RATE_LIMITED, offers=[], partial=True)

        http_sem = self._interactive_sem if lane == "interactive" else self._global_sem
        source_sem = self._per_source_sems[adapter.id]
        timeout_s = _resolve_timeout_s(adapter, deadline_s, lane)

        result: SearchResult
        async with http_sem, source_sem:
            try:
                async with asyncio.timeout(timeout_s):
                    result = await adapter.search(query)
            except TimeoutError:
                result = SearchResult(status=ResultStatus.TIMEOUT, offers=[], partial=True)
            except Exception as exc:  # adapters must not raise (§5 rule 1) — belt-and-braces here
                result = SearchResult(status=ResultStatus.UPSTREAM_ERROR, offers=[], partial=True, error=str(exc))

        # EMPTY means the adapter was queried successfully and genuinely found nothing — that's
        # not a failure. Conflating it with real errors (timeout/rate-limited/upstream) was a
        # real bug: a route with legitimately sparse results (e.g. yandex_rasp returning no
        # ticket prices for a route/date, which turns out to be common) would trip the circuit
        # breaker after 5 empty-but-successful queries and get wrongly marked "down".
        healthy = result.status in (ResultStatus.OK, ResultStatus.EMPTY)
        async with self._health_locks[adapter.id]:
            # re-read rather than reuse the pre-HTTP-call `state` above — that value can be
            # arbitrarily stale by now (the adapter call may have taken the full timeout),
            # and computing off it would silently overwrite a concurrent task's update.
            fresh_state = await self._health_repo.get_state(adapter.id)
            if healthy:
                await self._health_repo.save_state(adapter.id, circuit.record_success(fresh_state))
            else:
                await self._health_repo.save_state(
                    adapter.id,
                    circuit.record_failure(fresh_state, now),
                    error_kind=result.status.value,
                    error_msg=result.error,
                )
        if result.status == ResultStatus.OK:
            await self._cache.set(
                cache_key, adapter.id, _serialize_result(result), "application/json", adapter.default_cache_ttl_s
            )

        return result

    async def health_check_all(self) -> dict[str, HealthStatus]:
        results: dict[str, HealthStatus] = {}
        for source_id, adapter in self._adapters.items():
            try:
                async with asyncio.timeout(adapter.request_timeout_s):
                    results[source_id] = await adapter.health_check()
            except Exception as exc:
                results[source_id] = HealthStatus(ok=False, detail=str(exc))
        return results

    @staticmethod
    def _cache_key(source_id: str, query: SearchQuery) -> str:
        dates = ",".join(d.isoformat() for d in query.dates)
        return f"{source_id}:{query.origin.place_id}:{query.dest.place_id}:{query.mode.value}:{dates}:{query.pax}"


def _resolve_timeout_s(adapter: SourceAdapter, deadline_s: float | None, lane: Lane) -> float:
    """How long one adapter call is allowed to take.

    This used to be `deadline_s or adapter.total_timeout_s`, i.e. any truthy caller deadline
    won outright and silently discarded whatever ceiling the adapter declared for itself. The
    scheduler's flat 90s was below several adapters' honest worst case (s7_direct needs 270s
    once a one-off browser solve is inside the budget), so a legitimate multi-date search
    regularly had its already-collected offers thrown away by the outer asyncio.timeout —
    not because anything failed, just because the clock ran out.

    Scheduler lane: a caller's deadline can only *raise* an adapter's ceiling, never push it
    below what the adapter says its own worst case costs. Nobody is waiting on a background
    poll, so truncating one is pure loss.

    Interactive lane: the exact opposite, deliberately. /search runs with a user watching the
    chat under `interactive_l1_deadline_s` (12s), where the deadline is a hard budget and a
    truncated answer beats a slow one. Confirmation-tier adapters *are* reachable from that
    path (`SearchOpts.tiers` includes CONFIRMATION there), so applying the floor to it would
    turn a 12-second search into a multi-minute one.
    """
    if deadline_s is None:
        return float(adapter.total_timeout_s)
    if lane == "interactive":
        return deadline_s
    return max(deadline_s, float(adapter.total_timeout_s))


def _serialize_result(result: SearchResult) -> bytes:
    payload = {
        "status": result.status.value,
        "partial": result.partial,
        "retry_after_s": result.retry_after_s,
        "error": result.error,
        "offers": [_offer_to_dict(o) for o in result.offers],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _deserialize_result(body: bytes) -> SearchResult:
    payload = json.loads(body.decode("utf-8"))
    return SearchResult(
        status=ResultStatus(payload["status"]),
        offers=[_offer_from_dict(o) for o in payload["offers"]],
        partial=payload["partial"],
        retry_after_s=payload.get("retry_after_s"),
        error=payload.get("error"),
    )


def _offer_to_dict(offer: Offer) -> dict[str, Any]:
    d = asdict(offer)
    d["tier"] = offer.tier.value
    d["observed_at"] = offer.observed_at.isoformat()
    d["source_cached_at"] = offer.source_cached_at.isoformat() if offer.source_cached_at else None
    d["expires_at"] = offer.expires_at.isoformat() if offer.expires_at else None
    for leg in d["legs"]:
        leg["mode"] = Mode(leg["mode"]).value if not isinstance(leg["mode"], str) else leg["mode"]
        leg["depart_utc"] = leg["depart_utc"] if isinstance(leg["depart_utc"], str) else leg["depart_utc"].isoformat()
        leg["depart_local"] = leg["depart_local"] if isinstance(leg["depart_local"], str) else leg["depart_local"].isoformat()
        arrive = leg["arrive_utc"]
        leg["arrive_utc"] = arrive if (arrive is None or isinstance(arrive, str)) else arrive.isoformat()
    return d


def _offer_from_dict(d: dict[str, Any]) -> Offer:
    legs = tuple(
        Leg(
            mode=Mode(leg["mode"]),
            from_place=PlaceRef(**leg["from_place"]),
            to_place=PlaceRef(**leg["to_place"]),
            depart_utc=datetime.fromisoformat(leg["depart_utc"]),
            depart_local=datetime.fromisoformat(leg["depart_local"]),
            carrier=leg.get("carrier"),
            number=leg.get("number"),
            arrive_utc=datetime.fromisoformat(leg["arrive_utc"]) if leg.get("arrive_utc") else None,
            tz=leg.get("tz"),
            fare_class=leg.get("fare_class"),
            time_precision=leg.get("time_precision", "exact"),
        )
        for leg in d["legs"]
    )
    return Offer(
        legs=legs,
        price_minor=d["price_minor"],
        currency=d["currency"],
        deep_link=d["deep_link"],
        source_id=d["source_id"],
        tier=Tier(d["tier"]),
        observed_at=datetime.fromisoformat(d["observed_at"]),
        source_cached_at=datetime.fromisoformat(d["source_cached_at"]) if d["source_cached_at"] else None,
        expires_at=datetime.fromisoformat(d["expires_at"]) if d["expires_at"] else None,
        confidence=d["confidence"],
        itinerary_hash=d["itinerary_hash"],
        duration_minutes=d.get("duration_minutes"),
    )
