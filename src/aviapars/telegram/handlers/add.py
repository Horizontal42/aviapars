from __future__ import annotations

from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.config import settings
from aviapars.db.repo_routes import RoutesRepo
from aviapars.domain.dates import DateSelection, to_route_fields
from aviapars.telegram import date_picker
from aviapars.telegram.format import describe_notification_mode, describe_selection, format_money
from aviapars.telegram.keyboards import (
    MODE_LABEL,
    DatePick,
    ModePick,
    NotifyModePick,
    PlacePick,
    date_mode_kb,
    menu_only_kb,
    mode_picker_kb,
    notify_mode_kb,
    route_saved_kb,
)
from aviapars.telegram.place_picker import ask_place_pick
from aviapars.telegram.states import AddRoute

router = Router(name="add")

# Sanity ceilings, not real limits — no real ticket costs this much, no route needs polling
# this rarely. Without them an absurd digit string (e.g. 19 digits) overflows timedelta()
# or SQLite's bind for max_price and takes down the whole scheduler tick, not just this route.
_MAX_PRICE_RUB = 100_000_000
_MAX_INTERVAL_MIN = 43_200  # 30 days


async def start_wizard(message: Message, state: FSMContext) -> None:
    """Entry point shared by /add and the main menu's «➕ Добавить маршрут»."""
    await state.clear()
    await state.set_state(AddRoute.origin)
    await message.answer("Откуда летим/едем? Напишите город.", reply_markup=menu_only_kb())


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await start_wizard(message, state)


@router.message(AddRoute.origin)
async def on_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await ask_place_pick(message, state, db, next_state=AddRoute.origin_pick, storage_key="candidates_origin")


@router.callback_query(AddRoute.origin_pick, PlacePick.filter())
async def on_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    candidates = data.get("candidates_origin")
    if not candidates or not 0 <= callback_data.idx < len(candidates):
        await query.answer("Список устарел, начните заново: /add", show_alert=True)
        return
    place = candidates[callback_data.idx]
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(AddRoute.destination)
    assert query.message is not None
    await query.message.answer(f"Откуда: {place.name_ru}. Теперь куда?", reply_markup=menu_only_kb())
    await query.answer()


@router.message(AddRoute.destination)
async def on_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    await ask_place_pick(message, state, db, next_state=AddRoute.destination_pick, storage_key="candidates_dest")


@router.callback_query(AddRoute.destination_pick, PlacePick.filter())
async def on_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    candidates = data.get("candidates_dest")
    if not candidates or not 0 <= callback_data.idx < len(candidates):
        await query.answer("Список устарел, начните заново: /add", show_alert=True)
        return
    place = candidates[callback_data.idx]
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(AddRoute.mode)
    assert query.message is not None
    await query.message.answer(f"Куда: {place.name_ru}. Какой транспорт?", reply_markup=mode_picker_kb())
    await query.answer()


@router.callback_query(AddRoute.mode, ModePick.filter())
async def on_mode_pick(query: CallbackQuery, callback_data: ModePick, state: FSMContext) -> None:
    await state.update_data(mode_pref=callback_data.value)
    await state.set_state(AddRoute.date_mode)
    assert query.message is not None
    await query.message.answer(
        f"{MODE_LABEL[callback_data.value]}. Насколько важна конкретная дата?",
        reply_markup=date_mode_kb(),
    )
    await query.answer()


@router.callback_query(AddRoute.date_mode, DatePick.filter())
async def on_date_mode_pick(query: CallbackQuery, callback_data: DatePick, state: FSMContext) -> None:
    if not isinstance(query.message, Message):
        await query.answer()
        return
    ready = date_picker.selection_for(callback_data.kind)
    if ready is not None:
        await _ask_threshold(query.message, state, ready)
        await query.answer()
        return
    prompt = date_picker.prompt_for(callback_data.kind)
    assert prompt is not None
    await state.update_data(date_kind=callback_data.kind)
    await state.set_state(AddRoute.date_value)
    await query.message.answer(prompt, reply_markup=menu_only_kb())
    await query.answer()


@router.message(AddRoute.date_value)
async def on_date_value_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    data = await state.get_data()
    parsed = date_picker.parse_selection(data["date_kind"], message.text, date.today())
    if isinstance(parsed, str):
        await message.answer(parsed, reply_markup=menu_only_kb())
        return
    await _ask_threshold(message, state, parsed)


async def _ask_threshold(message: Message, state: FSMContext, selection: DateSelection) -> None:
    await state.update_data(date_selection=selection)
    await state.set_state(AddRoute.threshold)
    await message.answer(
        f"Даты: {describe_selection(selection, date.today())}.\n\n"
        "При какой цене прислать уведомление? Введите сумму в рублях, например 8000.\n"
        "Уведомление придёт только когда найдётся цена не выше этой.",
        reply_markup=menu_only_kb(),
    )


@router.message(AddRoute.threshold)
async def on_threshold_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    try:
        rub = int(message.text.strip().replace(" ", ""))
    except ValueError:
        await message.answer("Введите число, например 8000.", reply_markup=menu_only_kb())
        return
    if rub <= 0 or rub > _MAX_PRICE_RUB:
        await message.answer(f"Введите цену от 1 до {_MAX_PRICE_RUB:,} ₽.".replace(",", " "), reply_markup=menu_only_kb())
        return
    await state.update_data(max_price_minor=rub * 100)
    await state.set_state(AddRoute.interval)
    await message.answer(
        f"Как часто проверять цену? Введите число минут (минимум {settings.poll_interval_floor_min}), "
        f"например 60.",
        reply_markup=menu_only_kb(),
    )


@router.message(AddRoute.interval)
async def on_interval_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число минут, например 60.", reply_markup=menu_only_kb())
        return
    if minutes < settings.poll_interval_floor_min or minutes > _MAX_INTERVAL_MIN:
        await message.answer(
            f"Введите от {settings.poll_interval_floor_min} до {_MAX_INTERVAL_MIN} минут.",
            reply_markup=menu_only_kb(),
        )
        return

    await state.update_data(poll_interval_min=minutes)
    await state.set_state(AddRoute.notify_mode)
    await message.answer(
        "Как присылать уведомления?\n\n"
        "⚡ <b>Сразу</b> — как только цена упадёт ниже порога.\n"
        "📬 <b>Раз в день</b> — одно сообщение с лучшей ценой за сутки.",
        reply_markup=notify_mode_kb(),
    )


@router.callback_query(AddRoute.notify_mode, NotifyModePick.filter())
async def on_notify_mode_pick(
    query: CallbackQuery, callback_data: NotifyModePick, state: FSMContext, db: aiosqlite.Connection
) -> None:
    if not isinstance(query.message, Message):
        await query.answer()
        return
    await _create_route(query.message, state, db, notification_mode=callback_data.value, user_id=query.from_user.id)
    await query.answer()


async def _create_route(
    message: Message, state: FSMContext, db: aiosqlite.Connection, *, notification_mode: str, user_id: int
) -> None:
    data = await state.get_data()
    minutes: int = data["poll_interval_min"]
    selection: DateSelection = data["date_selection"]
    dates = to_route_fields(selection)
    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        # `user_id` is passed in, not read off `message.from_user`: the last wizard step is a
        # button press, so the message this replies to was sent by the bot itself.
        user_id=user_id,
        title=f"{data['origin_title']} → {data['dest_title']}",
        origin_place_id=data["origin_place_id"],
        dest_place_id=data["dest_place_id"],
        mode_pref=data["mode_pref"],
        date_mode=dates.date_mode,
        fixed_dates=dates.fixed_dates,
        window_start_date=dates.window_start_date,
        window_start_offset_days=dates.window_start_offset_days,
        window_length_days=dates.window_length_days,
        max_price=data["max_price_minor"],
        max_price_currency="RUB",
        poll_interval_min=minutes,
        notification_mode=notification_mode,
    )
    await state.clear()
    await message.answer(
        f"Готово! Слежу за маршрутом «{route.title}».\n"
        f"Даты: {describe_selection(selection, date.today())}.\n"
        f"Порог: {format_money(route.max_price, route.max_price_currency)}. "
        f"Проверяю каждые {minutes} мин.\n"
        f"Уведомления: {describe_notification_mode(route.notification_mode)}.",
        reply_markup=route_saved_kb(),
    )
