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


def _amount(variant: dict[str, Any]) -> float | None:
    """None means unusable — missing/non-numeric `value.amount` — never a defect to crash
    the whole batch over; the caller just treats this variant as absent."""
    value = variant.get("price", {}).get("value")
    if not isinstance(value, dict):
        return None
    amount = value.get("amount")
    return amount if isinstance(amount, (int, float)) else None


def select_price_variant(variants: list[dict[str, Any]]) -> tuple[dict[str, Any], float, Tier] | None:
    """Pick the cheapest usable price variant. Returns (variant, confidence, tier), or
    None if every variant is "absent" (no real or estimated price at all) or malformed."""

    def usable(variant: dict[str, Any], price_type: str) -> bool:
        return variant.get("price", {}).get("type") == price_type and _amount(variant) is not None

    exact = [v for v in variants if usable(v, "exact")]
    if exact:
        return min(exact, key=_amount), 1.0, Tier.CONFIRMATION  # type: ignore[arg-type]

    approximate = [v for v in variants if usable(v, "approximate")]
    if approximate:
        return min(approximate, key=_amount), 0.5, Tier.DISCOVERY  # type: ignore[arg-type]

    return None
