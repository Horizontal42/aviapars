from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Awaitable, Callable, Protocol, TypeVar


class Tier(Enum):
    DISCOVERY = "discovery"
    CONFIRMATION = "confirmation"


class Mode(Enum):
    PLANE = "plane"
    TRAIN = "train"
    # BUS lands here when a bus adapter does — see ARCHITECTURE.md "Adding a mode".


class ResultStatus(Enum):
    OK = "ok"
    EMPTY = "empty"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    AUTH_ERROR = "auth_error"
    BLOCKED = "blocked"
    UPSTREAM_ERROR = "upstream_error"
    CACHE_MISS = "cache_miss"


@dataclass(frozen=True, slots=True)
class PlaceRef:
    place_id: int
    iata: str | None
    yandex_code: str | None
    rzd_code: str | None
    city_id: int | None
    # Carried through so result rendering can show a human-readable place
    # instead of a bare 3-letter code — adapters never read this themselves,
    # they only pass q.origin/q.dest through to Leg.from_place/to_place.
    name_ru: str | None = None


@dataclass(frozen=True, slots=True)
class Leg:
    """`carrier`/`number`/`arrive_utc`/`tz` are None for discovery-tier sources
    that only give a route+date price signal, not a real flight schedule (the
    Travelpayouts month-matrix endpoint is exactly this — verified live in
    Phase 0: it returns depart_date/value/number_of_changes/duration, no
    airline or flight number at all). `time_precision` makes that honest and
    machine-readable rather than silently fabricating a time or carrier.
    `depart_utc`/`depart_local` are still required — for date_only legs they
    hold midnight of `depart_date` as a placeholder, which callers must treat
    as date-precision only when `time_precision == "date_only"`.
    """

    mode: Mode
    from_place: PlaceRef
    to_place: PlaceRef
    depart_utc: datetime
    depart_local: datetime
    carrier: str | None = None
    number: str | None = None
    arrive_utc: datetime | None = None
    tz: str | None = None
    fare_class: str | None = None
    time_precision: str = "exact"  # "exact" | "date_only"


@dataclass(frozen=True, slots=True)
class Offer:
    legs: tuple[Leg, ...]
    price_minor: int
    currency: str
    deep_link: str | None
    source_id: str
    tier: Tier
    observed_at: datetime
    source_cached_at: datetime | None
    expires_at: datetime | None
    confidence: float
    itinerary_hash: str
    duration_minutes: int | None = None  # total trip duration when known but not derivable
    # from leg times alone (date_only legs have no arrive_utc to subtract).


@dataclass(frozen=True, slots=True)
class SearchQuery:
    origin: PlaceRef
    dest: PlaceRef
    dates: tuple[date, ...]
    pax: int
    mode: Mode
    currency: str = "RUB"


@dataclass(frozen=True, slots=True)
class SearchResult:
    status: ResultStatus
    offers: list[Offer]
    partial: bool
    retry_after_s: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    min_interval_s: float
    daily_cap: int | None = None


def probe_dates_total_timeout_s(per_date_s: float, *, setup_s: float = 0.0) -> int:
    """Honest `total_timeout_s` for an adapter that spends one sequential request
    per probe date inside a single `search()` call (`supports_batch_dates = False`).

    Worst case is `per_date_s` x however many dates one cycle probes, plus whatever
    one-off setup the adapter pays before the loop (a location/city id lookup, an
    `init` RPC, S7's browser solve). Derived from `flex_probe_dates_per_cycle`
    rather than hardcoded so the two can't drift — that drift is exactly what left
    every confirmation adapter declaring a flat 60s for work whose own docstring
    admitted it could need 7 sequential requests.

    These are ceilings for a source that hangs, not expected durations; a source
    that actually behaves that way trips the circuit breaker after 5 tries and
    then costs nothing at all until it recovers.
    """
    from aviapars.config import settings  # local: keeps the adapter contract itself config-free

    return int(per_date_s * settings.flex_probe_dates_per_cycle + setup_s)


_T = TypeVar("_T")


async def with_dns_retry(call: Callable[[], Awaitable[_T]], *, retries: int = 1) -> _T:
    """One immediate retry around a single curl_cffi request.

    Live-verified on the production VDS (2026-08-19): repeating the exact same
    request to an already-succeeding host through curl_cffi's own async DNS
    resolver failed roughly half the time with "Resolving timed out" / "Could
    not resolve host", while the OS resolver (`getent hosts`) succeeded every
    single time in the same window — this is curl_cffi's resolver being flaky
    on this host, not a real outage or a blocked domain. It was silently
    eating every curl_cffi-based confirmation adapter's success rate (matches
    the "26 errors in a row" pattern reported for tutu_avia_direct). A retry
    close to a real ~50% per-attempt failure rate recovers the large majority
    of these; the registry's own circuit breaker still trips normally if both
    attempts fail, so a genuine outage isn't masked, just not double-counted
    off a transient resolver hiccup.
    """
    try:
        return await call()
    except Exception:
        if retries <= 0:
            raise
        return await call()


class SourceAdapter(Protocol):
    """See plans/squishy-scribbling-dawn.md §5 for the full contract rationale.

    Non-negotiable rules (enforced partly here, partly by the registry wrapper):
    1. search() never raises — failures become SearchResult(status=..., offers=[]).
    2. The registry wraps every call in asyncio.timeout() regardless of what the
       adapter itself does.
    3. Adapters are stateless w.r.t. DB/Telegram — testable against recorded fixtures.
    4. Every Offer carries honest freshness metadata; no age signal => low confidence,
       never gates a notification alone.
    5. Rate limiting, backoff, circuit breaking and caching live in the registry
       wrapper, not here — adapters only declare `rate_limit`.
    6. One dead source degrades gracefully; the registry is responsible for that,
       not the adapter.
    """

    id: str
    display_name: str
    modes: frozenset[Mode]
    tier: Tier
    countries: frozenset[str] | None  # None = no declared restriction (default)
    supports_batch_dates: bool
    rate_limit: RateLimitPolicy
    default_cache_ttl_s: int
    request_timeout_s: int
    total_timeout_s: int

    async def search(self, q: SearchQuery) -> SearchResult: ...

    async def health_check(self) -> HealthStatus: ...
