from __future__ import annotations

from typing import Protocol

from aviapars.adapters.base import Offer


class RouteThreshold(Protocol):
    """Structural subset of routes (§4) that crosses_threshold actually needs —
    satisfied by the real Route model once db/models.py exists, no import
    needed from there."""

    max_price: int
    max_price_currency: str


def crosses_threshold(route: RouteThreshold, offer: Offer) -> bool:
    """Pure threshold check (§9) — no price above route.max_price ever counts
    as a deal, regardless of how it compares to history. Anti-noise
    (dedupe/cooldown/quiet hours) is the Notifier's job, not this function's —
    see notify/dedupe.py.
    """
    if offer.currency != route.max_price_currency:
        return False
    return offer.price_minor <= route.max_price
