from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from zoneinfo import ZoneInfo

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
    probe_dates_total_timeout_s,
    with_dns_retry,
)

SEARCH_URL = "https://ticket.flypobeda.ru/websky/json/search-variants-mono-brand-cartesian"
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://ticket.flypobeda.ru",
    "Referer": "https://ticket.flypobeda.ru/websky/",
}

# The search response gives local departure/arrival clock time per airport with no UTC
# offset at all — treating it as UTC directly would misplace non-Moscow flights by hours
# (e.g. SVX is UTC+5). Covers the major RU airports this roster is likely to query;
# falls back to Moscow time for anything unlisted, same honest-simplification precedent
# already used in rzd_direct.py rather than guessing per-station.
_AIRPORT_TZ = {
    "LED": "Europe/Moscow", "SVO": "Europe/Moscow", "DME": "Europe/Moscow", "VKO": "Europe/Moscow",
    "KRR": "Europe/Moscow", "AER": "Europe/Moscow", "ROV": "Europe/Moscow", "KGD": "Europe/Kaliningrad",
    "MMK": "Europe/Moscow", "ARH": "Europe/Moscow", "VOG": "Europe/Volgograd", "MRV": "Europe/Moscow",
    "SVX": "Asia/Yekaterinburg", "PEE": "Asia/Yekaterinburg", "UFA": "Asia/Yekaterinburg",
    "CEK": "Asia/Yekaterinburg", "TJM": "Asia/Yekaterinburg", "SGC": "Asia/Yekaterinburg",
    "OMS": "Asia/Omsk", "NJC": "Asia/Omsk",
    "OVB": "Asia/Novosibirsk", "KEJ": "Asia/Novosibirsk", "BAX": "Asia/Novosibirsk",
    "KJA": "Asia/Krasnoyarsk", "NOZ": "Asia/Krasnoyarsk",
    "IKT": "Asia/Irkutsk", "UUD": "Asia/Irkutsk",
    "UUS": "Asia/Sakhalin", "VVO": "Asia/Vladivostok", "KHV": "Asia/Vladivostok",
    "PKC": "Asia/Kamchatka", "GDX": "Asia/Magadan",
    "KZN": "Europe/Moscow", "GOJ": "Europe/Moscow", "SCW": "Europe/Moscow",
}
_DEFAULT_TZ = "Europe/Moscow"


def _airport_tz(code: str | None) -> ZoneInfo:
    return ZoneInfo(_AIRPORT_TZ.get(code or "", _DEFAULT_TZ))


def _deep_link(q: SearchQuery, depart_date: date) -> str:
    """Same query shape as a real user's search — captured verbatim from the
    site's own page URL in a live browser session (ticket.flypobeda.ru.har)."""
    params = [
        ("origin-city-code[0]", q.origin.iata),
        ("destination-city-code[0]", q.dest.iata),
        ("date[0]", depart_date.strftime("%d.%m.%Y")),
        ("segmentsCount", "1"),
        ("adultsCount", str(q.pax)),
        ("youngAdultsCount", "0"),
        ("childrenCount", "0"),
        ("infantsWithSeatCount", "0"),
        ("infantsWithoutSeatCount", "0"),
        ("lang", "ru"),
    ]
    return f"https://ticket.flypobeda.ru/websky/?{urlencode(params)}"


class PobedaDirectAdapter:
    """Confirmation-tier direct scrape of Pobeda's own WebSky booking search.

    Unlike `aeroflot_direct`/`azimuth_direct`, TLS impersonation does nothing here —
    Pobeda's WAF returns an explicit "your IP failed verification, if you're using a
    VPN turn it off" page for *any* datacenter or commercial-VPN-range IP, verified
    live against the VDS itself and three different VPN exits (RU/DE/US), all
    identically blocked. What actually gets through is a residential IP: routing this
    one adapter's traffic over a SOCKS5 relay reachable through the admin's own home
    Tailscale connection (see ARCHITECTURE.md) — a real residential ISP address, not a
    VPN/hosting range, so it doesn't trip the same detector. `proxy_url` is this
    adapter's own optional egress override; every other adapter in the roster stays
    direct, same as the bot's Telegram-only `telegram_proxy_url` — the two proxies
    exist for unrelated reasons and are not the same connection.

    Once past that gate the API itself has no further antibot: no cookies, no
    session, a single stateless POST returns real fares.
    """

    id = "pobeda_direct"
    display_name = "Победа"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None
    supports_batch_dates = False
    request_timeout_s = 20
    # one POST per probe date, nothing before the loop
    total_timeout_s = probe_dates_total_timeout_s(request_timeout_s)

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 1800,
    ) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=15.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._proxy_url = proxy_url

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        session_kwargs: dict[str, Any] = {"impersonate": "chrome", "timeout": self.request_timeout_s}
        if self._proxy_url:
            session_kwargs["proxy"] = self._proxy_url

        offers: list[Offer] = []
        blocked = False
        partial = False
        async with AsyncSession(**session_kwargs) as session:
            for depart_date in q.dates:
                day_offers, outcome = await self._search_day(session, q, depart_date)
                if outcome == "blocked":
                    blocked = True
                    continue
                if outcome == "error":
                    partial = True
                    continue
                offers.extend(day_offers)

        if offers:
            status = ResultStatus.OK
        elif blocked:
            status = ResultStatus.BLOCKED
        elif partial:
            status = ResultStatus.UPSTREAM_ERROR
        else:
            status = ResultStatus.EMPTY
        return SearchResult(status=status, offers=offers, partial=partial)

    async def _search_day(self, session: Any, q: SearchQuery, depart_date: date) -> tuple[list[Offer], str]:
        """Returns (offers, outcome) where outcome is "ok" (incl. legitimately empty),
        "blocked" (403), or "error" (network/parse failure) — mirrors rzd_direct's per-day
        result so one bad date in the window doesn't sink the whole cycle."""
        body = {
            "searchGroupId": "standard",
            "segmentsCount": "1",
            "date[0]": depart_date.strftime("%d.%m.%Y"),
            "origin-city-code[0]": q.origin.iata,
            "destination-city-code[0]": q.dest.iata,
            "adultsCount": str(q.pax),
            "youngAdultsCount": "0",
            "childrenCount": "0",
            "infantsWithSeatCount": "0",
            "infantsWithoutSeatCount": "0",
        }
        try:
            resp = await with_dns_retry(lambda: session.post(SEARCH_URL, headers=_HEADERS, data=body))
        except Exception:
            return [], "error"

        if resp.status_code == 403:
            return [], "blocked"
        if resp.status_code != 200:
            return [], "error"
        try:
            payload = resp.json()
        except ValueError:
            return [], "error"

        if payload.get("result") != "ok":
            return [], "ok"  # legitimate "nothing found" — not a failure

        return self._parse_offers(payload, q, depart_date), "ok"

    def _parse_offers(self, payload: dict[str, Any], q: SearchQuery, depart_date: date) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)
        prices_by_chain: dict[str, list[dict[str, Any]]] = (payload.get("prices") or [{}])[0]

        for chain in payload.get("flights") or []:
            chain_id = chain.get("chainId")
            fares = prices_by_chain.get(chain_id) or []
            flight_legs = chain.get("flights") or []
            if not fares or not flight_legs:
                continue

            cheapest = min(fares, key=lambda f: Decimal(f["price"]))

            legs: list[Leg] = []
            for flight in flight_legs:
                dep_local = datetime.strptime(
                    flight["departuredate"] + " " + flight["departuretime"], "%d.%m.%Y %H:%M"
                )
                arr_local = datetime.strptime(flight["arrivaldate"] + " " + flight["arrivaltime"], "%d.%m.%Y %H:%M")
                dep_utc = dep_local.replace(tzinfo=_airport_tz(flight.get("originport"))).astimezone(timezone.utc)
                arr_utc = arr_local.replace(tzinfo=_airport_tz(flight.get("destinationport"))).astimezone(
                    timezone.utc
                )
                legs.append(
                    Leg(
                        mode=Mode.PLANE,
                        from_place=q.origin,
                        to_place=q.dest,
                        depart_utc=dep_utc,
                        depart_local=dep_local,
                        carrier=(flight.get("company") or {}).get("name_en"),
                        number=flight.get("racenumber"),
                        arrive_utc=arr_utc,
                        time_precision="exact",
                    )
                )
            if not legs:
                continue

            price = Decimal(cheapest["price"])
            itinerary_hash = hashlib.sha256(
                f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                f"{legs[0].number}|{cheapest.get('brand')}".encode()
            ).hexdigest()[:16]

            offers.append(
                Offer(
                    legs=tuple(legs),
                    price_minor=round(price * 100),
                    currency=cheapest.get("currency", q.currency).upper(),
                    deep_link=_deep_link(q, depart_date),
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
            dest=PlaceRef(place_id=0, iata="SVX", yandex_code=None, rzd_code=None, city_id=None),
            dates=(date.today() + timedelta(days=14),),
            pax=1,
            mode=Mode.PLANE,
        )
        try:
            result = await self.search(probe)
            return HealthStatus(ok=result.status in (ResultStatus.OK, ResultStatus.EMPTY))
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
