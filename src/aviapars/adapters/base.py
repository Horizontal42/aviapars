from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Protocol


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
    city_id: int | None


@dataclass(frozen=True, slots=True)
class Leg:
    mode: Mode
    carrier: str
    number: str
    from_place: PlaceRef
    to_place: PlaceRef
    depart_utc: datetime
    arrive_utc: datetime
    depart_local: datetime
    tz: str
    fare_class: str | None = None


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
