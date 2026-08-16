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
    """One poll cycle for one route (§6/§9). Merges outcomes across every
    mode the route allows (relevant for mode_pref='auto'), records every
    offer seen, and fires a single notification for the cheapest one that
    crosses the threshold — never for anything above it.
    """
    origin = await places_repo.get(route.origin_place_id)
    dest = await places_repo.get(route.dest_place_id)
    if origin is None or dest is None:
        return  # route creation guarantees valid place ids; nothing to recover here

    dates = _resolve_dates(route)
    opts = SearchOpts(tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}), allow_network=True, deadline_s=90)

    best_price: int | None = None
    best_offer = None
    for mode in allowed_modes(route):
        outcome = await search_itineraries(
            registry, origin=origin, dest=dest, dates=dates, pax=route.pax, mode=mode, opts=opts
        )
        for itinerary in outcome.itineraries:
            offer = itinerary.offers[0]  # single-leg only until Phase D's hub composition
            await observations_repo.record(route.id, offer)
            if best_price is None or itinerary.price_minor < best_price:
                best_price = itinerary.price_minor
                best_offer = offer

    if best_offer is None:
        return
    if not crosses_threshold(route, best_offer):
        return

    await notifier.send_deal(user=user, route=route, offer=best_offer, confirmed=best_offer.tier == Tier.CONFIRMATION)


def _resolve_dates(route: Route) -> tuple[date, ...]:
    if route.date_mode == "fixed":
        return tuple(date.fromisoformat(d) for d in (route.fixed_dates or []))
    start_offset = route.window_start_offset_days or 0
    length = route.window_length_days or 30
    start = date.today() + timedelta(days=start_offset)
    return tuple(start + timedelta(days=i) for i in range(length))
