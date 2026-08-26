from __future__ import annotations

from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from aviapars.adapters.base import Mode, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.models import Place, Route
from aviapars.domain.combiner import Itinerary, SearchOpts, itinerary_offers, search_itineraries
from aviapars.domain.dates import DateSelection, resolve_date_plan, selection_from_route
from aviapars.domain.places import modes_for_pref
from aviapars.telegram import date_picker
from aviapars.telegram.format import (
    MODE_GROUP_TITLE,
    describe_checked_dates,
    describe_draft,
    describe_selection,
    format_date,
    format_mode_label,
    format_money,
    mode_group,
    place_label,
    screen_text,
    select_top_itineraries,
)
from aviapars.telegram.handlers import add as add_handlers
from aviapars.telegram.keyboards import (
    MODE_LABEL,
    DatePick,
    ModePick,
    PlacePick,
    SearchShowAll,
    SearchTrack,
    date_mode_kb,
    menu_only_kb,
    mode_picker_kb,
    search_again_kb,
    search_results_kb,
)
from aviapars.telegram.place_picker import ask_place_pick, resolve_pick
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import AddRoute, SearchFlow

router = Router(name="search")

TITLE = "🔎 Разовый поиск"

_RESULTS_SHOWN = 5
# "Показать все" shows up to this many, not literally every itinerary found — a wall of 80
# one-line results would blow past Telegram's 4096-char message cap and stop being readable
# long before that. If the true count is higher, the header says how many were actually found
# vs shown, same honesty rule as `describe_checked_dates` for probed dates.
_RESULTS_MAX = 25

# Same reasoning as handlers/add.py's constant of the same name — these handlers have no
# content-type filter, so a sticker/photo/voice message mid-wizard used to hit an unguarded
# assert and crash the update silently instead of getting a friendly re-prompt.
_NON_TEXT_ERROR = "⚠️ Не понял. Пришлите ответ текстом."


async def step(screen: Screen, state: FSMContext, prompt: str, *, reply_markup: InlineKeyboardMarkup | None) -> None:
    """Same one-screen shape as the /add wizard (handlers/add.step) — title,
    what's chosen so far, then the question."""
    data = await state.get_data()
    await screen.show(screen_text(TITLE, describe_draft(data, date.today()), prompt), reply_markup=reply_markup)


async def start_wizard(screen: Screen, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchFlow.origin)
    await step(
        screen,
        state,
        "Ищу варианты прямо сейчас и ничего не сохраняю.\n\nОткуда едем? Напишите город.",
        reply_markup=menu_only_kb(),
    )


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    await start_wizard(await screen_for(message, state), state)


async def start_from_route(
    screen: Screen, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry, route: Route
) -> None:
    """Bridge from a route card's «🔎 Варианты» (§10) — the same one-shot search,
    pre-filled from the route's own origin/destination/dates, nothing saved."""
    await state.clear()
    places_repo = PlacesRepo(db)
    origin = await places_repo.get(route.origin_place_id)
    dest = await places_repo.get(route.dest_place_id)
    assert origin is not None and dest is not None
    await state.update_data(
        origin_place_id=origin.id,
        origin_title=origin.name_ru,
        dest_place_id=dest.id,
        dest_title=dest.name_ru,
        # Not asked again: the user already chose the transport when creating the route,
        # and this button means "show me what's out there for *this* route".
        mode_pref=route.mode_pref,
    )
    await _run_search(screen, state, db, registry, selection_from_route(route))


async def _origin_picked(screen: Screen, state: FSMContext, place: Place) -> None:
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(SearchFlow.destination)
    await step(screen, state, "Куда едем? Напишите город.", reply_markup=menu_only_kb())


async def _destination_picked(screen: Screen, state: FSMContext, place: Place) -> None:
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(SearchFlow.mode)
    await step(screen, state, "Чем добираемся?", reply_markup=mode_picker_kb())


@router.message(SearchFlow.origin)
async def on_search_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    screen = await screen_for(message, state)
    if message.text is None:
        await step(screen, state, _NON_TEXT_ERROR, reply_markup=menu_only_kb())
        return
    await ask_place_pick(
        screen,
        state,
        db,
        title=TITLE,
        text=message.text,
        pick_state=SearchFlow.origin_pick,
        storage_key="candidates_origin",
        on_picked=lambda place: _origin_picked(screen, state, place),
    )


@router.callback_query(SearchFlow.origin_pick, PlacePick.filter())
async def on_search_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_origin"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /search", show_alert=True)
        return
    await _origin_picked(await screen_for(query, state), state, place)
    await query.answer()


@router.message(SearchFlow.destination)
async def on_search_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    screen = await screen_for(message, state)
    if message.text is None:
        await step(screen, state, _NON_TEXT_ERROR, reply_markup=menu_only_kb())
        return
    await ask_place_pick(
        screen,
        state,
        db,
        title=TITLE,
        text=message.text,
        pick_state=SearchFlow.destination_pick,
        storage_key="candidates_dest",
        on_picked=lambda place: _destination_picked(screen, state, place),
    )


@router.callback_query(SearchFlow.destination_pick, PlacePick.filter())
async def on_search_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_dest"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /search", show_alert=True)
        return
    await _destination_picked(await screen_for(query, state), state, place)
    await query.answer()


@router.callback_query(SearchFlow.mode, ModePick.filter())
async def on_search_mode_pick(query: CallbackQuery, callback_data: ModePick, state: FSMContext) -> None:
    await state.update_data(mode_pref=callback_data.value)
    await state.set_state(SearchFlow.date_mode)
    await step(await screen_for(query, state), state, "Когда едем?", reply_markup=date_mode_kb())
    await query.answer()


@router.callback_query(SearchFlow.date_mode, DatePick.filter())
async def on_search_date_mode_pick(
    query: CallbackQuery, callback_data: DatePick, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    screen = await screen_for(query, state)
    ready = date_picker.selection_for(callback_data.kind)
    if ready is not None:
        await query.answer()  # answered up front: the search below outlives Telegram's callback deadline
        await _run_search(screen, state, db, registry, ready)
        return
    prompt = date_picker.prompt_for(callback_data.kind)
    assert prompt is not None
    await state.update_data(date_kind=callback_data.kind)
    await state.set_state(SearchFlow.date_value)
    await step(screen, state, prompt, reply_markup=menu_only_kb())
    await query.answer()


@router.message(SearchFlow.date_value)
async def on_search_date_value_text(
    message: Message, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    screen = await screen_for(message, state)
    if message.text is None:
        await step(screen, state, _NON_TEXT_ERROR, reply_markup=menu_only_kb())
        return
    data = await state.get_data()
    parsed = date_picker.parse_selection(data["date_kind"], message.text, date.today())
    if isinstance(parsed, str):
        await step(screen, state, parsed, reply_markup=menu_only_kb())
        return
    await _run_search(screen, state, db, registry, parsed)


async def _run_search(
    screen: Screen,
    state: FSMContext,
    db: aiosqlite.Connection,
    registry: AdapterRegistry,
    selection: DateSelection,
) -> None:
    """One synchronous pass under the interactive budget (§6 L1).

    A flexible window is deliberately NOT scanned in full here regardless of how
    long `interactive_l1_deadline_s` allows a single call to run: one-request-
    per-date sources would fan out into dozens of requests for a 30-day window,
    so only `flex_search_probe_dates` of them are probed and the reply says
    exactly which — the full window is what background tracking is for. Batch
    sources still return the whole window, so wide-window results do show up.
    """
    today = date.today()
    data = await state.get_data()
    # Mode is shown here explicitly, not just implied by which offers turn up — a plane-only
    # search and "trains genuinely don't exist on this route" look identical in the results
    # otherwise, and the mode step is easy to tap through on habit without noticing the choice.
    mode_label = MODE_LABEL.get(data.get("mode_pref", "auto"), "")
    title = f"🔎 {data['origin_title']} → {data['dest_title']} · {mode_label}"
    plan, _ = resolve_date_plan(selection, today=today, probe_budget=settings.flex_search_probe_dates)
    if plan.is_empty:
        await screen.show(
            screen_text(title, "⚠️ В этом окне не осталось будущих дат."), reply_markup=search_again_kb()
        )
        await state.clear()
        return

    places_repo = PlacesRepo(db)
    origin = await places_repo.get(data["origin_place_id"])
    dest = await places_repo.get(data["dest_place_id"])
    assert origin is not None and dest is not None

    await screen.show(
        screen_text(title, describe_selection(selection, today), "⏳ Ищу варианты, в том числе через хаб…")
    )
    # A mode with no registered adapter (or no coverage for these places) just contributes
    # zero offers — search_itineraries degrades to whatever sources actually answered.
    # `.get` with a default covers the one entry point that skips the picker on purpose
    # (start_from_route, which takes the route's own mode_pref) plus any stale FSM state.
    outcome = await search_itineraries(
        registry,
        places_repo,
        origin=origin,
        dest=dest,
        dates=plan,
        pax=1,
        modes=modes_for_pref(data.get("mode_pref", "auto")),
        opts=SearchOpts(
            tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}),
            allow_network=True,
            # A hard budget, unlike the scheduler's: the user is watching the chat, so a
            # truncated answer beats a slow one. `lane` is what tells the registry to treat
            # it that way (and routes /search onto its own http_concurrency_interactive slot
            # instead of competing with the scheduler for the global ones).
            deadline_s=settings.interactive_l1_deadline_s,
            lane="interactive",
            max_hops=1,
            hub_probe_dates_cap=settings.flex_hub_probe_dates,
        ),
    )

    checked = describe_checked_dates(selection, plan, today)
    if not outcome.itineraries:
        skipped = ", ".join(f"{sid} ({reason})" for sid, reason in outcome.sources_skipped)
        note = f"\nНе ответили: {skipped}." if skipped else ""
        await screen.show(
            screen_text(title, describe_selection(selection, today), f"Ничего не нашёл.\n{checked}{note}"),
            reply_markup=search_again_kb(),
        )
        await state.clear()
        return

    gap_note = _mode_gap_note(
        modes_for_pref(data.get("mode_pref", "auto")), outcome.itineraries, outcome.sources_skipped, registry
    )
    if gap_note:
        checked = f"{checked}\n{gap_note}"

    await state.update_data(
        results_all_itineraries=select_top_itineraries(outcome.itineraries, limit=_RESULTS_MAX),
        results_found_total=len(outcome.itineraries),
        results_route_title=f"{data['origin_title']} → {data['dest_title']}",
        results_title=title,
        results_selection=selection,
        results_checked=checked,
        results_source_names={a.id: a.display_name for a in registry.all()},
    )
    await state.set_state(SearchFlow.results)
    await _render_results(screen, state, limit=_RESULTS_SHOWN)


def _mode_gap_note(
    modes: frozenset[Mode],
    itineraries: list[Itinerary],
    sources_skipped: list[tuple[str, str]],
    registry: AdapterRegistry,
) -> str:
    """When one whole requested mode comes back with zero itineraries while
    another mode found plenty, the results screen otherwise looks identical
    to "trains genuinely don't exist here" — say which sources were asked
    for that mode and why they didn't contribute (adapter EMPTY/blocked/
    missing-code reasons), instead of silently dropping the mode."""
    missing_set = set(modes)
    for it in itineraries:
        for offer in it.offers:
            for leg in offer.legs:
                missing_set.discard(leg.mode)
        if not missing_set:
            break

    if not missing_set:
        return ""

    missing = [m for m in modes if m in missing_set]
    adapters_by_id = {a.id: a for a in registry.all()}
    lines: list[str] = []
    for mode in missing:
        # One /search can call the same adapter several times (direct leg + one
        # call per hub candidate — see combiner.search_itineraries), so the same
        # (source, reason) pair often repeats 2-4x; dict.fromkeys collapses exact
        # repeats while keeping distinct reasons for the same source (e.g. a
        # direct-leg TIMEOUT next to a hub-leg RATE_LIMITED is real signal, not
        # noise — that's the rate-limit bucket contention itself showing through).
        reasons = list(
            dict.fromkeys(
                f"{adapters_by_id[sid].display_name} ({reason})"
                for sid, reason in sources_skipped
                if sid in adapters_by_id and mode in adapters_by_id[sid].modes
            )
        )
        label = MODE_GROUP_TITLE.get(mode.value, mode.value)
        lines.append(f"{label}: без результатов" + (f" — {', '.join(reasons)}." if reasons else "."))
    return "\n".join(lines)


async def _render_results(screen: Screen, state: FSMContext, *, limit: int) -> None:
    """Draws the results screen from whatever's already staged in FSM state
    (see `_run_search`'s `state.update_data` above) — shared by the initial
    search and by "📋 Показать все" (`on_search_show_all`), which re-renders
    the same screen with a bigger `limit` instead of re-searching."""
    data = await state.get_data()
    today = date.today()
    all_found: list[Itinerary] = data["results_all_itineraries"]
    total: int = data["results_found_total"]
    source_names: dict[str, str] = data.get("results_source_names", {})
    shown = all_found[:limit]

    note = f"Нашёл вариантов: {total}. Показываю {len(shown)}."
    if total > len(all_found):
        note += f" (Показываю максимум {_RESULTS_MAX} за раз.)"
    lines = [note, ""]
    # Headers only when there is actually something to separate — a plane-only search
    # gets the same flat list it always had, not a lone «✈️ Авиабилеты» banner.
    grouped = len({mode_group(it) for it in shown}) > 1
    current_group: str | None = None
    for i, itinerary in enumerate(shown, start=1):
        group = mode_group(itinerary)
        if grouped and group != current_group:
            if current_group is not None:
                lines.append("")
            current_group = group
            lines.append(f"<b>{MODE_GROUP_TITLE.get(group, group)}</b>")
        lines.append(f"{i}️⃣ {_render_itinerary_line(itinerary, source_names)}")

    legend = "✅ цена подтверждена перевозчиком · ⚠️ из кэша агрегатора"
    await state.update_data(results_itineraries=shown)
    await screen.show(
        screen_text(
            data["results_title"], describe_selection(data["results_selection"], today), "\n".join(lines),
            legend, data["results_checked"],
        ),
        reply_markup=search_results_kb(len(shown), more_available=len(all_found) - len(shown)),
    )


@router.callback_query(SearchFlow.results, SearchShowAll.filter())
async def on_search_show_all(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("results_all_itineraries"):
        await query.answer("Поиск устарел, начните заново: /search", show_alert=True)
        return
    await _render_results(await screen_for(query, state), state, limit=_RESULTS_MAX)
    await query.answer()


def _carrier_or_source_label(itinerary: Itinerary, source_names: dict[str, str]) -> str:
    """Who's actually flying/selling this, not just ✅/⚠️. Confirmation-tier
    offers know the real carrier (airline/train operator); discovery-tier
    offers from an aggregator often don't (Travelpayouts' month-matrix has no
    airline field at all — see base.py Leg docstring), so this falls back to
    the source name itself rather than showing nothing."""
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    carriers = list(dict.fromkeys(leg.carrier for leg in all_legs if leg.carrier))
    if carriers:
        return "/".join(carriers)
    sources = list(dict.fromkeys(source_names.get(o.source_id, o.source_id) for o in itinerary_offers(itinerary)))
    return "/".join(sources)


def _render_itinerary_line(itinerary: Itinerary, source_names: dict[str, str]) -> str:
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    mode_label = format_mode_label([leg.mode for leg in all_legs])
    price = format_money(itinerary.price_minor, itinerary.currency)
    # local, not UTC — see telegram/format.py's format_deal_card for why.
    depart = format_date(all_legs[0].depart_local.date())
    parts = [f"<b>{price}</b>", depart, mode_label]
    if len(itinerary.offers) > 1:
        hub_place = all_legs[0].to_place
        parts[-1] += f" через {place_label(hub_place)}"
    if itinerary.duration_minutes is not None:
        h, m = divmod(itinerary.duration_minutes, 60)
        parts.append(f"{h}ч{f' {m:02d}м' if m else ''}")
    parts.append(_carrier_or_source_label(itinerary, source_names))
    confirmed_all = all(o.tier == Tier.CONFIRMATION for o in itinerary_offers(itinerary))
    return " · ".join(parts) + (" ✅" if confirmed_all else " ⚠️")


@router.callback_query(SearchFlow.results, SearchTrack.filter())
async def on_search_track(query: CallbackQuery, callback_data: SearchTrack, state: FSMContext) -> None:
    """§10 bridge: jumps straight into /add's threshold step with everything
    else pre-filled from the search the user just ran — including the date
    selection, so a flexible search becomes a flexible route."""
    data = await state.get_data()
    itineraries = data.get("results_itineraries")
    if not itineraries or not 0 <= callback_data.idx < len(itineraries):
        await query.answer("Поиск устарел, начните заново: /search", show_alert=True)
        return
    itinerary: Itinerary = itineraries[callback_data.idx]
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    modes_used = {leg.mode.value for leg in all_legs}
    mode_pref = modes_used.pop() if len(modes_used) == 1 else "auto"
    selection: DateSelection = data["results_selection"]

    await state.update_data(
        origin_place_id=data["origin_place_id"],
        origin_title=data["origin_title"],
        dest_place_id=data["dest_place_id"],
        dest_title=data["dest_title"],
        mode_pref=mode_pref,
        date_selection=selection,
    )
    await state.set_state(AddRoute.threshold)
    screen = await screen_for(query, state)
    suggested_rub = (itinerary.price_minor // 100 // 500) * 500
    await add_handlers.step(
        screen,
        state,
        "Беру места, транспорт и даты из этого поиска.\n\n"
        f"Ниже какой цены присылать уведомление?\nСумма в рублях, "
        f"например {suggested_rub or itinerary.price_minor // 100}.",
        reply_markup=menu_only_kb(),
    )
    await query.answer()
