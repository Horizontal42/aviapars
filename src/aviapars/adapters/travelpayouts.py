from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, timezone
from typing import Any

import aiohttp

from aviapars.adapters.base import (
    HealthStatus,
    Leg,
    Mode,
    Offer,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    build_search_result,
)

BASE_URL = "https://api.travelpayouts.com/v2/prices/month-matrix"


class TravelpayoutsAdapter:
    """Discovery-tier flight prices via Travelpayouts' month-matrix endpoint.

    Verified live against the real API in Phase 0 (2026-08-17): the response
    gives depart_date/value/found_at/actual/number_of_changes/duration — no
    airline, no flight number, no arrival time. There is no `expires_at`
    field on this endpoint (that belongs to a different Travelpayouts
    endpoint this plan doesn't use) — see plans/squishy-scribbling-dawn.md §7
    for the freshness-gate implications.

    Rows with number_of_changes > 0 are dropped: we have no idea which hub
    or which second carrier is involved, and fabricating a connection would
    violate the project's no-invented-data rule. Connecting itineraries are
    the combiner's job (§8), built from real per-leg searches — this adapter
    only ever emits direct-flight offers.
    """

    id = "travelpayouts"
    display_name = "Travelpayouts (Aviasales Data API)"
    modes = frozenset({Mode.PLANE})
    tier = Tier.DISCOVERY
    countries: frozenset[str] | None = None  # global coverage, no restriction (§5)
    supports_batch_dates = True
    request_timeout_s = 15
    total_timeout_s = 45

    def __init__(self, token: str, marker: str, *, rate_limit: RateLimitPolicy | None = None, cache_ttl_s: int = 10800) -> None:
        self._token = token
        self._marker = marker
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=6.0)
        self.default_cache_ttl_s = cache_ttl_s

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing IATA code")

        wanted_dates = set(q.dates) if q.dates else None
        months = sorted({d.replace(day=1) for d in q.dates}) if q.dates else [date.today().replace(day=1)]

        offers: list[Offer] = []
        partial = False
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._fetch_month(session, q.origin.iata, q.dest.iata, month, q.currency)
                for month in months
            ]
            results = await asyncio.gather(*tasks)

            for rows, ok in results:
                if not ok:
                    partial = True
                    continue
                for row in rows:
                    if row.get("number_of_changes", 0) != 0:
                        continue
                    if not row.get("actual", True):
                        continue
                    row_date = date.fromisoformat(row["depart_date"])
                    if wanted_dates is not None and row_date not in wanted_dates:
                        continue
                    offers.append(self._row_to_offer(row, q))

        return build_search_result(offers, partial=partial)

    async def _fetch_month(
        self, session: aiohttp.ClientSession, origin: str, dest: str, month: date, currency: str
    ) -> tuple[list[dict[str, Any]], bool]:
        params = {
            "currency": currency.lower(),
            "origin": origin,
            "destination": dest,
            "month": month.isoformat(),
            "show_to_affiliates": "true",
            "token": self._token,
        }
        try:
            async with session.get(
                BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=self.request_timeout_s)
            ) as resp:
                if resp.status != 200:
                    return [], False
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError):
            return [], False

        if not payload.get("success"):
            return [], False
        return payload.get("data", []), True

    def _row_to_offer(self, row: dict[str, Any], q: SearchQuery) -> Offer:
        # search() already rejected the query if either IATA code was missing (§5 rule 1
        # spirit — fail fast before doing any work), so these are never None here.
        assert q.origin.iata is not None and q.dest.iata is not None
        depart_date = date.fromisoformat(row["depart_date"])
        depart_midnight = datetime(depart_date.year, depart_date.month, depart_date.day, tzinfo=timezone.utc)
        found_at = datetime.fromisoformat(row["found_at"])
        now = datetime.now(timezone.utc)

        leg = Leg(
            mode=Mode.PLANE,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=depart_midnight,
            depart_local=depart_midnight,
            time_precision="date_only",
        )
        itinerary_hash = hashlib.sha256(
            f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|direct".encode()
        ).hexdigest()[:16]

        return Offer(
            legs=(leg,),
            price_minor=round(row["value"] * 100),
            currency=q.currency.upper(),
            deep_link=self._deep_link(q.origin.iata, q.dest.iata, depart_date),
            source_id=self.id,
            tier=Tier.DISCOVERY,
            observed_at=now,
            source_cached_at=found_at,
            expires_at=None,  # not provided by month-matrix — freshness gate uses source_cached_at age instead
            confidence=1.0,  # validator.freshness_gate downgrades this by age
            itinerary_hash=itinerary_hash,
            duration_minutes=row.get("duration"),
        )

    def _deep_link(self, origin: str, dest: str, depart_date: date) -> str:
        ddmm = depart_date.strftime("%d%m")
        return f"https://www.aviasales.com/search/{origin}{ddmm}{dest}1?marker={self._marker}"

    async def health_check(self) -> HealthStatus:
        try:
            async with aiohttp.ClientSession() as session:
                rows, ok = await self._fetch_month(session, "MOW", "LED", date.today().replace(day=1), "RUB")
            return HealthStatus(ok=ok)
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
