from __future__ import annotations

from datetime import datetime, timezone

from aviapars.adapters.base import Mode
from aviapars.domain.combiner import Itinerary

MODE_EMOJI: dict[Mode, str] = {
    Mode.PLANE: "✈️",
    Mode.TRAIN: "🚂",
}


def format_mode_label(modes: list[Mode]) -> str:
    """«✈️+🚂 через Москву» style label. Works for any number/order of modes,
    including a future third mode — see plans/squishy-scribbling-dawn.md §5."""
    return "+".join(MODE_EMOJI[m] for m in modes)


_CURRENCY_SUFFIX = {"RUB": "₽", "USD": "$", "EUR": "€"}


def format_money(price_minor: int, currency: str) -> str:
    """price_minor is always the smallest unit (kopecks/cents) — see §4 schema."""
    major = price_minor // 100
    suffix = _CURRENCY_SUFFIX.get(currency, currency)
    return f"{major:,}".replace(",", " ") + f" {suffix}"


def format_deal_card(route_title: str, itinerary: Itinerary, *, threshold_minor: int, confirmed: bool) -> str:
    """Deal notification card (§9/§10). Degrades gracefully when leg detail is
    missing (date_only/no-carrier discovery offers) rather than fabricating
    a time or airline — see base.py Leg docstring. `itinerary.offers` has one
    entry for a direct trip, two for a hub-composed one (§8) — each offer can
    come from a different source with its own tier/confidence, so per-leg
    price is shown whenever there's more than one."""
    price = format_money(itinerary.price_minor, itinerary.currency)
    threshold = format_money(threshold_minor, itinerary.currency)
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    mode_label = format_mode_label([leg.mode for leg in all_legs])
    depart_date = min(leg.depart_utc for leg in all_legs).date()

    lines = [
        f"🔥 {route_title}   {price}  (порог: {threshold})",
        f"   {depart_date.strftime('%d.%m')} · {mode_label}",
    ]
    multi_leg = len(itinerary.offers) > 1
    for offer in itinerary.offers:
        for leg in offer.legs:
            origin = leg.from_place.iata or str(leg.from_place.place_id)
            dest = leg.to_place.iata or str(leg.to_place.place_id)
            carrier_bits = " ".join(b for b in (leg.carrier, leg.number) if b)
            time_str = leg.depart_local.strftime("%H:%M") if leg.time_precision == "exact" else "время неизвестно"
            carrier_str = f"{carrier_bits}  " if carrier_bits else ""
            price_str = f"  {format_money(offer.price_minor, offer.currency)}" if multi_leg else ""
            lines.append(f"   {MODE_EMOJI[leg.mode]} {origin} → {dest}  {carrier_str}{time_str}{price_str}")

    if itinerary.duration_minutes is not None:
        h, m = divmod(itinerary.duration_minutes, 60)
        lines.append(f"   В пути: {h}ч {m}м" if m else f"   В пути: {h}ч")

    if confirmed:
        lines.append("   ✅ Подтверждено · только что")
    else:
        cached_ats = [o.source_cached_at for o in itinerary.offers if o.source_cached_at is not None]
        age_h = _age_hours(min(cached_ats)) if cached_ats else None
        sources = ", ".join(sorted({o.source_id for o in itinerary.offers}))
        age_str = f"{age_h} ч назад" if age_h is not None else "возраст неизвестен"
        lines.append(f"   ⚠️ Не подтверждено · данные {sources}, {age_str}")

    return "\n".join(lines)


def _age_hours(cached_at: datetime | None) -> int | None:
    if cached_at is None:
        return None
    return int((datetime.now(timezone.utc) - cached_at).total_seconds() // 3600)
