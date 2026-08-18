from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from aviapars.adapters.base import Mode


class RouteThreshold(Protocol):
    """Structural subset of routes (§4) that crosses_threshold actually needs —
    satisfied by the real Route model once db/models.py exists, no import
    needed from there."""

    max_price: int
    max_price_currency: str
    max_price_by_mode: dict[str, int] | None


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


def threshold_for(route: RouteThreshold, modes: Collection[Mode] = ()) -> int:
    """The ceiling that actually applies to an itinerary travelling by `modes`.

    Per-mode ceilings are an opt-in, not the default: `max_price_by_mode` is
    NULL for every single-mode route and for every multi-mode route whose owner
    picked one shared number, and then this is just `max_price` — exactly what
    it has always meant.

    A **mixed**-mode hub itinerary (one plane leg, one train leg) is compared
    against the shared `max_price`, never against a per-mode ceiling. Its price
    is neither "a plane price" nor "a train price", so neither ceiling describes
    it, and adding two ceilings that were each chosen for a whole journey would
    compare one combined price against a number nobody picked. When per-mode
    ceilings are set, `max_price` holds the larger of them (see
    telegram/handlers/add.py) — the most the user said they'd pay for this trip
    by any single means, which is the honest bound for a trip that is partly
    both. Same-mode hub routing (plane→hub→plane) is not mixed and does use its
    mode's ceiling.
    """
    by_mode = route.max_price_by_mode
    if not by_mode:
        return route.max_price
    distinct = {m.value for m in modes}
    if len(distinct) == 1:
        # .get, not [...]: a route created before a third mode existed simply has no key
        # for it, and the shared ceiling is the right answer then, not a KeyError.
        return by_mode.get(distinct.pop(), route.max_price)
    return route.max_price


def crosses_threshold(route: RouteThreshold, priced: Priced, modes: Collection[Mode] = ()) -> bool:
    """Pure threshold check (§9) — no price above the applicable ceiling ever
    counts as a deal, regardless of how it compares to history. Anti-noise
    (dedupe/cooldown/quiet hours) is the Notifier's job, not this function's —
    see notify/dedupe.py.

    `modes` is what the itinerary actually travels by; omitting it means "judge
    by the shared ceiling", which is what every caller wanted before per-mode
    ceilings existed.
    """
    if priced.currency != route.max_price_currency:
        return False
    return priced.price_minor <= threshold_for(route, modes)
