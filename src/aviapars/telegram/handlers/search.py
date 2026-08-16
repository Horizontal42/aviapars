from __future__ import annotations

from datetime import date, datetime

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.base import Mode, Tier
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.repo_places import PlacesRepo
from aviapars.domain.combiner import Itinerary, SearchOpts, search_itineraries
from aviapars.telegram.format import format_mode_label, format_money
from aviapars.telegram.keyboards import PlacePick, SearchTrack, search_results_kb
from aviapars.telegram.place_picker import ask_place_pick
from aviapars.telegram.states import AddRoute, SearchFlow

router = Router(name="search")

# Every mode with a registered adapter is tried; a mode with nothing
# registered for it (or no coverage for these places) just contributes zero
# offers — search_itineraries degrades to whatever sources actually answered.
_SEARCH_MODES = frozenset({Mode.PLANE, Mode.TRAIN})


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchFlow.origin)
    await message.answer("Разовый поиск (маршрут не сохраняется). Откуда? Напишите город.")


@router.message(SearchFlow.origin)
async def on_search_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await ask_place_pick(message, state, db, next_state=SearchFlow.origin_pick, storage_key="candidates_origin")


@router.callback_query(SearchFlow.origin_pick, PlacePick.filter())
async def on_search_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = data["candidates_origin"][callback_data.idx]
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(SearchFlow.destination)
    assert query.message is not None
    await query.message.answer(f"Откуда: {place.name_ru}. Теперь куда?")
    await query.answer()


@router.message(SearchFlow.destination)
async def on_search_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await ask_place_pick(message, state, db, next_state=SearchFlow.destination_pick, storage_key="candidates_dest")


@router.callback_query(SearchFlow.destination_pick, PlacePick.filter())
async def on_search_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = data["candidates_dest"][callback_data.idx]
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(SearchFlow.date)
    assert query.message is not None
    await query.message.answer(f"Куда: {place.name_ru}. Дата вылета? Формат: ДД.ММ.ГГГГ")
    await query.answer()


@router.message(SearchFlow.date)
async def on_search_date_text(
    message: Message, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    assert message.text is not None
    try:
        parsed = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Не понял дату. Формат: ДД.ММ.ГГГГ, например 18.09.2026")
        return
    if parsed < date.today():
        await message.answer("Эта дата уже в прошлом. Введите дату в будущем.")
        return

    data = await state.get_data()
    places_repo = PlacesRepo(db)
    origin = await places_repo.get(data["origin_place_id"])
    dest = await places_repo.get(data["dest_place_id"])
    assert origin is not None and dest is not None

    await message.answer("🔎 Ищу варианты, включая через хаб (например, через Москву)…")
    outcome = await search_itineraries(
        registry,
        places_repo,
        origin=origin,
        dest=dest,
        dates=(parsed,),
        pax=1,
        modes=_SEARCH_MODES,
        opts=SearchOpts(
            tiers=frozenset({Tier.DISCOVERY, Tier.CONFIRMATION}), allow_network=True, deadline_s=12, max_hops=1
        ),
    )

    if not outcome.itineraries:
        skipped = ", ".join(f"{sid} ({reason})" for sid, reason in outcome.sources_skipped)
        note = f"\nПропущены источники: {skipped}" if skipped else ""
        await message.answer(f"Ничего не нашёл на эту дату.{note}")
        await state.clear()
        return

    top = outcome.itineraries[:5]
    lines = [f"🔎 {data['origin_title']} → {data['dest_title']} · {parsed.strftime('%d.%m.%Y')}"]
    lines.append(f"Найдено {len(outcome.itineraries)} вариант(ов), показываю {len(top)}")
    for i, itinerary in enumerate(top, start=1):
        lines.append(f"{i}️⃣ {_render_itinerary_line(itinerary)}")

    await state.update_data(
        results_itineraries=top,
        results_route_title=f"{data['origin_title']} → {data['dest_title']}",
        results_date=parsed.isoformat(),
    )
    await state.set_state(SearchFlow.results)
    await message.answer("\n".join(lines), reply_markup=search_results_kb(len(top)))


def _render_itinerary_line(itinerary: Itinerary) -> str:
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    modes = [leg.mode for leg in all_legs]
    mode_label = format_mode_label(modes)
    price = format_money(itinerary.price_minor, itinerary.currency)
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
    return f"{price} {mode_label}{hub_note}{dur}{confirmed}"


@router.callback_query(SearchFlow.results, SearchTrack.filter())
async def on_search_track(query: CallbackQuery, callback_data: SearchTrack, state: FSMContext) -> None:
    """§10 bridge: jumps straight into /add's threshold step with everything
    else pre-filled from the search the user just ran."""
    data = await state.get_data()
    itinerary: Itinerary = data["results_itineraries"][callback_data.idx]
    all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
    modes_used = {leg.mode.value for leg in all_legs}
    mode_pref = modes_used.pop() if len(modes_used) == 1 else "auto"

    await state.update_data(
        origin_place_id=data["origin_place_id"],
        origin_title=data["origin_title"],
        dest_place_id=data["dest_place_id"],
        dest_title=data["dest_title"],
        mode_pref=mode_pref,
        fixed_date=data["results_date"],
    )
    await state.set_state(AddRoute.threshold)
    assert query.message is not None
    suggested_rub = (itinerary.price_minor // 100 // 500) * 500
    await query.message.answer(
        f"Маршрут «{data['results_route_title']}» на {data['results_date']} — беру эти параметры.\n"
        f"При какой цене прислать уведомление? Введите сумму в рублях "
        f"(например {suggested_rub or itinerary.price_minor // 100})."
    )
    await query.answer()
