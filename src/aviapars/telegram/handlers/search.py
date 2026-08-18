from __future__ import annotations

from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.base import Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.models import Route
from aviapars.domain.combiner import Itinerary, SearchOpts, search_itineraries
from aviapars.domain.dates import DateSelection, resolve_date_plan, selection_from_route
from aviapars.domain.places import modes_for_pref
from aviapars.telegram import date_picker
from aviapars.telegram.format import (
    MODE_GROUP_TITLE,
    describe_checked_dates,
    describe_selection,
    format_date,
    format_mode_label,
    format_money,
    mode_group,
    select_top_itineraries,
)
from aviapars.telegram.keyboards import (
    MODE_LABEL,
    DatePick,
    ModePick,
    PlacePick,
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

_RESULTS_SHOWN = 5


async def start_wizard(screen: Screen, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchFlow.origin)
    await screen.show("Разовый поиск (маршрут не сохраняется). Откуда? Напишите город.", reply_markup=menu_only_kb())


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


@router.message(SearchFlow.origin)
async def on_search_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    await ask_place_pick(
        await screen_for(message, state),
        state,
        db,
        text=message.text,
        pick_state=SearchFlow.origin_pick,
        storage_key="candidates_origin",
    )


@router.callback_query(SearchFlow.origin_pick, PlacePick.filter())
async def on_search_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_origin"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /search", show_alert=True)
        return
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(SearchFlow.destination)
    screen = await screen_for(query, state)
    await screen.show(f"Откуда: {place.name_ru}. Теперь куда?", reply_markup=menu_only_kb())
    await query.answer()


@router.message(SearchFlow.destination)
async def on_search_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    await ask_place_pick(
        await screen_for(message, state),
        state,
        db,
        text=message.text,
        pick_state=SearchFlow.destination_pick,
        storage_key="candidates_dest",
    )


@router.callback_query(SearchFlow.destination_pick, PlacePick.filter())
async def on_search_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_dest"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /search", show_alert=True)
        return
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(SearchFlow.mode)
    screen = await screen_for(query, state)
    await screen.show(f"Куда: {place.name_ru}. Какой транспорт?", reply_markup=mode_picker_kb())
    await query.answer()


@router.callback_query(SearchFlow.mode, ModePick.filter())
async def on_search_mode_pick(query: CallbackQuery, callback_data: ModePick, state: FSMContext) -> None:
    await state.update_data(mode_pref=callback_data.value)
    await state.set_state(SearchFlow.date_mode)
    screen = await screen_for(query, state)
    await screen.show(f"{MODE_LABEL[callback_data.value]}. Когда?", reply_markup=date_mode_kb())
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
    await screen.show(prompt, reply_markup=menu_only_kb())
    await query.answer()


@router.message(SearchFlow.date_value)
async def on_search_date_value_text(
    message: Message, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    data = await state.get_data()
    parsed = date_picker.parse_selection(data["date_kind"], message.text, date.today())
    if isinstance(parsed, str):
        await screen.show(parsed, reply_markup=menu_only_kb())
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

    A flexible window is deliberately NOT scanned in full here: one-request-
    per-date sources can't cover 30 days inside ~12 seconds, so only
    `flex_search_probe_dates` of them are probed and the reply says exactly
    which — the full window is what background tracking is for. Batch sources
    still return the whole window, so wide-window results do show up.
    """
    today = date.today()
    plan, _ = resolve_date_plan(selection, today=today, probe_budget=settings.flex_search_probe_dates)
    if plan.is_empty:
        await screen.show("В этом окне не осталось будущих дат.", reply_markup=search_again_kb())
        await state.clear()
        return

    data = await state.get_data()
    places_repo = PlacesRepo(db)
    origin = await places_repo.get(data["origin_place_id"])
    dest = await places_repo.get(data["dest_place_id"])
    assert origin is not None and dest is not None

    await screen.show("🔎 Ищу варианты, включая через хаб…")
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
        note = f"\nПропущены источники: {skipped}" if skipped else ""
        await screen.show(f"Ничего не нашёл.\n{checked}{note}", reply_markup=search_again_kb())
        await state.clear()
        return

    top = select_top_itineraries(outcome.itineraries, limit=_RESULTS_SHOWN)
    lines = [f"🔎 {data['origin_title']} → {data['dest_title']} · {describe_selection(selection, today)}"]
    lines.append(f"Найдено {len(outcome.itineraries)} вариант(ов), показываю {len(top)}")
    # Headers only when there is actually something to separate — a plane-only search
    # gets the same flat list it always had, not a lone «✈️ Авиабилеты» banner.
    grouped = len({mode_group(it) for it in top}) > 1
    current_group: str | None = None
    for i, itinerary in enumerate(top, start=1):
        group = mode_group(itinerary)
        if grouped and group != current_group:
            current_group = group
            lines.append(f"\n{MODE_GROUP_TITLE.get(group, group)}")
        lines.append(f"{i}️⃣ {_render_itinerary_line(itinerary)}")
    lines.append(f"\n{checked}")

    await state.update_data(
        results_itineraries=top,
        results_route_title=f"{data['origin_title']} → {data['dest_title']}",
        results_selection=selection,
    )
    await state.set_state(SearchFlow.results)
    await screen.show("\n".join(lines), reply_markup=search_results_kb(len(top)))


def _render_itinerary_line(itinerary: Itinerary) -> str:
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    modes = [leg.mode for leg in all_legs]
    mode_label = format_mode_label(modes)
    price = format_money(itinerary.price_minor, itinerary.currency)
    # local, not UTC — see telegram/format.py's format_deal_card for why.
    depart = format_date(all_legs[0].depart_local.date())
    dur = ""
    if itinerary.duration_minutes is not None:
        h, m = divmod(itinerary.duration_minutes, 60)
        dur = f" · {h}ч{f' {m}м' if m else ''}"
    hub_note = ""
    if len(itinerary.offers) > 1:
        hub_place = all_legs[0].to_place
        hub_note = f" через {hub_place.iata or hub_place.place_id}"
    confirmed_all = all(o.tier == Tier.CONFIRMATION for o in itinerary.offers)
    confirmed = " ✅" if confirmed_all else " ⚠️ не подтверждено"
    return f"{price} · {depart} {mode_label}{hub_note}{dur}{confirmed}"


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
    await screen.show(
        f"Маршрут «{data['results_route_title']}» — беру эти параметры.\n"
        f"Даты: {describe_selection(selection, date.today())}.\n"
        f"При какой цене прислать уведомление? Введите сумму в рублях "
        f"(например {suggested_rub or itinerary.price_minor // 100}).",
        reply_markup=menu_only_kb(),
    )
    await query.answer()
