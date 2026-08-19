from __future__ import annotations

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
    probe_dates_total_timeout_s,
)

BASE_URL = "https://api.rasp.yandex-net.ru/v3.0/search/"


class YandexRaspAdapter:
    """Train prices via Яндекс.Расписания's official, documented v3 search
    endpoint (https://yandex.ru/dev/rasp/doc/ru/reference/schedule-point-point).
    Unlike Travelpayouts, this is a live query each time — not a crowd-sourced
    cache — so `source_cached_at` is set to the fetch time itself rather than
    left None (§7's freshness gate treats "no age signal" as reject-outright
    for discovery-tier, which would be wrong here: we genuinely do know the
    age, it's ~0).

    `tickets_info` (and therefore price) is not guaranteed present on every
    segment — segments without it are skipped rather than guessed at.
    """

    id = "yandex_rasp"
    display_name = "Яндекс.Расписания"
    modes = frozenset({Mode.TRAIN})
    tier = Tier.DISCOVERY
    countries: frozenset[str] | None = frozenset({"RU", "BY", "KZ", "UA", "UZ"})  # §5: region-locked, declare it
    supports_batch_dates = False  # one date per request — no month-matrix equivalent here
    request_timeout_s = 15
    # one GET per probe date — supports_batch_dates is False, so the same 7-date loop applies
    total_timeout_s = probe_dates_total_timeout_s(request_timeout_s)

    def __init__(self, api_key: str, *, rate_limit: RateLimitPolicy | None = None, cache_ttl_s: int = 7200) -> None:
        self._api_key = api_key
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=2.0)
        self.default_cache_ttl_s = cache_ttl_s

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.TRAIN:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.yandex_code or not q.dest.yandex_code:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing yandex_code")

        dates = q.dates or (date.today(),)
        offers: list[Offer] = []
        partial = False
        async with aiohttp.ClientSession() as session:
            for d in dates:
                segments, ok = await self._fetch_day(session, q.origin.yandex_code, q.dest.yandex_code, d)
                if not ok:
                    partial = True
                    continue
                for segment in segments:
                    offer = self._segment_to_offer(segment, q)
                    if offer is not None:
                        offers.append(offer)

        return build_search_result(offers, partial=partial)

    async def _fetch_day(
        self, session: aiohttp.ClientSession, origin_code: str, dest_code: str, day: date
    ) -> tuple[list[dict[str, Any]], bool]:
        params = {
            "from": origin_code,
            "to": dest_code,
            "date": day.isoformat(),
            "transport_types": "train",
            "format": "json",
            "limit": "100",
            "apikey": self._api_key,
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
        segments = payload.get("segments")
        if segments is None:
            return [], False
        return segments, True

    def _segment_to_offer(self, segment: dict[str, Any], q: SearchQuery) -> Offer | None:
        tickets = segment.get("tickets_info")
        places = (tickets or {}).get("places") or []
        if not places:
            return None  # no price on this segment — nothing honest to report

        cheapest = min(places, key=lambda p: p["price"]["whole"])
        price = cheapest["price"]
        price_minor = price["whole"] * 100 + price.get("cents", 0)
        currency = cheapest.get("currency", "RUB").upper()

        depart = datetime.fromisoformat(segment["departure"])
        arrive_raw = segment.get("arrival")
        arrive = datetime.fromisoformat(arrive_raw) if arrive_raw else None

        thread = segment.get("thread") or {}
        carrier = (thread.get("carrier") or {}).get("title")
        number = thread.get("number")

        now = datetime.now(timezone.utc)
        leg = Leg(
            mode=Mode.TRAIN,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=depart.astimezone(timezone.utc),
            depart_local=depart,
            carrier=carrier,
            number=number,
            arrive_utc=arrive.astimezone(timezone.utc) if arrive else None,
            tz=str(depart.tzinfo),
            time_precision="exact",
        )
        duration = int((arrive - depart).total_seconds() // 60) if arrive else None
        itinerary_hash = hashlib.sha256(
            f"{self.id}|{q.origin.yandex_code}|{q.dest.yandex_code}|{number}|{segment['departure']}".encode()
        ).hexdigest()[:16]

        return Offer(
            legs=(leg,),
            price_minor=price_minor,
            currency=currency,
            deep_link=None,
            source_id=self.id,
            tier=Tier.DISCOVERY,
            observed_at=now,
            source_cached_at=now,  # live query, not a crowd cache — age is genuinely ~0, see class docstring
            expires_at=None,
            confidence=1.0,
            itinerary_hash=itinerary_hash,
            duration_minutes=duration,
        )

    async def health_check(self) -> HealthStatus:
        try:
            async with aiohttp.ClientSession() as session:
                _, ok = await self._fetch_day(session, "c213", "c2", date.today())  # Moscow -> SPb
            return HealthStatus(ok=ok)
        except Exception as exc:
            return HealthStatus(ok=False, detail=str(exc))
