from __future__ import annotations

from typing import Protocol


class RouteThreshold(Protocol):
    """Structural subset of routes (§4) that crosses_threshold actually needs —
    satisfied by the real Route model once db/models.py exists, no import
    needed from there."""

    max_price: int
    max_price_currency: str


class Priced(Protocol):
    """Structural subset shared by a single-leg Offer and a composed
    multi-leg Itinerary (§8) — crosses_threshold only cares about the total
    price, whichever produced it. Declared as properties (not plain fields)
    so a frozen dataclass's read-only attributes satisfy this structurally —
    mypy treats a bare `x: int` Protocol member as requiring a settable
    attribute, which a frozen dataclass instance is not."""

    @property
    def price_minor(self) -> int: ...

    @property
    def currency(self) -> str: ...


def crosses_threshold(route: RouteThreshold, priced: Priced) -> bool:
    """Pure threshold check (§9) — no price above route.max_price ever counts
    as a deal, regardless of how it compares to history. Anti-noise
    (dedupe/cooldown/quiet hours) is the Notifier's job, not this function's —
    see notify/dedupe.py.
    """
    if priced.currency != route.max_price_currency:
        return False
    return priced.price_minor <= route.max_price
