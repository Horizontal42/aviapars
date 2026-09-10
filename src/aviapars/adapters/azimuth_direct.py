from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from curl_cffi.requests import AsyncSession

from aviapars.adapters.base import (
    CURL_FORCE_IPV4,
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
    build_search_result,
    probe_dates_total_timeout_s,
    with_dns_retry,
)

RPC_URL = "https://booking.azimuth.ru/rpc/tsh"
_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://booking.azimuth.ru",
    "Referer": "https://booking.azimuth.ru/new/",
}
_INIT_PARAMS = {
    "partner_id": "protoprod_desktop",
    "currency": "RUB",
    "site_id": "r2",
    "spa_mode": "N",
    "is_mobile_app": "N",
    "module": "booking",
}


class AzimuthDirectAdapter:
    """Confirmation-tier direct scrape of Azimuth's own JSON-RPC booking API
    (booking.azimuth.ru/rpc/tsh — Sirena-Travel-style `[["method", params], ...]`
    batch-call shape). No TLS/JS antibot at all (plain curl_cffi, no cookies) —
    the only gate is a lightweight `init` RPC call that returns a bearer token
    in the `Authorization` *response* header (not a cookie), which every
    following call on the same "session" must echo back as a request header.
    Verified live (2026-08-18): a fresh init + get-availability round trip on
    LED->MOW returned real fares with no session held between calls — init is
    called fresh every search rather than cached, since it's a single cheap
    extra request and avoids any expiry bookkeeping.
    """

    id = "azimuth_direct"
    display_name = "Азимут"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = frozenset({"RU"})  # regional Southern Russia carrier
    supports_batch_dates = False
    request_timeout_s = 15
    # one get-availability per probe date, preceded by a single `init` RPC for the bearer token
    total_timeout_s = probe_dates_total_timeout_s(request_timeout_s, setup_s=request_timeout_s)

    def __init__(self, *, rate_limit: RateLimitPolicy | None = None, cache_ttl_s: int = 1800) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=10.0)
        self.default_cache_ttl_s = cache_ttl_s

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        try:
            async with AsyncSession(
                impersonate="chrome", timeout=self.request_timeout_s, curl_options=CURL_FORCE_IPV4
            ) as session:
                init_resp = await with_dns_retry(
                    lambda: session.post(RPC_URL, headers=_HEADERS, json=[["init", _INIT_PARAMS]])
                )
                token = init_resp.headers.get("Authorization")
                if init_resp.status_code != 200 or not token:
                    return SearchResult(status=ResultStatus.BLOCKED, offers=[], partial=False, error="init failed")

                offers: list[Offer] = []
                blocked = False
                partial = False

                tasks = [self._search_day(session, q, token, depart_date) for depart_date in q.dates]
                results = await asyncio.gather(*tasks)

                for day_offers, outcome in results:
                    if outcome == "blocked":
                        blocked = True
                        continue
                    if outcome == "error":
                        partial = True
                        continue
                    offers.extend(day_offers)
        except Exception:
            # only the init/session setup itself lands here — _search_day catches its own
            # per-date exceptions so one bad date can't discard offers already collected
            # from the others in this same batch.
            return SearchResult(status=ResultStatus.UPSTREAM_ERROR, offers=[], partial=False, error="request failed")

        return build_search_result(offers, blocked=blocked, partial=partial)

    async def _search_day(
        self, session: Any, q: SearchQuery, token: str, depart_date: date
    ) -> tuple[list[Offer], str]:
        """Returns (offers, outcome) where outcome is "ok" (incl. legitimately empty),
        "blocked" (non-200), or "error" (parse failure) — mirrors rzd_direct's per-day
        result so one bad date in the window doesn't sink the whole cycle."""
        avail_headers = {**_HEADERS, "Authorization": token}
        avail_body = [
            [
                "get-availability",
                {
                    "owrt": "OW",
                    "departure_point": q.origin.iata,
                    "arrival_point": q.dest.iata,
                    "outbound_date": depart_date.strftime("%d.%m.%Y"),
                    "return_date": "",
                    "adult_count": str(q.pax),
                    "child_count": "0",
                    "infant_count": "0",
                    "direct_only": "",
                    "date_range": 1,
                    "promocode": "",
                    "return_full_names": "Y",
                    "child_alone": "",
                },
            ]
        ]
        try:
            resp = await with_dns_retry(lambda: session.post(RPC_URL, headers=avail_headers, json=avail_body))
        except Exception:
            return [], "error"
        if resp.status_code != 200:
            return [], "blocked"
        try:
            payload = resp.json()
        except ValueError:
            return [], "error"

        return self._parse_offers(payload, q, depart_date), "ok"

    def _parse_offers(self, payload: list[dict[str, Any]], q: SearchQuery, depart_date: date) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)

        call_result = next((c["result"] for c in payload if c.get("method") == "get-availability"), None)
        if not call_result:
            return offers
        directions = call_result.get("directions") or []

        for direction in directions:
            if direction.get("date") != depart_date.strftime("%d.%m.%Y"):
                continue
            for flight in direction.get("flights") or []:
                segments = flight.get("segments") or []
                flight_offers = flight.get("offers") or []
                if not segments or not flight_offers:
                    continue

                cheapest = min(flight_offers, key=lambda o: Decimal(o["price"]))

                legs: list[Leg] = []
                for seg in segments:
                    dep_local = datetime.strptime(seg["departure_date"] + " " + seg["departure_time"], "%d.%m.%Y %H:%M")
                    dep_utc = datetime.strptime(seg["departure_utc"], "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone.utc)
                    arr_utc = datetime.strptime(seg["arrival_utc"], "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone.utc)
                    arr_local = None
                    if seg.get("arrival_date") and seg.get("arrival_time"):
                        arr_local = datetime.strptime(seg["arrival_date"] + " " + seg["arrival_time"], "%d.%m.%Y %H:%M")
                    legs.append(
                        Leg(
                            mode=Mode.PLANE,
                            from_place=q.origin,
                            to_place=q.dest,
                            depart_utc=dep_utc,
                            depart_local=dep_local,
                            carrier=seg.get("ak_full_name"),
                            number=seg.get("flight_number"),
                            arrive_utc=arr_utc,
                            arrive_local=arr_local,
                            time_precision="exact",
                        )
                    )
                if not legs:
                    continue

                price = Decimal(cheapest["price"])
                itinerary_hash = hashlib.sha256(
                    f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                    f"{legs[0].number}|{cheapest.get('marketing_fare_code2')}".encode()
                ).hexdigest()[:16]

                offers.append(
                    Offer(
                        legs=tuple(legs),
                        price_minor=round(price * 100),
                        currency="RUB",
                        # not prefilled: no route-query-param format was ever captured in the
                        # HAR session (booking.azimuth.ru/new/ is an SPA with no visible route
                        # state in the URL) — the base search page is honest, a fabricated
                        # query string risking a broken link is not.
                        deep_link="https://booking.azimuth.ru/new/",
                        source_id=self.id,
                        tier=Tier.CONFIRMATION,
                        observed_at=now,
                        source_cached_at=now,
                        expires_at=None,
                        confidence=1.0,
                        itinerary_hash=itinerary_hash,
                    )
                )
        return offers

    async def health_check(self) -> HealthStatus:
        probe = SearchQuery(
            origin=PlaceRef(place_id=0, iata="LED", yandex_code=None, rzd_code=None, city_id=None),
            dest=PlaceRef(place_id=0, iata="MOW", yandex_code=None, rzd_code=None, city_id=None),
            dates=(date.today() + timedelta(days=14),),
            pax=1,
            mode=Mode.PLANE,
        )
        try:
            result = await self.search(probe)
            return HealthStatus(ok=result.status in (ResultStatus.OK, ResultStatus.EMPTY))
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
