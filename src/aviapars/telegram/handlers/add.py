from __future__ import annotations

from datetime import date, datetime

import aiosqlite
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from aviapars.config import settings
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.telegram.format import format_money
from aviapars.telegram.keyboards import ModePick, PlacePick, mode_picker_kb, place_picker_kb
from aviapars.telegram.states import AddRoute

router = Router(name="add")

_MODE_LABEL = {"plane": "✈️ Самолёт", "train": "🚂 Поезд", "auto": "🔀 Авто"}


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddRoute.origin)
    await message.answer("Откуда летим/едем? Напишите город.")


@router.message(AddRoute.origin)
async def on_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await _ask_place_pick(message, state, db, next_state=AddRoute.origin_pick, storage_key="candidates_origin")


@router.callback_query(AddRoute.origin_pick, PlacePick.filter())
async def on_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = data["candidates_origin"][callback_data.idx]
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(AddRoute.destination)
    assert query.message is not None
    await query.message.answer(f"Откуда: {place.name_ru}. Теперь куда?")
    await query.answer()


@router.message(AddRoute.destination)
async def on_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await _ask_place_pick(message, state, db, next_state=AddRoute.destination_pick, storage_key="candidates_dest")


@router.callback_query(AddRoute.destination_pick, PlacePick.filter())
async def on_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = data["candidates_dest"][callback_data.idx]
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(AddRoute.mode)
    assert query.message is not None
    await query.message.answer(f"Куда: {place.name_ru}. Какой транспорт?", reply_markup=mode_picker_kb())
    await query.answer()


@router.callback_query(AddRoute.mode, ModePick.filter())
async def on_mode_pick(query: CallbackQuery, callback_data: ModePick, state: FSMContext) -> None:
    await state.update_data(mode_pref=callback_data.value)
    await state.set_state(AddRoute.date)
    assert query.message is not None
    await query.message.answer(f"{_MODE_LABEL[callback_data.value]}. Дата вылета? Формат: ДД.ММ.ГГГГ")
    await query.answer()


@router.message(AddRoute.date)
async def on_date_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    try:
        parsed = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Не понял дату. Формат: ДД.ММ.ГГГГ, например 18.09.2026")
        return
    if parsed < date.today():
        await message.answer("Эта дата уже в прошлом. Введите дату в будущем.")
        return
    await state.update_data(fixed_date=parsed.isoformat())
    await state.set_state(AddRoute.threshold)
    await message.answer(
        "При какой цене прислать уведомление? Введите сумму в рублях, например 8000.\n"
        "Уведомление придёт только когда найдётся цена не выше этой."
    )


@router.message(AddRoute.threshold)
async def on_threshold_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    try:
        rub = int(message.text.strip().replace(" ", ""))
    except ValueError:
        await message.answer("Введите число, например 8000.")
        return
    if rub <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return
    await state.update_data(max_price_minor=rub * 100)
    await state.set_state(AddRoute.interval)
    await message.answer(
        f"Как часто проверять цену? Введите число минут (минимум {settings.poll_interval_floor_min}), "
        f"например 60."
    )


@router.message(AddRoute.interval)
async def on_interval_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число минут, например 60.")
        return
    if minutes < settings.poll_interval_floor_min:
        await message.answer(f"Минимум {settings.poll_interval_floor_min} минут — иначе долбим источники слишком часто.")
        return

    data = await state.get_data()
    assert message.from_user is not None
    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=message.from_user.id,
        title=f"{data['origin_title']} → {data['dest_title']}",
        origin_place_id=data["origin_place_id"],
        dest_place_id=data["dest_place_id"],
        mode_pref=data["mode_pref"],
        date_mode="fixed",
        fixed_dates=[data["fixed_date"]],
        window_start_offset_days=None,
        window_length_days=None,
        max_price=data["max_price_minor"],
        max_price_currency="RUB",
        poll_interval_min=minutes,
    )
    await state.clear()
    await message.answer(
        f"Готово! Слежу за маршрутом «{route.title}» на {data['fixed_date']}.\n"
        f"Порог: {format_money(route.max_price, route.max_price_currency)}. "
        f"Проверяю каждые {minutes} мин.\n\n"
        f"Список маршрутов: /routes"
    )


async def _ask_place_pick(
    message: Message, state: FSMContext, db: aiosqlite.Connection, *, next_state: State, storage_key: str
) -> None:
    assert message.text is not None
    places = await PlacesRepo(db).search_by_name(message.text)
    if not places:
        await message.answer("Ничего не нашёл. Попробуйте другое название города.")
        return
    await state.update_data({storage_key: places})
    await state.set_state(next_state)
    await message.answer("Выберите вариант:", reply_markup=place_picker_kb(places))
