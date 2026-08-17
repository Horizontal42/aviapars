from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from curl_cffi.requests import AsyncSession

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

SEARCH_URL = "https://www.aeroflot.ru/sbe/flight-search/api/web/v1/flight/search"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.aeroflot.ru",
    "Referer": "https://www.aeroflot.ru/ru-ru/sb/search",
    "X-Client-Type": "web-client",
}


class AeroflotDirectAdapter:
    """Confirmation-tier direct scrape of Aeroflot Group's shared booking search.

    No JS-challenge, no cookies, no session — verified live (2026-08-17) that a
    single stateless POST with Chrome TLS impersonation (curl_cffi) returns real
    fares; plain curl gets a 503 WAF block page, so the antibot here is TLS-
    fingerprint-only, same class as `rzd_direct`/`travelpayouts`, not the
    ServicePipe-style challenge that forces `s7_direct` to keep a browser
    fallback.

    Group-shared inventory is the whole point of this adapter: Aeroflot's own
    search surfaces flights it markets (`marketing_airline_code`) but that
    Rossiya or Pobeda actually operate (`operating_airline_code`) — verified
    live that a Saint Petersburg-Sochi search returned Rossiya (FV) as the
    sole operator on every itinerary. `Leg.carrier` is set from the operating
    airline, not the marketing one, since that's the carrier the passenger
    actually flies and the one this project's roster cares about tracking
    (see ARCHITECTURE.md — Rossiya has no separate scrapeable adapter of its
    own; rossiya-airlines.ru appears to be network-blocked for non-residential
    IPs regardless of TLS fingerprint, unlike this shared endpoint).
    """

    id = "aeroflot_direct"
    display_name = "Аэрофлот / Россия / Победа"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None  # group flies international routes too
    supports_batch_dates = False
    request_timeout_s = 20
    total_timeout_s = 30

    def __init__(self, *, rate_limit: RateLimitPolicy | None = None, cache_ttl_s: int = 1800) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=15.0)
        self.default_cache_ttl_s = cache_ttl_s

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        depart_date = q.dates[0]  # confirmation tier: one specific leg/date (plan §6 L2)
        body = {
            "routes": [
                {"origin": q.origin.iata, "destination": q.dest.iata, "departure_date": depart_date.isoformat()}
            ],
            "country": "RU",
            "lang": "ru",
            "adults": q.pax,
            "cabin": "economy",
            "children": 0,
            "childrenfrgn": 0,
            "infants": 0,
            "award": False,
            "coupons": [],
            "vouchers": [],
            "days": 1,
        }

        try:
            async with AsyncSession(impersonate="chrome", timeout=self.request_timeout_s) as session:
                resp = await session.post(SEARCH_URL, headers=_HEADERS, json=body)
        except Exception:
            return SearchResult(status=ResultStatus.UPSTREAM_ERROR, offers=[], partial=False, error="request failed")

        if resp.status_code == 503:
            return SearchResult(status=ResultStatus.BLOCKED, offers=[], partial=False, error="waf block")
        if resp.status_code != 200:
            return SearchResult(
                status=ResultStatus.UPSTREAM_ERROR, offers=[], partial=False, error=f"http {resp.status_code}"
            )
        try:
            payload = resp.json()
        except ValueError:
            return SearchResult(status=ResultStatus.PARSE_ERROR, offers=[], partial=False, error="bad json")

        offers = self._parse_offers(payload, q, depart_date)
        status = ResultStatus.OK if offers else ResultStatus.EMPTY
        return SearchResult(status=status, offers=offers, partial=False)

    def _parse_offers(self, payload: dict[str, Any], q: SearchQuery, depart_date: date) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)
        groups = (payload.get("data") or {}).get("route_itineraries") or []
        itineraries = groups[0] if groups else []

        for itinerary in itineraries:
            legs_raw = itinerary.get("legs") or []
            offer_options = itinerary.get("offers") or []
            if not legs_raw or not offer_options:
                continue

            cheapest = min(offer_options, key=lambda o: Decimal(o["price_total_amount"]))

            legs: list[Leg] = []
            for leg in legs_raw:
                for seg in leg.get("segments") or []:
                    legs.append(self._parse_segment(seg, q))
            if not legs:
                continue

            price = Decimal(cheapest["price_total_amount"])
            itinerary_hash = hashlib.sha256(
                f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                f"{legs[0].number}|{cheapest.get('brand_code')}".encode()
            ).hexdigest()[:16]

            offers.append(
                Offer(
                    legs=tuple(legs),
                    price_minor=round(price * 100),
                    currency=cheapest.get("currency_code", q.currency).upper(),
                    deep_link=None,
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

    def _parse_segment(self, seg: dict[str, Any], q: SearchQuery) -> Leg:
        dep_local = datetime.fromisoformat(seg["departure"])
        dep_utc = datetime.fromisoformat(seg["departure_utc"]).replace(tzinfo=timezone.utc)
        arr_utc = None
        if seg.get("arrival_utc"):
            arr_utc = datetime.fromisoformat(seg["arrival_utc"]).replace(tzinfo=timezone.utc)
        offset_min = seg.get("departure_offset")
        tz = _format_offset(offset_min) if offset_min is not None else None

        return Leg(
            mode=Mode.PLANE,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=dep_utc,
            depart_local=dep_local,
            carrier=seg.get("operating_airline_name") or seg.get("marketing_airline_name"),
            number=seg.get("operating_flight_number") or seg.get("marketing_flight_number"),
            arrive_utc=arr_utc,
            tz=tz,
            time_precision="exact",
        )

    async def health_check(self) -> HealthStatus:
        probe = SearchQuery(
            origin=PlaceRef(place_id=0, iata="LED", yandex_code=None, rzd_code=None, city_id=None),
            dest=PlaceRef(place_id=0, iata="SVO", yandex_code=None, rzd_code=None, city_id=None),
            dates=(date.today() + timedelta(days=14),),
            pax=1,
            mode=Mode.PLANE,
        )
        try:
            result = await self.search(probe)
            return HealthStatus(ok=result.status in (ResultStatus.OK, ResultStatus.EMPTY))
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))


def _format_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h:02d}:{m:02d}"
