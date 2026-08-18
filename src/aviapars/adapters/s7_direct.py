from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp
from curl_cffi.requests import AsyncSession

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
)
from aviapars.solver.client import BrowserSolverClient

LOCATIONS_URL = "https://locations.api.s7.ru/completion/completions/locations/airports/ru/{code}"
AIR_URL = "https://ibe.s7.ru/air"
LOCATIONS_HEADERS = {
    "Origin": "https://ibe.s7.ru",
    "Referer": "https://ibe.s7.ru/",
    "X-Client-Id": "ibe",
    "Accept": "*/*",
}
_EXEC_RE = re.compile(r'__r_execution\s*=\s*"([^"]+)"')
_CONV_RE = re.compile(r'__r_ibe_conversation\s*=\s*"([^"]+)"')
_CHALLENGE_MARKER = "js-challenge-loader"

SessionGetter = Callable[[], Awaitable[dict[str, Any] | None]]
SessionSetter = Callable[[dict[str, Any], datetime], Awaitable[None]]


class S7DirectAdapter:
    """Confirmation-tier direct scrape of S7's own booking search (ibe.s7.ru).

    Two-tier request strategy, cheapest first — never scheduled/rate-limited, only
    on-demand when the cheap path actually fails:
    1. Cheap path — plain curl_cffi request (Chrome TLS impersonation, no browser) using
       cached antibot cookies from a prior browser solve, if any. ibe.s7.ru fronts a
       ServicePipe JS-challenge; verified live that once cookies from a passed challenge
       are on hand, a *fresh* bootstrap request for a *new* search query sails through on
       cookies alone (the challenge is browser/session-trust scoped, not tied to one
       specific search's Spring Webflow conversation id — those ids are re-derived for
       free from the cheap-path bootstrap response on every call, never cached).
    2. Browser fallback — only when the cheap path's bootstrap response still shows the
       challenge marker (or there's no cached session yet): spawn an isolated
       patchright/chromium subprocess (BrowserSolverClient) purely to pass the challenge
       and harvest fresh cookies, then retry the cheap path once.

    Session cookies are cached via injected `session_getter`/`session_setter` callables
    (not a direct DB connection) — keeps this adapter stateless w.r.t. DB per §5 rule 3,
    while still surviving process restarts when the caller wires them to a real repo.
    """

    id = "s7_direct"
    display_name = "S7 Airlines"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None  # S7 flies international routes too, no restriction (§5)
    supports_batch_dates = False
    request_timeout_s = 20
    total_timeout_s = 60

    def __init__(
        self,
        *,
        session_getter: SessionGetter | None = None,
        session_setter: SessionSetter | None = None,
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 1800,
        solver_timeout_s: float = 90,
        session_ttl_s: int = 1500,
    ) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=15.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._session_getter = session_getter
        self._session_setter = session_setter
        self._solver = BrowserSolverClient(timeout_s=solver_timeout_s)
        self._session_ttl_s = session_ttl_s
        self._location_id_cache: dict[str, str] = {}

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        async with aiohttp.ClientSession() as loc_session:
            origin_id = await self._resolve_location_id(loc_session, q.origin.iata)
            dest_id = await self._resolve_location_id(loc_session, q.dest.iata)
        if origin_id is None or dest_id is None:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="location not found")

        cookies = await self._get_cached_cookies()
        offers: list[Offer] = []
        blocked = False
        solver_tried = False
        for depart_date in q.dates:
            day_offers, ok = await self._try_search(q, depart_date, origin_id, dest_id, cookies)
            if not ok and not solver_tried:
                # Only ever solve once per search() call — the challenge is session/browser
                # scoped, not per-date (see class docstring), and a fresh browser subprocess
                # per date in a 7-date cycle would be both slow and pointless.
                solver_tried = True
                solved = await self._solver.solve("s7", {"AA1": q.dest.iata, "DA1": q.origin.iata, "CUR": "RUB"})
                if solved is None:
                    blocked = True
                    break  # persistent block — further dates would fail the same way
                cookies = solved["cookies"]
                await self._save_session(cookies)
                day_offers, ok = await self._try_search(q, depart_date, origin_id, dest_id, cookies)
            if not ok:
                blocked = True
                break
            offers.extend(day_offers)

        if offers:
            status = ResultStatus.OK
        elif blocked:
            status = ResultStatus.BLOCKED
        else:
            status = ResultStatus.EMPTY
        return SearchResult(status=status, offers=offers, partial=False)

    async def _get_cached_cookies(self) -> dict[str, str] | None:
        if self._session_getter is None:
            return None
        payload = await self._session_getter()
        if payload is None:
            return None
        cookies: dict[str, str] | None = payload.get("cookies")
        return cookies or None

    async def _save_session(self, cookies: dict[str, str]) -> None:
        if self._session_setter is None:
            return
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._session_ttl_s)
        await self._session_setter({"cookies": cookies}, expires_at)

    async def _resolve_location_id(self, session: aiohttp.ClientSession, iata: str) -> str | None:
        cached = self._location_id_cache.get(iata)
        if cached is not None:
            return cached
        url = LOCATIONS_URL.format(code=iata)
        try:
            async with session.get(
                url, headers=LOCATIONS_HEADERS, timeout=aiohttp.ClientTimeout(total=self.request_timeout_s)
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError):
            return None

        items = (payload.get("completion") or {}).get("items") or []
        for item in items:
            if item.get("code") == iata:
                loc_id: str = item["id"]
                self._location_id_cache[iata] = loc_id
                return loc_id
            for child in item.get("children") or []:
                if child.get("code") == iata:
                    loc_id = child["id"]
                    self._location_id_cache[iata] = loc_id
                    return loc_id
        return None

    async def _try_search(
        self,
        q: SearchQuery,
        depart_date: date,
        origin_id: str,
        dest_id: str,
        cookies: dict[str, str] | None,
    ) -> tuple[list[Offer], bool]:
        """Returns (offers, ok). ok=False means the antibot challenge blocked this
        attempt (caller falls back to the browser solver) — distinct from ok=True with
        offers=[], which is a legitimate empty search result."""
        session_kwargs: dict[str, Any] = {"impersonate": "chrome", "timeout": self.request_timeout_s}
        if cookies:
            session_kwargs["cookies"] = cookies

        async with AsyncSession(**session_kwargs) as session:
            params = {
                "id": "deeplink",
                "RDMPTN": "false",
                "journeySpan": "OW",
                "TY": "0",
                "AA1": q.dest.iata,
                "DA1": q.origin.iata,
                "CUR": q.currency,
                "TA": str(q.pax),
                "TC": "0",
                "DD1": depart_date.isoformat(),
                "LAN": "ru",
                "FSC1": "1",
                "FLC": "1",
                "DLI1": origin_id,
                "ALI1": dest_id,
            }
            # id=deeplink is S7's own external-referrer link mechanism — this exact URL
            # (no live session needed) is what we GET below to bootstrap a search, so it's
            # already verified to open a real prefilled search on ibe.s7.ru.
            deep_link = f"{AIR_URL}?{urlencode(params)}"
            try:
                resp = await session.get(AIR_URL, params=params)
            except Exception:
                return [], False
            text = resp.text
            if resp.status_code != 200 or _CHALLENGE_MARKER in text:
                return [], False

            m_exec = _EXEC_RE.search(text)
            m_conv = _CONV_RE.search(text)
            if not m_exec or not m_conv:
                return [], False
            execution, conversation = m_exec.group(1), m_conv.group(1)

            form_data = {
                "_eventId": "getExtendedSearchResult",
                "clientId": "ibe",
                "execution": execution,
                "ibe_conversation": conversation,
            }
            try:
                resp2 = await session.post(
                    AIR_URL,
                    params={"execution": execution, "ibe_conversation": conversation},
                    data=form_data,
                )
            except Exception:
                return [], False
            if resp2.status_code != 200:
                return [], False
            try:
                payload = resp2.json()
            except ValueError:
                return [], False

            return self._parse_offers(payload, q, depart_date, deep_link), True

    def _parse_offers(
        self, payload: dict[str, Any], q: SearchQuery, depart_date: date, deep_link: str | None = None
    ) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)

        for option_set in payload.get("optionSets") or []:
            for flight_option in option_set.get("flightOptions") or []:
                route = flight_option.get("route") or {}
                segments = route.get("segments") or []
                if not segments:
                    continue

                solutions = flight_option.get("solutions") or {}
                available_solutions = [s for s in solutions.values() if s.get("available")]
                if not available_solutions:
                    continue
                cheapest = min(
                    available_solutions,
                    key=lambda s: s["pricing"]["total"]["salePrice"]["amount"],
                )
                price = cheapest["pricing"]["total"]["salePrice"]

                legs: list[Leg] = []
                for seg in segments:
                    # departureDate/arrivalDate are naive local clock time; the response
                    # gives the real IANA zone per endpoint (verified live: not always the
                    # same zone at both ends), so each side gets localized separately before
                    # conversion — treating either as UTC directly crashed on comparison
                    # against any tz-aware datetime elsewhere in the pipeline (freshness_gate).
                    dep_tz_name = seg.get("departureTimeZone")
                    arr_tz_name = seg.get("arrivalTimeZone")
                    dep_local = datetime.fromisoformat(seg["departureDate"])
                    dep_utc = dep_local.replace(tzinfo=ZoneInfo(dep_tz_name) if dep_tz_name else timezone.utc)
                    dep_utc = dep_utc.astimezone(timezone.utc)
                    arr_utc = None
                    if seg.get("arrivalDate"):
                        arr_local = datetime.fromisoformat(seg["arrivalDate"])
                        arr_utc = arr_local.replace(tzinfo=ZoneInfo(arr_tz_name) if arr_tz_name else timezone.utc)
                        arr_utc = arr_utc.astimezone(timezone.utc)
                    airline = seg.get("operatingAirline") or {}
                    legs.append(
                        Leg(
                            mode=Mode.PLANE,
                            from_place=q.origin,
                            to_place=q.dest,
                            depart_utc=dep_utc,
                            depart_local=dep_local,
                            carrier=airline.get("name"),
                            number=airline.get("flightNumber"),
                            arrive_utc=arr_utc,
                            tz=dep_tz_name,
                            time_precision="exact",
                        )
                    )

                duration = (route.get("duration") or {}).get("amountInMinutes")
                itinerary_hash = hashlib.sha256(
                    f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                    f"{legs[0].number}|{cheapest.get('fareFamily')}".encode()
                ).hexdigest()[:16]

                offers.append(
                    Offer(
                        legs=tuple(legs),
                        price_minor=round(price["amount"] * 100),
                        currency=price.get("currency", q.currency).upper(),
                        deep_link=deep_link,
                        source_id=self.id,
                        tier=Tier.CONFIRMATION,
                        observed_at=now,
                        source_cached_at=now,
                        expires_at=None,
                        confidence=1.0,
                        itinerary_hash=itinerary_hash,
                        duration_minutes=duration,
                    )
                )
        return offers

    async def health_check(self) -> HealthStatus:
        try:
            async with aiohttp.ClientSession() as session:
                loc = await self._resolve_location_id(session, "MOW")
            return HealthStatus(ok=loc is not None)
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
