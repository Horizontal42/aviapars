from __future__ import annotations

from datetime import date, timedelta

from aviapars.adapters.base import Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Route, User
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.domain.combiner import SearchOpts, search_itineraries
from aviapars.domain.dealer import crosses_threshold
from aviapars.domain.places import allowed_modes
from aviapars.notify.notifier import Notifier


async def process_route(
    route: Route,
    *,
    user: User,
    registry: AdapterRegistry,
    places_repo: PlacesRepo,
    observations_repo: ObservationsRepo,
    notifier: Notifier,
) -> None:
    """One poll cycle for one route (§6/§9). Searches every mode the route
    allows in one combiner call — including hub-composed itineraries when
    `allow_hub_routing` is set — records every offer seen (both legs of a
    composed itinerary), and fires a single notification for the cheapest
    itinerary that crosses the threshold — never for anything above it.
    """
    origin = await places_repo.get(route.origin_place_id)
    dest = await places_repo.get(route.dest_place_id)
    if origin is None or dest is None:
        return  # route creation guarantees valid place ids; nothing to recover here

    dates = _resolve_dates(route)
    opts = SearchOpts(
        tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}),
        allow_network=True,
        deadline_s=90,
        max_hops=route.max_hops if route.allow_hub_routing else 0,
        hubs=tuple(route.hub_place_ids) if route.hub_place_ids else None,
        max_transfer_min=route.max_transfer_min,
        max_total_hours=route.max_total_hours,
    )

    outcome = await search_itineraries(
        registry,
        places_repo,
        origin=origin,
        dest=dest,
        dates=dates,
        pax=route.pax,
        modes=allowed_modes(route),
        opts=opts,
    )
    for itinerary in outcome.itineraries:
        for offer in itinerary.offers:
            await observations_repo.record(route.id, offer)

    if not outcome.itineraries:
        return
    best = outcome.itineraries[0]  # already sorted by price
    if not crosses_threshold(route, best):
        return

    confirmed = all(offer.tier == Tier.CONFIRMATION for offer in best.offers)
    await notifier.send_deal(user=user, route=route, itinerary=best, confirmed=confirmed)


def _resolve_dates(route: Route) -> tuple[date, ...]:
    if route.date_mode == "fixed":
        return tuple(date.fromisoformat(d) for d in (route.fixed_dates or []))
    start_offset = route.window_start_offset_days or 0
    length = route.window_length_days or 30
    start = date.today() + timedelta(days=start_offset)
    return tuple(start + timedelta(days=i) for i in range(length))
