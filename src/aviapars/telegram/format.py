from __future__ import annotations

from datetime import date, datetime, timezone

from aviapars.adapters.base import Mode
from aviapars.domain.combiner import Itinerary, itinerary_modes
from aviapars.domain.dates import DatePlan, DateSelection, window

MODE_EMOJI: dict[Mode, str] = {
    Mode.PLANE: "✈️",
    Mode.TRAIN: "🚂",
}


def format_mode_label(modes: list[Mode]) -> str:
    """«✈️+🚂 через Москву» style label. Works for any number/order of modes,
    including a future third mode — see plans/squishy-scribbling-dawn.md §5."""
    return "+".join(MODE_EMOJI[m] for m in modes)


MIXED_GROUP = "mixed"
# Order results are presented in — same order as the mode picker's buttons.
MODE_GROUP_ORDER = (Mode.PLANE.value, Mode.TRAIN.value, MIXED_GROUP)
MODE_GROUP_TITLE = {
    Mode.PLANE.value: "✈️ Авиабилеты",
    Mode.TRAIN.value: "🚂 ЖД-билеты",
    MIXED_GROUP: "🔀 Смешанные (через хаб)",
}


def mode_group(itinerary: Itinerary) -> str:
    """Which results section an itinerary belongs to.

    A hub itinerary that mixes a plane leg with a train leg belongs to neither
    single-mode group — it is not "a plane price" or "a train price", it is its
    own combined thing — so it gets its own section rather than being filed
    under whichever leg happens to come first.
    """
    distinct = {m.value for m in itinerary_modes(itinerary)}
    return distinct.pop() if len(distinct) == 1 else MIXED_GROUP


def _group_rank(group: str) -> int:
    return MODE_GROUP_ORDER.index(group) if group in MODE_GROUP_ORDER else len(MODE_GROUP_ORDER)


def select_top_itineraries(itineraries: list[Itinerary], *, limit: int) -> list[Itinerary]:
    """Pick what the results screen shows, grouped by transport (§10).

    Straight "cheapest N overall" hid whole modes: a route where trains
    undercut planes filled all five slots with trains, and the plane price the
    user asked about was simply absent. So every group present gets its cheapest
    option first, and only the remaining slots go to the global price order.

    Returns a flat list, deliberately: `SearchTrack` callback data indexes into
    it by position (see `handlers/search.on_search_track`), and rendering it
    under per-group headers must not change that contract. Sorted by group, then
    price, so the groups come out contiguous and the rendered numbering is
    still ascending down the screen.
    """
    groups: dict[str, list[Itinerary]] = {}
    for itinerary in sorted(itineraries, key=lambda it: it.price_minor):
        groups.setdefault(mode_group(itinerary), []).append(itinerary)

    picked: list[Itinerary] = []
    seen: set[int] = set()
    for group in sorted(groups, key=_group_rank):
        if len(picked) >= limit:
            break
        cheapest = groups[group][0]
        picked.append(cheapest)
        seen.add(id(cheapest))
    for itinerary in sorted(itineraries, key=lambda it: it.price_minor):
        if len(picked) >= limit:
            break
        if id(itinerary) not in seen:
            picked.append(itinerary)
            seen.add(id(itinerary))

    picked.sort(key=lambda it: (_group_rank(mode_group(it)), it.price_minor))
    return picked


def format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def describe_selection(sel: DateSelection, today: date) -> str:
    """One-line «какие даты отслеживаем» for route cards and confirmations."""
    days = window(sel, today)
    if not days:
        return "даты уже прошли"
    if sel.mode == "fixed":
        return ", ".join(format_date(d) for d in days)
    if sel.window_start is None:
        return f"любая дата: {format_date(days[0])} – {format_date(days[-1])} ({len(days)} дн., окно едет вперёд)"
    return f"{format_date(days[0])} – {format_date(days[-1])} ({len(days)} дн.)"


def describe_checked_dates(sel: DateSelection, plan: DatePlan, today: date) -> str:
    """Says what was *actually* queried, not what was asked for. A wide window
    is never fully scanned by one-request-per-date sources in a single pass
    (§6), so claiming otherwise would be a lie the user can't check."""
    total = len(window(sel, today))
    if sel.mode == "fixed" or len(plan.probe_dates) >= total:
        return f"Проверено дат: {total}"
    probed = ", ".join(format_date(d) for d in plan.probe_dates)
    return (
        f"Из окна в {total} дн. точечно проверено {len(plan.probe_dates)}: {probed}. "
        f"Источники с пакетной выдачей отдают всё окно целиком."
    )


_NOTIFICATION_MODE_LABEL = {
    "instant": "⚡ сразу, как найдётся",
    "digest": "📬 раз в день, лучшая цена за сутки",
}


def describe_notification_mode(mode: str) -> str:
    return _NOTIFICATION_MODE_LABEL.get(mode, mode)


_CURRENCY_SUFFIX = {"RUB": "₽", "USD": "$", "EUR": "€"}


def format_money(price_minor: int, currency: str) -> str:
    """price_minor is always the smallest unit (kopecks/cents) — see §4 schema."""
    major = price_minor // 100
    suffix = _CURRENCY_SUFFIX.get(currency, currency)
    return f"{major:,}".replace(",", " ") + f" {suffix}"


def describe_thresholds(max_price: int, currency: str, by_mode: dict[str, int] | None) -> str:
    """«8 000 ₽» for one shared ceiling, «✈️ 8 000 ₽ · 🚂 4 000 ₽» when the route
    carries separate ones — route cards and the /add confirmation both have to
    show what's actually in effect, not just the `max_price` column."""
    if not by_mode:
        return format_money(max_price, currency)
    # Mode declaration order, which is also the order the wizard asks in.
    parts = [f"{MODE_EMOJI[m]} {format_money(by_mode[m.value], currency)}" for m in Mode if m.value in by_mode]
    return " · ".join(parts) if parts else format_money(max_price, currency)


def format_deal_card(
    route_title: str,
    itinerary: Itinerary,
    *,
    threshold_minor: int,
    confirmed: bool,
    source_names: dict[str, str] | None = None,
) -> str:
    """Deal notification card (§9/§10). Degrades gracefully when leg detail is
    missing (date_only/no-carrier discovery offers) rather than fabricating
    a time or airline — see base.py Leg docstring. `itinerary.offers` has one
    entry for a direct trip, two for a hub-composed one (§8) — each offer can
    come from a different source with its own tier/confidence, so per-leg
    price is shown whenever there's more than one.

    `source_names` maps adapter `id` -> `display_name` (built once from the
    live registry, see notify/notifier.py) so the card can say "Победа",
    not the raw "pobeda_direct" slug; falls back to the slug if the map
    wasn't passed (keeps this function usable standalone/in tests).

    Used unchanged for both `instant` and `digest` notification modes (§9) —
    a digest message is the same card as an instant one, just delivered
    later alongside the day's other qualifying deals; see notify/notifier.py.
    """
    source_names = source_names or {}
    price = format_money(itinerary.price_minor, itinerary.currency)
    threshold = format_money(threshold_minor, itinerary.currency)
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    mode_label = format_mode_label([leg.mode for leg in all_legs])
    # local, not UTC: a 00:35 Moscow departure or a 09:00 Vladivostok one both read back a
    # day early off depart_utc.date() — the user needs the date they'd see on a ticket.
    depart_date = all_legs[0].depart_local.date()

    headline = f"🔥 {route_title}   {price}  (порог: {threshold})"
    lines = [headline, f"   {depart_date.strftime('%d.%m')} · {mode_label}"]
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

    source_str = ", ".join(sorted({source_names.get(o.source_id, o.source_id) for o in itinerary.offers}))
    if confirmed:
        lines.append(f"   ✅ Подтверждено · только что · {source_str}")
    else:
        cached_ats = [o.source_cached_at for o in itinerary.offers if o.source_cached_at is not None]
        age_h = _age_hours(min(cached_ats)) if cached_ats else None
        age_str = f"{age_h} ч назад" if age_h is not None else "возраст неизвестен"
        lines.append(f"   ⚠️ Не подтверждено · данные {source_str}, {age_str}")

    seen_links: set[str] = set()
    for offer in itinerary.offers:
        if offer.deep_link and offer.deep_link not in seen_links:
            seen_links.add(offer.deep_link)
            name = source_names.get(offer.source_id, offer.source_id)
            lines.append(f"   🔗 Купить у {name}: {offer.deep_link}")

    return "\n".join(lines)


def _age_hours(cached_at: datetime | None) -> int | None:
    if cached_at is None:
        return None
    return int((datetime.now(timezone.utc) - cached_at).total_seconds() // 3600)
