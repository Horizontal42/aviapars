from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from aviapars.adapters.base import Mode, Offer, PlaceRef, SearchQuery, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Place
from aviapars.domain.validator import freshness_gate


@dataclass(frozen=True, slots=True)
class SearchOpts:
    tiers: frozenset[Tier]
    allow_network: bool = True
    deadline_s: float = 90.0
    max_hops: int = 1  # unused until Phase D's hub composition lands
    hubs: tuple[int, ...] | None = None
    leg_pool_size: int = 3


@dataclass(frozen=True, slots=True)
class Itinerary:
    offers: tuple[Offer, ...]
    price_minor: int
    currency: str
    violations: tuple[str, ...] = ()

    @property
    def duration_minutes(self) -> int | None:
        total = sum((o.duration_minutes or 0) for o in self.offers)
        return total or None


@dataclass(slots=True)
class SearchOutcome:
    itineraries: list[Itinerary]
    leg_pool: dict[str, list[Offer]]
    sources_used: list[str]
    sources_skipped: list[tuple[str, str]]
    oldest_data_at: datetime | None
    partial: bool


async def search_itineraries(
    registry: AdapterRegistry,
    *,
    origin: Place,
    dest: Place,
    dates: tuple[date, ...],
    pax: int,
    mode: Mode,
    opts: SearchOpts,
) -> SearchOutcome:
    """Single shared entry point (§8) for "what are the ways from A to B" —
    both the scheduler and a future /search call this, differing only in
    `opts`. Current scope: direct offers only, one mode per call (an "auto"
    route means the caller loops over allowed_modes and merges outcomes).
    Hub/mixed-mode composition (§8's compose()) is a Phase D addition that
    slots in here without changing this function's signature — `SearchOpts`
    already carries `max_hops`/`hubs` for that.
    """
    origin_ref = _place_ref(origin)
    dest_ref = _place_ref(dest)
    query = SearchQuery(origin=origin_ref, dest=dest_ref, dates=dates, pax=pax, mode=mode)

    adapters = [a for a in registry.for_query(mode, origin.country, dest.country) if a.tier in opts.tiers]

    itineraries: list[Itinerary] = []
    leg_pool: dict[str, list[Offer]] = {}
    sources_used: list[str] = []
    sources_skipped: list[tuple[str, str]] = []
    oldest: datetime | None = None
    partial = False
    now = datetime.now(timezone.utc)

    for adapter in adapters:
        result = await registry.search(adapter, query, allow_network=opts.allow_network, deadline_s=opts.deadline_s)
        if result.partial:
            partial = True
        if result.status.value != "ok":
            sources_skipped.append((adapter.id, result.status.value))
            continue
        sources_used.append(adapter.id)

        kept: list[Offer] = []
        for offer in result.offers:
            gated = freshness_gate(offer, now)
            if gated is None:
                continue
            kept.append(gated)
            if gated.source_cached_at and (oldest is None or gated.source_cached_at < oldest):
                oldest = gated.source_cached_at

        pool_key = f"{origin.id}:{dest.id}:{mode.value}"
        leg_pool.setdefault(pool_key, []).extend(kept[: opts.leg_pool_size])
        for offer in kept:
            itineraries.append(Itinerary(offers=(offer,), price_minor=offer.price_minor, currency=offer.currency))

    itineraries.sort(key=lambda it: it.price_minor)
    return SearchOutcome(
        itineraries=itineraries,
        leg_pool=leg_pool,
        sources_used=sources_used,
        sources_skipped=sources_skipped,
        oldest_data_at=oldest,
        partial=partial,
    )


def _place_ref(place: Place) -> PlaceRef:
    return PlaceRef(place_id=place.id, iata=place.iata, yandex_code=place.yandex_code, city_id=place.city_id)
