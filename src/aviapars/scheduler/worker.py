from __future__ import annotations

import logging
from datetime import date

import aiosqlite

from aviapars.adapters.base import Mode, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.db.models import Route, User
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.domain.combiner import Itinerary, SearchOpts, itinerary_modes, search_itineraries
from aviapars.domain.dates import DatePlan, resolve_date_plan, selection_from_route
from aviapars.domain.dealer import crosses_threshold
from aviapars.domain.places import allowed_modes
from aviapars.notify.notifier import Notifier

log = logging.getLogger(__name__)


async def process_route(
    route: Route,
    *,
    user: User,
    registry: AdapterRegistry,
    places_repo: PlacesRepo,
    routes_repo: RoutesRepo,
    observations_repo: ObservationsRepo,
    notifier: Notifier,
) -> None:
    """One poll cycle for one route (§6/§9). Searches every mode the route
    allows in one combiner call — including hub-composed itineraries when
    `allow_hub_routing` is set — records every offer seen (both legs of a
    composed itinerary), and notifies about the cheapest itinerary that crosses
    the route's threshold — never about anything above it. See `_deals_to_send`
    for why a route with per-mode ceilings can yield more than one.

    A route on a flexible date window doesn't scan the whole window through
    one-request-per-date sources every cycle (§6): `date_cursor` rotates a
    small slice forward each time, so the window is covered over several
    cycles at a per-cycle cost that doesn't depend on its width. Batch-capable
    sources see the whole window anyway — for them it's the same request.
    """
    origin = await places_repo.get(route.origin_place_id)
    dest = await places_repo.get(route.dest_place_id)
    if origin is None or dest is None:
        return  # route creation guarantees valid place ids; nothing to recover here

    dates, next_cursor = _resolve_dates(route)
    if next_cursor != route.date_cursor:
        await routes_repo.set_date_cursor(route.id, next_cursor)
    if dates.is_empty:
        return  # window fully in the past — nothing left to watch

    # No per-adapter deadline here on purpose (was a flat 90s). Nobody is waiting on a
    # background poll, and 90s was below what several confirmation adapters honestly need for
    # a 7-date cycle — it threw away offers they had already collected. Each adapter's own
    # `total_timeout_s` governs instead (see adapters/base.probe_dates_total_timeout_s).
    #
    # The obvious worry — "N adapters x a big ceiling makes a cycle unbounded" — is smaller
    # than it was, not larger: combiner fans the adapters out concurrently now (bounded by
    # http_concurrency_global), so a leg costs roughly the max of the ceilings rather than
    # their sum. Even fully pathological, that is below what the old sequential 90s-each loop
    # cost, and a source that actually hangs trips the circuit breaker and stops being asked.
    opts = SearchOpts(
        tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}),
        allow_network=True,
        max_hops=route.max_hops if route.allow_hub_routing else 0,
        hubs=tuple(route.hub_place_ids) if route.hub_place_ids else None,
        max_transfer_min=route.max_transfer_min,
        max_total_hours=route.max_total_hours,
        hub_probe_dates_cap=settings.flex_hub_probe_dates,
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
    # The same offer (same itinerary_hash) commonly appears in several composed itineraries
    # at once — a leg shared across hub combinations — so recording every occurrence would
    # insert the identical row once per combination it happens to be part of.
    recorded_hashes: set[str] = set()
    for itinerary in outcome.itineraries:
        for offer in itinerary.offers:
            if offer.itinerary_hash in recorded_hashes:
                continue
            recorded_hashes.add(offer.itinerary_hash)
            try:
                await observations_repo.record(route.id, offer)
            except aiosqlite.IntegrityError:
                # Observed live: "🔍 Проверить сейчас" on a route the user deletes mid-search
                # (a slow cycle against several confirmation adapters can run minutes) leaves
                # `route.id` gone by the time this INSERT runs — a FOREIGN KEY failure that,
                # unhandled, used to crash the whole update and skip the notify step below for
                # every other offer in this same cycle, not just the doomed one.
                log.warning("observation insert for route %s failed (route likely deleted mid-cycle)", route.id)

    for deal in _deals_to_send(route, outcome.itineraries):
        confirmed = all(offer.tier == Tier.CONFIRMATION for offer in deal.offers)
        # crosses_threshold() knows about modes but nothing about delivery — "is this cheap
        # enough" is a fact about the price, not about how the user wants to hear it.
        # send_deal() itself branches on route.notification_mode for instant vs digest.
        await notifier.send_deal(user=user, route=route, itinerary=deal, confirmed=confirmed)


def _deals_to_send(route: Route, itineraries: list[Itinerary]) -> list[Itinerary]:
    """The cheapest qualifying itinerary — or one per transport group, when the
    route carries separate per-mode ceilings (§9).

    Per-mode ceilings change two things about picking a deal. First, the
    globally cheapest itinerary is no longer necessarily the qualifying one: a
    cheap train sitting above the train ceiling can rank ahead of a plane that
    is under the plane ceiling, so the whole (price-sorted) list is scanned
    instead of only its head being checked. Under a single shared ceiling that
    is identical to the old behaviour — the first entry qualifies or nothing
    does.

    Second, "planes under 8000 OR trains under 4000" is two independent asks.
    Reporting only the cheaper of the two would silently answer half of it and
    leave a qualifying plane deal invisible for as long as some cheap train
    exists, so each transport group with a qualifying option gets its own
    notification. Grouping is by the itinerary's set of modes, which puts a
    mixed plane+train hub trip in its own bucket, same split the /search
    results screen uses. Routes on one shared ceiling still send exactly one
    message per cycle.
    """
    qualifying = [it for it in itineraries if crosses_threshold(route, it, itinerary_modes(it))]
    if not qualifying or not route.max_price_by_mode:
        return qualifying[:1]
    best_per_group: dict[frozenset[Mode], Itinerary] = {}
    for itinerary in qualifying:  # price-sorted, so the first seen per group is its cheapest
        best_per_group.setdefault(frozenset(itinerary_modes(itinerary)), itinerary)
    return list(best_per_group.values())


def _resolve_dates(route: Route) -> tuple[DatePlan, int]:
    return resolve_date_plan(
        selection_from_route(route),
        today=date.today(),
        probe_budget=settings.flex_probe_dates_per_cycle,
        cursor=route.date_cursor,
    )
