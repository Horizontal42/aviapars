from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Mapping

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier, leg_duration_minutes
from aviapars.db.models import Route
from aviapars.domain.combiner import Itinerary, itinerary_modes, itinerary_offers
from aviapars.domain.dates import DatePlan, DateSelection, selection_from_route, window
from aviapars.domain.grouping import Match
from aviapars.telegram.keyboards import MODE_LABEL

MODE_EMOJI: dict[Mode, str] = {
    Mode.PLANE: "✈️",
    Mode.TRAIN: "🚂",
}

# Nominative and genitive («вылет 07:35» / «время вылета источник не знает»). Split by mode
# because «вылет»/«прилёт» are aircraft words: a train departs and arrives, and Russian rail
# says «отправление»/«прибытие» — what is actually printed on an RZD ticket and on station
# boards. Arrival is one word for both modes, so it isn't in the table.
_DEPART_LABEL: dict[Mode, tuple[str, str]] = {
    Mode.PLANE: ("вылет", "вылета"),
    Mode.TRAIN: ("отправление", "отправления"),
}


def screen_text(title: str, *blocks: str) -> str:
    """Every screen the bot draws has the same shape: a bold title, then
    optional context, then the question. Worth having one place for it now that
    all of them are literally the same Telegram message being rewritten —
    inconsistent headings read as glitches rather than as separate screens."""
    return "\n\n".join([f"<b>{title}</b>", *(block for block in blocks if block)])


def describe_draft(data: Mapping[str, object], today: date) -> str:
    """«Екатеринбург → Сочи · ✈️ Самолёт · 25.09.2026 – 05.10.2026» — what the
    wizard has collected so far.

    Only worth showing since the wizard became one evolving message: the earlier
    answers used to be visible right above the current prompt, and now they
    aren't, so the screen has to carry them itself.
    """
    parts: list[str] = []
    origin, dest = data.get("origin_title"), data.get("dest_title")
    if isinstance(origin, str) and isinstance(dest, str):
        parts.append(f"{origin} → {dest}")
    elif isinstance(origin, str):
        parts.append(f"{origin} → …")
    mode = data.get("mode_pref")
    if isinstance(mode, str) and mode in MODE_LABEL:
        parts.append(MODE_LABEL[mode])
    selection = data.get("date_selection")
    if isinstance(selection, DateSelection):
        parts.append(describe_selection(selection, today))
    return " · ".join(parts)


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


def place_label(ref: PlaceRef) -> str:
    """«Москва Шереметьево (SVO)» instead of a bare 3-letter code — the
    picker step still shows codes in parens on purpose (keyboards.py), this
    is only for screens where the code alone means nothing without context."""
    if ref.name_ru and ref.iata:
        return f"{ref.name_ru} ({ref.iata})"
    return ref.name_ru or ref.iata or str(ref.place_id)


def format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def describe_selection(sel: DateSelection, today: date) -> str:
    """One-line «какие даты отслеживаем» for route cards and confirmations."""
    days = window(sel, today)
    if not days:
        return "даты уже прошли"
    if sel.mode == "fixed":
        return ", ".join(format_date(d) for d in days)
    span = f"{format_date(days[0])} – {format_date(days[-1])}"
    if sel.window_start is None:
        return f"{span} ({len(days)} дн., окно едет вперёд)"
    return f"{span} ({len(days)} дн.)"


def describe_checked_dates(sel: DateSelection, plan: DatePlan, today: date) -> str:
    """Says what was *actually* queried, not what was asked for. A wide window
    is never fully scanned by one-request-per-date sources in a single pass
    (§6), so claiming otherwise would be a lie the user can't check."""
    total = len(window(sel, today))
    if sel.mode == "fixed" or len(plan.probe_dates) >= total:
        return f"Проверено дат: {total}."
    probed = ", ".join(format_date(d) for d in plan.probe_dates)
    return (
        f"Из окна в {total} дн. точечно проверено {len(plan.probe_dates)}: {probed}. "
        f"Источники с пакетной выдачей смотрят всё окно целиком."
    )


_NOTIFICATION_MODE_LABEL = {
    "instant": "⚡ сразу, как найдётся",
    # Not "лучшая цена за сутки": every crossing of the threshold is still its own
    # message, digest only holds them until quiet hours end (см. ARCHITECTURE.md).
    "digest": "📬 раз в день, все находки разом",
}


def describe_notification_mode(mode: str) -> str:
    return _NOTIFICATION_MODE_LABEL.get(mode, mode)


def describe_age(moment: datetime, now: datetime) -> str:
    """Relative, human-sized. A raw ISO timestamp in a status list is something
    the reader has to subtract in their head to get the one fact they wanted."""
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн. назад"


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


def format_route_card(route: Route, today: date) -> str:
    """The tracked-route card. Same block in both places it appears — the
    /add confirmation and /routes — so "what I saved" and "what I'm watching"
    can't drift into two different-looking descriptions of one route."""
    status = "▶️ активен" if route.active else "⏸ на паузе"
    return screen_text(
        route.title,
        "\n".join(
            [
                f"🗓 {describe_selection(selection_from_route(route), today)}",
                f"💰 Порог: {describe_thresholds(route.max_price, route.max_price_currency, route.max_price_by_mode)}",
                f"⏱ Проверяю каждые {route.poll_interval_min} мин · {status}",
                f"🔔 Уведомления: {describe_notification_mode(route.notification_mode)}",
            ]
        ),
    )


_MATCH_NOTE = {
    Match.SAME_FLIGHT: "🔁 Один и тот же рейс у разных источников — совпал номер рейса",
    Match.UNVERIFIED: "❔ Один это рейс или разные — проверить нечем: совпадают только маршрут и дата",
}


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
    a time or airline — see base.py Leg docstring.

    Four blocks, blank-line separated: what/how much, the journey itself, how
    sure we are it is one journey, where to buy it. Everything crammed onto
    fewer lines wrapped mid-word in the Telegram client and read as a wall of
    text — the owner's actual complaint about the old card.

    `itinerary.offers` is the journey's legs (one direct, two hub-composed §8);
    `itinerary.alternates` is a different thing entirely — other sources'
    reports of that same trip, grouped by domain/grouping.py. The buy block
    lists a line per source when they were grouped, and a line per leg when the
    trip is hub-composed (the two never co-occur, see combiner.Itinerary).

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

    lines = [
        f"<b>🔥 {route_title}</b>",
        f"💰 <b>{price}</b> · порог {threshold}",
        f"🗓 {depart_date.strftime('%d.%m.%Y')} · {mode_label}",
        "",
        *_journey_block(all_legs, itinerary),
    ]

    if itinerary.alternates and itinerary.match is not None:
        lines += ["", _MATCH_NOTE[itinerary.match]]

    lines += ["", *_source_block(itinerary, confirmed=confirmed, source_names=source_names)]
    return "\n".join(lines)


def _journey_block(legs: list[Leg], itinerary: Itinerary) -> list[str]:
    """Per leg: where, then a labelled 🕑 line, then who flies it. Layover in between.

    Departure, arrival and duration each get a word in front of them and live on the
    line the reader is already looking at. The first version put the trip duration in
    the header next to the date and the times in an indented continuation line, and the
    owner reported all three as missing — technically present, actually unreadable.

    Every value is shown exactly when it is known, and named as unknown otherwise:
    - departure/arrival come from the *local* clock a source stated (`depart_local`,
      `arrive_local`), never from a UTC stamp plus the departure offset — a leg that
      crosses zones lands on a different one, so that arithmetic invents a time.
    - duration is computed from the UTC stamps instead (`leg_duration_minutes`), which
      needs no timezone at all and is therefore right even across zones. A date_only
      discovery offer has no stamps but does state a duration, so a single-leg trip
      falls back to the offer's own figure — that is the one real fact those offers
      carry, and it must not be dropped.
    """
    lines: list[str] = []
    for i, leg in enumerate(legs):
        if i:
            # blank lines on both sides: the transfer is the seam between two flights, and
            # run into them it read as just another detail line of the leg above it
            lines += ["", _layover_line(legs[i - 1], leg), ""]
        lines.append(f"{MODE_EMOJI[leg.mode]} {place_label(leg.from_place)} → {place_label(leg.to_place)}")

        duration = leg_duration_minutes(leg)
        if duration is None and len(legs) == 1:
            duration = itinerary.duration_minutes
        departure, departure_genitive = _DEPART_LABEL[leg.mode]
        bits: list[str] = []
        if leg.time_precision == "exact":
            bits.append(f"{departure} {leg.depart_local.strftime('%H:%M')}")
            if leg.arrive_local is not None:
                bits.append(f"прибытие {leg.arrive_local.strftime('%H:%M')}")
        else:
            bits.append(f"время {departure_genitive} источник не знает")
        bits.append(f"в пути {_describe_duration(duration)}")
        lines.append("🕑 " + " · ".join(bits))

        carrier_bits = " · ".join(b for b in (leg.carrier, f"рейс {leg.number}" if leg.number else None) if b)
        if carrier_bits:
            lines.append(f"🎫 {carrier_bits}")

    if len(legs) > 1:
        lines += ["", f"⏱ Всего в пути {_describe_duration(itinerary.duration_minutes)}"]
    return lines


def _layover_line(previous: Leg, following: Leg) -> str:
    """«🔄 Пересадка 1ч 40м · Москва (SVO)» — both the wait and where you wait it
    out, which is what decides whether a connection is worth taking. The gap needs a
    real arrival stamp on the previous leg; a date_only offer has only a midnight
    placeholder, so the place is stated on its own rather than an invented duration."""
    place = place_label(previous.to_place)
    if previous.arrive_utc is None or previous.time_precision != "exact" or following.time_precision != "exact":
        return f"🔄 Пересадка · {place}"
    gap = int((following.depart_utc - previous.arrive_utc).total_seconds() // 60)
    if gap < 0:
        return f"🔄 Пересадка · {place}"
    return f"🔄 Пересадка {_describe_duration(gap)} · {place}"


def _source_block(itinerary: Itinerary, *, confirmed: bool, source_names: dict[str, str]) -> list[str]:
    """Freshness verdict, then where to actually buy this.

    Two shapes, never both (see combiner.Itinerary): cross-source alternates are
    competing prices for one trip, so they're listed cheapest-first with each source's
    own tier marker; a hub itinerary's offers are legs, so they stay in travel order
    and say which leg they sell.
    """
    offers = itinerary_offers(itinerary)
    if confirmed:
        lines = ["✅ Подтверждено у перевозчика · только что"]
    else:
        cached_ats = [o.source_cached_at for o in offers if o.source_cached_at is not None]
        age_h = _age_hours(min(cached_ats)) if cached_ats else None
        lines = [f"⚠️ Не подтверждено · данные {f'{age_h} ч назад' if age_h is not None else 'неизвестной свежести'}"]

    if itinerary.alternates:
        for offer in sorted(offers, key=lambda o: o.price_minor):
            marker = "✅" if offer.tier == Tier.CONFIRMATION else "⚠️"
            price = format_money(offer.price_minor, offer.currency)
            lines.append(f"{marker} {price} · {_buy_link(offer, source_names)}")
        return lines

    # Deduped by link: both legs of a hub itinerary often come from one source with one
    # search URL covering the whole trip, and printing it twice is pure noise.
    seen: set[tuple[str, str | None]] = set()
    legs_to_buy: list[Offer] = []
    for offer in itinerary.offers:
        if (offer.source_id, offer.deep_link) in seen:
            continue
        seen.add((offer.source_id, offer.deep_link))
        legs_to_buy.append(offer)

    for offer in legs_to_buy:
        prefix = ""
        if len(legs_to_buy) > 1:
            legs = offer.legs
            prefix = f"{place_label(legs[0].from_place)} → {place_label(legs[-1].to_place)}: "
            prefix += f"{format_money(offer.price_minor, offer.currency)} · "
        lines.append(f"💳 {prefix}{_buy_link(offer, source_names)}")
    return lines


def _buy_link(offer: Offer, source_names: dict[str, str]) -> str:
    """HTML anchor — parse_mode=HTML is the bot's global default (telegram/bot.py), so a
    bare URL in the text was just a long unclickable string wrapping across three lines."""
    name = source_names.get(offer.source_id, offer.source_id)
    if not offer.deep_link:
        return f"{name} (без ссылки)"
    return f'<a href="{offer.deep_link}">купить у {name}</a>'


def _describe_duration(minutes: int | None) -> str:
    """Always says something, even «неизвестно» — an omitted duration read as
    "no data" and "adapter forgot to fill duration_minutes" identically."""
    if minutes is None:
        return "неизвестно"
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m}м"
    return f"{h}ч {m:02d}м" if m else f"{h}ч"


def _age_hours(cached_at: datetime | None) -> int | None:
    if cached_at is None:
        return None
    # clamped like describe_age: a source_cached_at a hair in the future (clock skew
    # between the source and this box) must not read as «-1 ч назад».
    return max(0, int((datetime.now(timezone.utc) - cached_at).total_seconds() // 3600))
