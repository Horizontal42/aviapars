from __future__ import annotations

from typing import Any

from aviapars.adapters.base import Tier

# Shared between tutu_rail_direct and tutu_avia_direct — same offers-api.tutu.ru platform,
# identical offerVariants/price shape on both verticals.
#
# price.type: "exact" (real fare, sales open — genuinely confirmation-tier), "approximate"
# (sales haven't opened yet for this train/flight — Tutu shows an estimate based on past
# fares, not a bookable price), "absent" (no seats / waitlist placeholder, amount=0).
# Verified live: dates far enough out can come back 100% "approximate"/"absent" with zero
# "exact" offers (RZD/airline sales-open horizon not yet reached) — dropping "approximate"
# outright made both adapters return EMPTY for most of a route's monitored date window.
#
# An "approximate" price is tagged Tier.DISCOVERY, not CONFIRMATION — the bot's confirmed/✅
# display (scheduler/worker.py, telegram/handlers/search.py) checks tier alone and every
# other confirmation-tier adapter always emits confidence=1.0, so tier=CONFIRMATION here
# would show an estimate to the user as "✅ Подтверждено" for a price that isn't bookable.


def select_price_variant(variants: list[dict[str, Any]]) -> tuple[dict[str, Any], float, Tier] | None:
    """Pick the cheapest usable price variant. Returns (variant, confidence, tier), or
    None if every variant is "absent" (no real or estimated price at all)."""
    exact = [v for v in variants if v.get("price", {}).get("type") == "exact"]
    if exact:
        return min(exact, key=lambda v: v["price"]["value"]["amount"]), 1.0, Tier.CONFIRMATION

    approximate = [v for v in variants if v.get("price", {}).get("type") == "approximate"]
    if approximate:
        return min(approximate, key=lambda v: v["price"]["value"]["amount"]), 0.5, Tier.DISCOVERY

    return None
