from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, datetime, timezone

from aviapars.adapters.base import Mode, Offer, PlaceRef, SearchQuery, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.domain.transfers import transfer_policy
from aviapars.domain.validator import freshness_gate

MAX_COMBO_CANDIDATES_DEFAULT = 24
MAX_HUBS_DEFAULT = 2


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
    dates: tuple[date, ...],
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
    if opts.max_hops >= 1:
        hubs = await _resolve_hubs(places_repo, origin, dest, opts)
        hubs_tried = [h.id for h in hubs]
        combo_count = 0

        for hub in hubs:
            leg_a_by_mode: dict[Mode, list[Offer]] = {}
            leg_b_by_mode: dict[Mode, list[Offer]] = {}
            for m in modes:
                offers_a, meta_a = await _search_leg(registry, origin, hub, dates, pax, m, opts, now)
                offers_b, meta_b = await _search_leg(registry, hub, dest, dates, pax, m, opts, now)
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
    dates: tuple[date, ...],
    pax: int,
    mode: Mode,
    opts: SearchOpts,
    now: datetime,
) -> tuple[list[Offer], _LegMeta]:
    origin_ref = _place_ref(origin)
    dest_ref = _place_ref(dest)
    query = SearchQuery(origin=origin_ref, dest=dest_ref, dates=dates, pax=pax, mode=mode)
    adapters = [a for a in registry.for_query(mode, origin.country, dest.country) if a.tier in opts.tiers]

    meta = _LegMeta(sources_used=[], sources_skipped=[], oldest=None, partial=False)
    kept: list[Offer] = []
    for adapter in adapters:
        result = await registry.search(adapter, query, allow_network=opts.allow_network, deadline_s=opts.deadline_s)
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
    transfer_times_known = a_last.time_precision == "exact" and b_first.time_precision == "exact"

    violations: list[str] = []
    gap_min: float | None = None
    if transfer_times_known:
        assert a_last.arrive_utc is not None  # exact precision guarantees a real arrival time
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
    if transfer_times_known and last_leg_b.time_precision == "exact":
        assert last_leg_b.arrive_utc is not None
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
