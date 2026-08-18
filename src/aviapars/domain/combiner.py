from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aviapars.adapters.base import Mode, Offer, PlaceRef, SearchQuery, SourceAdapter, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.domain.dates import DatePlan
from aviapars.domain.transfers import transfer_policy
from aviapars.domain.validator import freshness_gate

log = logging.getLogger(__name__)

MAX_COMBO_CANDIDATES_DEFAULT = 24
MAX_HUBS_DEFAULT = 2
HUB_PROBE_DATES_CAP_DEFAULT = 1


@dataclass(frozen=True, slots=True)
class SearchOpts:
    tiers: frozenset[Tier]
    allow_network: bool = True
    deadline_s: float = 90.0
    max_hops: int = 1  # 0 = direct only
    hubs: tuple[int, ...] | None = None  # None = auto-discover (§8)
    leg_pool_size: int = 3
    max_transfer_min: int | None = None
    max_total_hours: float | None = None
    max_combo_candidates: int = MAX_COMBO_CANDIDATES_DEFAULT
    # Hub legs probe far fewer dates than the direct leg on purpose: a hub itinerary needs
    # *matching* dates on both legs, so widening both sides multiplies one-request-per-date
    # sources quadratically to produce pairs that _dates_composable then throws away. Batch
    # sources still see the whole window on hub legs, since that costs them nothing.
    hub_probe_dates_cap: int = HUB_PROBE_DATES_CAP_DEFAULT


@dataclass(frozen=True, slots=True)
class Itinerary:
    offers: tuple[Offer, ...]
    price_minor: int
    currency: str
    violations: tuple[str, ...] = ()
    duration_minutes: int | None = None


@dataclass(slots=True)
class SearchOutcome:
    itineraries: list[Itinerary]
    leg_pool: dict[str, list[Offer]]
    hubs_tried: list[int]
    sources_used: list[str]
    sources_skipped: list[tuple[str, str]]
    oldest_data_at: datetime | None
    partial: bool


async def search_itineraries(
    registry: AdapterRegistry,
    places_repo: PlacesRepo,
    *,
    origin: Place,
    dest: Place,
    dates: DatePlan,
    pax: int,
    modes: frozenset[Mode],
    opts: SearchOpts,
) -> SearchOutcome:
    """Single shared entry point (§8) for "what are the ways from A to B" —
    both the scheduler and /search call this, differing only in `opts`.
    Direct offers for every allowed mode, plus (if `opts.max_hops >= 1`) hub
    composition: for each candidate hub and each mode pair (same-mode hub
    routing counts too, not just mode-mixing), search origin->hub and
    hub->dest independently and combine. Cross-product is bounded by
    `opts.max_combo_candidates` since it's quadratic in `len(modes)`.

    `dates` arrives already budget-resolved (`domain.dates.DatePlan`): each
    adapter is asked for the whole window or just this cycle's probe slice
    depending on its `supports_batch_dates`, so a flexible route costs a
    bounded number of requests regardless of how wide its window is.
    """
    now = datetime.now(timezone.utc)
    itineraries: list[Itinerary] = []
    leg_pool: dict[str, list[Offer]] = {}
    sources_used: list[str] = []
    sources_skipped: list[tuple[str, str]] = []
    oldest: datetime | None = None
    partial = False

    for mode in modes:
        offers, meta = await _search_leg(registry, origin, dest, dates, pax, mode, opts, now)
        _merge_meta(meta, sources_used, sources_skipped)
        oldest = _min_dt(oldest, meta.oldest)
        partial = partial or meta.partial
        leg_pool.setdefault(_pool_key(origin, dest, mode), []).extend(offers[: opts.leg_pool_size])
        for offer in offers:
            itineraries.append(
                Itinerary(
                    offers=(offer,),
                    price_minor=offer.price_minor,
                    currency=offer.currency,
                    duration_minutes=offer.duration_minutes,
                )
            )

    hubs_tried: list[int] = []
    if opts.max_hops >= 1 and not dates.is_empty:
        hub_dates = dates.narrowed(opts.hub_probe_dates_cap)
        hubs = await _resolve_hubs(places_repo, origin, dest, opts)
        hubs_tried = [h.id for h in hubs]
        combo_count = 0

        for hub in hubs:
            leg_a_by_mode: dict[Mode, list[Offer]] = {}
            leg_b_by_mode: dict[Mode, list[Offer]] = {}
            for m in modes:
                offers_a, meta_a = await _search_leg(registry, origin, hub, hub_dates, pax, m, opts, now)
                offers_b, meta_b = await _search_leg(registry, hub, dest, hub_dates, pax, m, opts, now)
                leg_a_by_mode[m] = offers_a[: opts.leg_pool_size]
                leg_b_by_mode[m] = offers_b[: opts.leg_pool_size]
                _merge_meta(meta_a, sources_used, sources_skipped)
                _merge_meta(meta_b, sources_used, sources_skipped)
                oldest = _min_dt(_min_dt(oldest, meta_a.oldest), meta_b.oldest)
                partial = partial or meta_a.partial or meta_b.partial
                leg_pool.setdefault(_pool_key(origin, hub, m), []).extend(leg_a_by_mode[m])
                leg_pool.setdefault(_pool_key(hub, dest, m), []).extend(leg_b_by_mode[m])

            for mode_a, mode_b in itertools.product(modes, repeat=2):
                if combo_count >= opts.max_combo_candidates:
                    break
                for offer_a, offer_b in itertools.product(leg_a_by_mode[mode_a], leg_b_by_mode[mode_b]):
                    if combo_count >= opts.max_combo_candidates:
                        break
                    if not _dates_composable(offer_a, offer_b):
                        continue
                    combo_count += 1
                    itinerary = _compose(
                        offer_a, offer_b, max_transfer_min=opts.max_transfer_min, max_total_hours=opts.max_total_hours
                    )
                    if not itinerary.violations:
                        itineraries.append(itinerary)

    itineraries.sort(key=lambda it: it.price_minor)
    return SearchOutcome(
        itineraries=itineraries,
        leg_pool=leg_pool,
        hubs_tried=hubs_tried,
        sources_used=sources_used,
        sources_skipped=sources_skipped,
        oldest_data_at=oldest,
        partial=partial,
    )


@dataclass(slots=True)
class _LegMeta:
    sources_used: list[str]
    sources_skipped: list[tuple[str, str]]
    oldest: datetime | None
    partial: bool


async def _search_leg(
    registry: AdapterRegistry,
    origin: Place,
    dest: Place,
    dates: DatePlan,
    pax: int,
    mode: Mode,
    opts: SearchOpts,
    now: datetime,
) -> tuple[list[Offer], _LegMeta]:
    origin_ref = _place_ref(origin)
    dest_ref = _place_ref(dest)
    adapters = [a for a in registry.for_query(mode, origin.country, dest.country) if a.tier in opts.tiers]

    meta = _LegMeta(sources_used=[], sources_skipped=[], oldest=None, partial=False)
    planned: list[tuple[SourceAdapter, SearchQuery]] = []
    for adapter in adapters:
        wanted = dates.batch_dates if adapter.supports_batch_dates else dates.probe_dates
        if not wanted:
            continue
        planned.append((adapter, SearchQuery(origin=origin_ref, dest=dest_ref, dates=wanted, pax=pax, mode=mode)))

    # Every adapter for this leg is asked at once, not one after another. What actually bounds
    # concurrent network activity is the registry's own machinery — http_concurrency_global /
    # http_concurrency_per_source semaphores, the per-source rate limiter, the circuit breaker —
    # and all of it is keyed by `adapter.id` (separate Semaphore/Lock/bucket per entry), so
    # concurrent calls to *different* adapters never touch shared state. Serializing here bought
    # nothing except a leg that cost the sum of every adapter's timeout instead of the max.
    #
    # return_exceptions=True is a real backstop, not defensiveness about an impossible case:
    # registry.search() is contractually non-raising (§5 rule 1, and it wraps the adapter in its
    # own try/except), but a bare gather() cancels every other in-flight adapter the instant one
    # task raises — concurrency is what turns a single adapter's bug into a whole-leg blackout,
    # and the old sequential loop simply could not lose already-collected offers that way.
    results = await asyncio.gather(
        *(
            registry.search(adapter, query, allow_network=opts.allow_network, deadline_s=opts.deadline_s)
            for adapter, query in planned
        ),
        return_exceptions=True,
    )

    kept: list[Offer] = []
    # gather() returns results in input order, so bookkeeping below runs in adapter order no
    # matter which request finished first — sources_used/sources_skipped stay reproducible.
    for (adapter, _query), result in zip(planned, results):
        if isinstance(result, BaseException):
            log.error("adapter %s raised out of registry.search()", adapter.id, exc_info=result)
            meta.sources_skipped.append((adapter.id, "upstream_error"))
            meta.partial = True
            continue
        if result.partial:
            meta.partial = True
        if result.status.value != "ok":
            meta.sources_skipped.append((adapter.id, result.status.value))
            continue
        meta.sources_used.append(adapter.id)
        for offer in result.offers:
            gated = freshness_gate(offer, now)
            if gated is None:
                continue
            kept.append(gated)
            meta.oldest = _min_dt(meta.oldest, gated.source_cached_at)

    kept.sort(key=lambda o: o.price_minor)
    return kept, meta


def _dates_composable(leg_a: Offer, leg_b: Offer) -> bool:
    """Both legs of a hub itinerary have to be the same trip. Once a route can
    carry a whole date window, the leg pools span different days, and pairing
    a flight on the 5th with a train on the 20th would be nonsense that the
    transfer check can't catch on its own — `date_only` discovery offers have
    no real timestamp to compare (see `_compose`)."""
    # local, not UTC: an overnight Moscow departure or a Far-East morning one both read
    # back a day early off depart_utc.date(), which could shift a legitimately same-day
    # pairing far enough to fail this window and get dropped.
    a = leg_a.legs[0].depart_local.date()
    b = leg_b.legs[0].depart_local.date()
    return 0 <= (b - a).days <= 1


def _compose(leg_a: Offer, leg_b: Offer, *, max_transfer_min: int | None, max_total_hours: float | None) -> Itinerary:
    """Composes two independently-searched offers into a hub itinerary (§8).

    Real bug found and fixed here: discovery-tier offers with no known
    departure/arrival time (`time_precision == "date_only"`, e.g. Travelpayouts)
    all use midnight-of-the-date as a placeholder timestamp (base.py Leg
    docstring). Composing two same-date date_only offers naively made every
    single one of them look like a 0-minute transfer — i.e. always "too
    short", so every same-date plane+plane-via-hub combo was silently
    dropped, which is a false negative, not honest uncertainty. Transfer/
    duration checks below only run when both endpoints actually have a real
    timestamp; otherwise feasibility is left unverified (not rejected, not
    passed) rather than computed from a placeholder — a confirmation-tier
    check is what actually resolves it later.
    """
    policy = transfer_policy(leg_a.legs[-1].mode, leg_b.legs[0].mode)
    a_last = leg_a.legs[-1]
    b_first = leg_b.legs[0]
    # `time_precision == "exact"` is a claim about the *source*, not a guarantee — several
    # adapters (rzd_direct, yandex_rasp, tutu_rail/avia direct) set "exact" while still
    # leaving arrive_utc None when the upstream response omits an arrival time. Checking
    # arrive_utc directly (not just time_precision) is what actually guarantees a usable
    # timestamp; an `assert` here previously crashed the whole search on exactly that case,
    # and would have silently stopped guarding anything at all under `python -O`.
    transfer_times_known = (
        a_last.time_precision == "exact" and b_first.time_precision == "exact" and a_last.arrive_utc is not None
    )

    violations: list[str] = []
    gap_min: float | None = None
    if transfer_times_known and a_last.arrive_utc is not None:
        gap_min = (b_first.depart_utc - a_last.arrive_utc).total_seconds() / 60
        if gap_min < 0:
            violations.append("плечо Б отправляется раньше прибытия плеча А")
        elif gap_min < policy.min_transfer_min:
            violations.append(f"пересадка {int(gap_min)} мин — меньше рекомендуемых {policy.min_transfer_min} мин")
        if max_transfer_min is not None and gap_min > max_transfer_min:
            violations.append(f"пересадка {int(gap_min)} мин превышает лимит {max_transfer_min} мин")

    if leg_a.currency != leg_b.currency:
        violations.append("разные валюты на плечах — сумма невозможна")

    duration_minutes: int | None = None
    last_leg_b = leg_b.legs[-1]
    if transfer_times_known and last_leg_b.time_precision == "exact" and last_leg_b.arrive_utc is not None:
        total_hours = (last_leg_b.arrive_utc - leg_a.legs[0].depart_utc).total_seconds() / 3600
        duration_minutes = int(total_hours * 60)
        if max_total_hours is not None and total_hours > max_total_hours:
            violations.append(f"итоговая длительность {total_hours:.1f}ч превышает лимит {max_total_hours}ч")

    same_currency = leg_a.currency == leg_b.currency
    transfer_cost = policy.transfer_cost_minor if same_currency else 0
    price = leg_a.price_minor + leg_b.price_minor + transfer_cost

    return Itinerary(
        offers=(leg_a, leg_b),
        price_minor=price,
        currency=leg_a.currency,
        violations=tuple(violations),
        duration_minutes=duration_minutes,
    )


async def _resolve_hubs(places_repo: PlacesRepo, origin: Place, dest: Place, opts: SearchOpts) -> list[Place]:
    if opts.hubs is not None:
        resolved = []
        for hub_id in opts.hubs:
            place = await places_repo.get(hub_id)
            if place is not None:
                resolved.append(place)
        return resolved
    return await places_repo.hub_candidates_near(origin, dest, limit=MAX_HUBS_DEFAULT)


def _merge_meta(meta: _LegMeta, sources_used: list[str], sources_skipped: list[tuple[str, str]]) -> None:
    for s in meta.sources_used:
        if s not in sources_used:
            sources_used.append(s)
    sources_skipped.extend(meta.sources_skipped)


def _min_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _pool_key(origin: Place, dest: Place, mode: Mode) -> str:
    return f"{origin.id}:{dest.id}:{mode.value}"


def _place_ref(place: Place) -> PlaceRef:
    return PlaceRef(
        place_id=place.id,
        iata=place.iata,
        yandex_code=place.yandex_code,
        rzd_code=place.rzd_code,
        city_id=place.city_id,
    )
