from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Awaitable, Callable, Iterable, Protocol, TypeVar

from curl_cffi.const import CurlIpResolve, CurlOpt
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import DNSError, ProxyError, Timeout

from aviapars.adapters.circuit import BACKOFF_STEPS_S, utcnow


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
    # Arrival in the *destination's* own local time, which `arrive_utc` + `tz` cannot
    # reconstruct: `tz` is the departure offset, and a leg that crosses zones arrives on a
    # different one. Only a source that states the local arrival itself can fill this, so
    # rendering shows an arrival time exactly when it is present rather than deriving one.
    arrive_local: datetime | None = None
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


def leg_duration_minutes(leg: Leg) -> int | None:
    """Gate-to-gate for one leg, from timestamps rather than any source's own figure.

    UTC on both ends on purpose: it needs no timezone knowledge and is therefore right
    even for a leg that crosses zones, which is exactly where a local-clock subtraction
    would be wrong. `date_only` legs hold a midnight placeholder and no arrival, so they
    return None instead of a fabricated span.
    """
    if leg.arrive_utc is None:
        return None
    return int((leg.arrive_utc - leg.depart_utc).total_seconds() // 60)


def offer_duration_minutes(offer: Offer) -> int | None:
    """The offer's own duration when the source stated one, otherwise derived.

    Several adapters (azimuth, pobeda) fill real arrival timestamps but never set
    `duration_minutes`, and a discovery-tier offer is the mirror image — a duration and
    no timestamps at all. Deriving here rather than in each adapter means one rule for
    the whole roster, and it is what both the card and cross-source grouping read.
    """
    if offer.duration_minutes is not None:
        return offer.duration_minutes
    if offer.legs[-1].arrive_utc is None:
        return None
    return int((offer.legs[-1].arrive_utc - offer.legs[0].depart_utc).total_seconds() // 60)


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


def build_search_result(
    offers: list[Offer], *, blocked: bool = False, partial: bool = False
) -> SearchResult:
    if offers:
        status = ResultStatus.OK
    elif blocked:
        status = ResultStatus.BLOCKED
    elif partial:
        status = ResultStatus.UPSTREAM_ERROR
    else:
        status = ResultStatus.EMPTY
    return SearchResult(status=status, offers=offers, partial=partial)


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    min_interval_s: float
    daily_cap: int | None = None


def probe_dates_total_timeout_s(per_date_s: float, *, setup_s: float = 0.0, egress_attempts: int = 1) -> int:
    """Honest `total_timeout_s` for an adapter that spends one sequential request
    per probe date inside a single `search()` call (`supports_batch_dates = False`).

    Worst case is `per_date_s` x however many dates one cycle probes, plus whatever
    one-off setup the adapter pays before the loop (a location/city id lookup, an
    `init` RPC, S7's browser solve). Derived from `flex_probe_dates_per_cycle`
    rather than hardcoded so the two can't drift — that drift is exactly what left
    every confirmation adapter declaring a flat 60s for work whose own docstring
    admitted it could need 7 sequential requests.

    `egress_attempts` is the same kind of correction for adapters that run behind
    an `EgressPool`: a blocked egress makes the adapter replay the whole date loop
    through the next one, and declaring the single-pass figure meant the registry's
    own `asyncio.timeout` could kill the *retry* — the exact attempt the pool exists
    to make — and report TIMEOUT for a source that was one hop from working. Pass
    `MAX_EGRESS_ATTEMPTS_PER_CALL`, not the configured pool length: the pool bounds
    attempts per call by that constant regardless of how many egresses an operator
    lists, so the ceiling stays a constant rather than something config can inflate.

    These are ceilings for a source that hangs, not expected durations; a source
    that actually behaves that way trips the circuit breaker after 5 tries and
    then costs nothing at all until it recovers.
    """
    from aviapars.config import settings  # local: keeps the adapter contract itself config-free

    return int((per_date_s * settings.flex_probe_dates_per_cycle + setup_s) * egress_attempts)


# Passed as `curl_options=CURL_FORCE_IPV4` to every curl_cffi AsyncSession in the
# roster. Root-caused live (2026-08-19): this VDS has an IPv6 stack (only a
# Tailscale ULA address, no real public IPv6 route) but curl_cffi still races
# AAAA/A lookups and IPv6/IPv4 connection attempts by default — forcing IPv4-only
# resolution took a live ~50% failure rate on repeated identical requests down to
# 0/10 in the same test. `with_dns_retry` below stays on as a backstop for
# whatever residual transport hiccups aren't this specific IPv6 issue.
CURL_FORCE_IPV4: dict[int, int] = {CurlOpt.IPRESOLVE: CurlIpResolve.V4}

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


MAX_EGRESS_ATTEMPTS_PER_CALL = 2

_RESOLVER_HINT = "resolv"  # "Resolving timed out", "Could not resolve host"


def is_egress_failure(exc: BaseException) -> bool:
    """True when a curl_cffi exception means the request never reached the site
    at all — TCP connect refused/timed out, a dead SOCKS hop, a read that never
    answered — i.e. evidence about the *egress*, not about the source.

    Needed because not every block announces itself with an HTTP status. tutu.ru
    bans by silently dropping the SYN (verified live 2026-08-19 from the VDS: raw
    `curl` to offers-api.tutu.ru just times out with no response at all, while the
    identical request through either local SOCKS5 proxy answers instantly), so for
    `tutu_*_direct` "the connection never completed" is the only blocked-signal
    that exists — there is no 403 to read like aeroflot/rzd/pobeda get.

    Deliberately *not* transport failures, both directions:
    - A parse/shape error means the egress worked fine and the payload changed.
      Rotating to another IP there just burns a second request on the same bug.
    - DNS resolution failures are excluded on purpose, and this is the one place
      the distinction is subtle enough to need saying: this VDS has a documented,
      unrelated flakiness in curl_cffi's own async resolver (see `with_dns_retry`
      — roughly half of repeated identical requests failed to resolve a host the
      OS resolver handled every time). Feeding that into rotation would cool off a
      perfectly good egress on a purely local hiccup. curl surfaces the two as the
      same `Timeout` type — only the message separates "Resolving timed out" from
      "Connection timed out" — hence the string check alongside `DNSError`.
    """
    if isinstance(exc, DNSError):
        return False
    if not isinstance(exc, (CurlConnectionError, ProxyError, Timeout)):
        return False
    return _RESOLVER_HINT not in str(exc).lower()


@dataclass(slots=True)
class _EgressCooldown:
    until: datetime
    escalation: int


class EgressPool:
    """An ordered pool of ways out to the internet — proxy URLs, or `None` for the
    box's own IP — that an adapter rotates through as they get blocked.

    Replaces the two-step `primary -> fallback-on-BLOCKED` cascade that
    `aeroflot_direct` and `rzd_direct` each hand-rolled. Same premise, generalised:
    carrier WAFs in this roster block by IP *class* or by specific address, not by
    query content, so a different way out is often the whole fix — and the cheapest
    egress that works right now is worth preferring over the one that always works
    (the admin's home relay, which depends on a personal machine being switched on).

    Sticky, not re-scanned per call. Once an egress comes back BLOCKED it is put on
    a cooldown and later calls start from the next one instead of re-probing the
    blocked one every single time. That matters for more than the wasted request:
    re-hitting a source that just blocked you is exactly the behaviour that earned
    this VDS a network-level ban from tutu.ru in the first place.

    Cooldown, not a one-way ratchet. A pool that only ever moves forward means one
    bad hour permanently downgrades a source to its worst egress; every block here
    instead expires after `BACKOFF_STEPS_S[escalation]` (15min -> 1h -> 4h, the
    circuit breaker's own ladder, reused so the project has one backoff vocabulary
    rather than two overlapping ones), after which the preferred egress is simply
    eligible again and the next call re-probes it for free. Escalation resets the
    moment that egress produces a real answer. The two mechanisms never delay the
    same thing: the circuit breaker decides whether the adapter runs at all, this
    decides which way out it uses when it does.

    Bounded per call by `MAX_EGRESS_ATTEMPTS_PER_CALL`, deliberately, even when the
    pool is longer. One `search()` is already a whole date loop; letting a call walk
    a 4-entry pool would quadruple its worst case and blow through the ceiling
    `total_timeout_s` declares. Convergence happens *across* calls instead — the
    cooldown left behind by attempt N is what makes call N+1 start further down the
    pool — which is what "switch as soon as something gets blocked" actually needs.

    No lock. Every state mutation here is synchronous, so it is atomic under
    asyncio and concurrent callers cannot corrupt it. Serializing the *attempts*
    would be the wrong fix for the one thing a lock would buy (two calls that
    overlap in time both paying for the same block before either records it):
    that costs at most one extra probe, while a lock would silently re-serialize
    every concurrent call to the source — which is what `http_concurrency_per_source`
    already governs, and an operator who raised that setting would find the pool
    quietly ignoring them.

    State is per-instance and in memory on purpose, not in `source_health`. It is
    derived, single-process, and cheap to rebuild — a restart costs one blocked
    probe per adapter, against which persisting it would need a schema migration
    plus a DB dependency inside the adapter layer, which §5 rule 3 says adapters
    do not have. It would also key state to proxy URLs that config can change
    between restarts, leaving orphaned rows describing egresses that no longer
    exist. If it ever genuinely needs to survive restarts, the precedent to follow
    is `s7_direct`'s injected getter/setter callables, not a repo handed to an
    adapter.
    """

    def __init__(
        self, egresses: Iterable[str | None] = (), *, attempts_per_call: int = MAX_EGRESS_ATTEMPTS_PER_CALL
    ) -> None:
        # dedupe preserving order: config lets every adapter's pool point at the same
        # home relay, and a pool that lists one egress twice would "rotate" onto the
        # address that just blocked it.
        seen: dict[str | None, None] = {}
        for url in egresses:
            seen.setdefault(url, None)
        self._egresses: tuple[str | None, ...] = tuple(seen) or (None,)
        self._attempts_per_call = attempts_per_call
        self._cooldowns: dict[str | None, _EgressCooldown] = {}

    async def run(self, attempt: Callable[[str | None], Awaitable[SearchResult]]) -> SearchResult:
        result = SearchResult(status=ResultStatus.BLOCKED, offers=[], partial=True, error="no egress attempted")
        for url in self._candidates(utcnow())[: self._attempts_per_call]:
            result = await attempt(url)
            self._record(url, result.status)
            if result.status != ResultStatus.BLOCKED:
                return result
        return result

    def _candidates(self, now: datetime) -> list[str | None]:
        ready: list[str | None] = []
        cooling: list[str | None] = []
        for url in self._egresses:
            cooldown = self._cooldowns.get(url)
            (cooling if cooldown is not None and now < cooldown.until else ready).append(url)
        # A pool where everything is currently cooling off still has to pick something —
        # returning nothing would report BLOCKED without spending a request, which is the
        # circuit breaker's job, not this one's. Least-recently-blocked goes first.
        cooling.sort(key=lambda url: self._cooldowns[url].until)
        return ready + cooling

    def _record(self, url: str | None, status: ResultStatus) -> None:
        if status == ResultStatus.BLOCKED:
            previous = self._cooldowns.get(url)
            escalation = previous.escalation if previous else 0
            step = BACKOFF_STEPS_S[min(escalation, len(BACKOFF_STEPS_S) - 1)]
            self._cooldowns[url] = _EgressCooldown(utcnow() + timedelta(seconds=step), escalation + 1)
            return
        if status in (ResultStatus.OK, ResultStatus.EMPTY):
            # EMPTY counts as proof alongside OK: the request was answered, the answer was
            # "no offers". Anything else (TIMEOUT, UPSTREAM_ERROR, PARSE_ERROR) says nothing
            # about the egress either way, so it leaves the cooldown exactly as it was.
            self._cooldowns.pop(url, None)


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
