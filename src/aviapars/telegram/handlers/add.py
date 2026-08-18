from __future__ import annotations

from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.config import settings
from aviapars.db.models import Place
from aviapars.db.repo_routes import RoutesRepo
from aviapars.domain.dates import DateSelection, to_route_fields
from aviapars.telegram import date_picker
from aviapars.telegram.format import describe_notification_mode, describe_selection, describe_thresholds
from aviapars.telegram.keyboards import (
    MODE_LABEL,
    DatePick,
    ModePick,
    NotifyModePick,
    PlacePick,
    ThresholdScopePick,
    date_mode_kb,
    menu_only_kb,
    mode_picker_kb,
    notify_mode_kb,
    route_saved_kb,
    threshold_scope_kb,
)
from aviapars.telegram.place_picker import ask_place_pick, resolve_pick
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import AddRoute

router = Router(name="add")

# Sanity ceilings, not real limits — no real ticket costs this much, no route needs polling
# this rarely. Without them an absurd digit string (e.g. 19 digits) overflows timedelta()
# or SQLite's bind for max_price and takes down the whole scheduler tick, not just this route.
_MAX_PRICE_RUB = 100_000_000
_MAX_INTERVAL_MIN = 43_200  # 30 days


async def start_wizard(screen: Screen, state: FSMContext) -> None:
    """Entry point shared by /add and the main menu's «➕ Добавить маршрут»."""
    await state.clear()
    await state.set_state(AddRoute.origin)
    await screen.show("Откуда летим/едем? Напишите город.", reply_markup=menu_only_kb())


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await start_wizard(await screen_for(message, state), state)


async def _origin_picked(screen: Screen, state: FSMContext, place: Place) -> None:
    await state.update_data(origin_place_id=place.id, origin_title=place.name_ru)
    await state.set_state(AddRoute.destination)
    await screen.show(f"Откуда: {place.name_ru}. Теперь куда?", reply_markup=menu_only_kb())


async def _destination_picked(screen: Screen, state: FSMContext, place: Place) -> None:
    await state.update_data(dest_place_id=place.id, dest_title=place.name_ru)
    await state.set_state(AddRoute.mode)
    await screen.show(f"Куда: {place.name_ru}. Какой транспорт?", reply_markup=mode_picker_kb())


@router.message(AddRoute.origin)
async def on_origin_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    await ask_place_pick(
        screen,
        state,
        db,
        text=message.text,
        pick_state=AddRoute.origin_pick,
        storage_key="candidates_origin",
        on_picked=lambda place: _origin_picked(screen, state, place),
    )


@router.callback_query(AddRoute.origin_pick, PlacePick.filter())
async def on_origin_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_origin"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /add", show_alert=True)
        return
    await _origin_picked(await screen_for(query, state), state, place)
    await query.answer()


@router.message(AddRoute.destination)
async def on_destination_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    await ask_place_pick(
        screen,
        state,
        db,
        text=message.text,
        pick_state=AddRoute.destination_pick,
        storage_key="candidates_dest",
        on_picked=lambda place: _destination_picked(screen, state, place),
    )


@router.callback_query(AddRoute.destination_pick, PlacePick.filter())
async def on_destination_pick(query: CallbackQuery, callback_data: PlacePick, state: FSMContext) -> None:
    data = await state.get_data()
    place = resolve_pick(data.get("candidates_dest"), callback_data.idx)
    if place is None:
        await query.answer("Список устарел, начните заново: /add", show_alert=True)
        return
    await _destination_picked(await screen_for(query, state), state, place)
    await query.answer()


@router.callback_query(AddRoute.mode, ModePick.filter())
async def on_mode_pick(query: CallbackQuery, callback_data: ModePick, state: FSMContext) -> None:
    await state.update_data(mode_pref=callback_data.value)
    await state.set_state(AddRoute.date_mode)
    screen = await screen_for(query, state)
    await screen.show(
        f"{MODE_LABEL[callback_data.value]}. Насколько важна конкретная дата?",
        reply_markup=date_mode_kb(),
    )
    await query.answer()


@router.callback_query(AddRoute.date_mode, DatePick.filter())
async def on_date_mode_pick(query: CallbackQuery, callback_data: DatePick, state: FSMContext) -> None:
    screen = await screen_for(query, state)
    ready = date_picker.selection_for(callback_data.kind)
    if ready is not None:
        await _ask_threshold(screen, state, ready)
        await query.answer()
        return
    prompt = date_picker.prompt_for(callback_data.kind)
    assert prompt is not None
    await state.update_data(date_kind=callback_data.kind)
    await state.set_state(AddRoute.date_value)
    await screen.show(prompt, reply_markup=menu_only_kb())
    await query.answer()


@router.message(AddRoute.date_value)
async def on_date_value_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    data = await state.get_data()
    parsed = date_picker.parse_selection(data["date_kind"], message.text, date.today())
    if isinstance(parsed, str):
        await screen.show(parsed, reply_markup=menu_only_kb())
        return
    await _ask_threshold(screen, state, parsed)


_SHARED_THRESHOLD_PROMPT = (
    "При какой цене прислать уведомление? Введите сумму в рублях, например 8000.\n"
    "Уведомление придёт только когда найдётся цена не выше этой."
)


async def _ask_threshold(screen: Screen, state: FSMContext, selection: DateSelection) -> None:
    await state.update_data(date_selection=selection)
    data = await state.get_data()
    dates_line = f"Даты: {describe_selection(selection, date.today())}.\n\n"
    # A single-mode route has nothing to split, so it goes straight to the one prompt it
    # always had — the extra question only exists for a route watching both.
    if data["mode_pref"] != "auto":
        await state.set_state(AddRoute.threshold)
        await screen.show(dates_line + _SHARED_THRESHOLD_PROMPT, reply_markup=menu_only_kb())
        return
    await state.set_state(AddRoute.threshold_scope)
    await screen.show(
        dates_line + "Маршрут следит и за самолётами, и за поездами.\n"
        "Один порог цены на всё или отдельный на каждый вид транспорта?",
        reply_markup=threshold_scope_kb(),
    )


@router.callback_query(AddRoute.threshold_scope, ThresholdScopePick.filter())
async def on_threshold_scope_pick(query: CallbackQuery, callback_data: ThresholdScopePick, state: FSMContext) -> None:
    screen = await screen_for(query, state)
    if callback_data.value == "shared":
        await state.set_state(AddRoute.threshold)
        await screen.show(_SHARED_THRESHOLD_PROMPT, reply_markup=menu_only_kb())
    else:
        await state.set_state(AddRoute.threshold_plane)
        await screen.show(_per_mode_prompt("plane"), reply_markup=menu_only_kb())
    await query.answer()


def _per_mode_prompt(mode: str) -> str:
    return f"Порог для «{MODE_LABEL[mode]}» — сумма в рублях, например 8000."


def _parse_price_minor(text: str) -> int | str:
    """Minor units, or the error message to show the user."""
    try:
        rub = int(text.strip().replace(" ", ""))
    except ValueError:
        return "Введите число, например 8000."
    if rub <= 0 or rub > _MAX_PRICE_RUB:
        return f"Введите цену от 1 до {_MAX_PRICE_RUB:,} ₽.".replace(",", " ")
    return rub * 100


@router.message(AddRoute.threshold)
async def on_threshold_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    parsed = _parse_price_minor(message.text)
    if isinstance(parsed, str):
        await screen.show(parsed, reply_markup=menu_only_kb())
        return
    await state.update_data(max_price_minor=parsed)
    await _ask_interval(screen, state)


@router.message(AddRoute.threshold_plane)
async def on_threshold_plane_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    parsed = _parse_price_minor(message.text)
    if isinstance(parsed, str):
        await screen.show(parsed, reply_markup=menu_only_kb())
        return
    await state.update_data(max_price_plane_minor=parsed)
    await state.set_state(AddRoute.threshold_train)
    await screen.show(_per_mode_prompt("train"), reply_markup=menu_only_kb())


@router.message(AddRoute.threshold_train)
async def on_threshold_train_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    parsed = _parse_price_minor(message.text)
    if isinstance(parsed, str):
        await screen.show(parsed, reply_markup=menu_only_kb())
        return
    data = await state.get_data()
    by_mode = {"plane": data["max_price_plane_minor"], "train": parsed}
    await state.update_data(
        max_price_by_mode=by_mode,
        # max_price stays NOT NULL and keeps a real meaning: it is what a mixed plane+train
        # hub itinerary gets compared against, since neither per-mode ceiling describes a
        # combined price. The larger of the two is the most the user said they'd pay for
        # this trip by any single means — see domain/dealer.threshold_for.
        max_price_minor=max(by_mode.values()),
    )
    await _ask_interval(screen, state)


async def _ask_interval(screen: Screen, state: FSMContext) -> None:
    await state.set_state(AddRoute.interval)
    await screen.show(
        f"Как часто проверять цену? Введите число минут (минимум {settings.poll_interval_floor_min}), "
        f"например 60.",
        reply_markup=menu_only_kb(),
    )


@router.message(AddRoute.interval)
async def on_interval_text(message: Message, state: FSMContext) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await screen.show("Введите целое число минут, например 60.", reply_markup=menu_only_kb())
        return
    if minutes < settings.poll_interval_floor_min or minutes > _MAX_INTERVAL_MIN:
        await screen.show(
            f"Введите от {settings.poll_interval_floor_min} до {_MAX_INTERVAL_MIN} минут.",
            reply_markup=menu_only_kb(),
        )
        return

    await state.update_data(poll_interval_min=minutes)
    await state.set_state(AddRoute.notify_mode)
    await screen.show(
        "Как присылать уведомления?\n\n"
        "⚡ <b>Сразу</b> — как только цена упадёт ниже порога.\n"
        "📬 <b>Раз в день</b> — одно сообщение с лучшей ценой за сутки.",
        reply_markup=notify_mode_kb(),
    )


@router.callback_query(AddRoute.notify_mode, NotifyModePick.filter())
async def on_notify_mode_pick(
    query: CallbackQuery, callback_data: NotifyModePick, state: FSMContext, db: aiosqlite.Connection
) -> None:
    screen = await screen_for(query, state)
    await _create_route(screen, state, db, notification_mode=callback_data.value, user_id=query.from_user.id)
    await query.answer()


async def _create_route(
    screen: Screen, state: FSMContext, db: aiosqlite.Connection, *, notification_mode: str, user_id: int
) -> None:
    data = await state.get_data()
    minutes: int = data["poll_interval_min"]
    selection: DateSelection = data["date_selection"]
    dates = to_route_fields(selection)
    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        # `user_id` is passed in, not read off a message: the last wizard step is a button
        # press, so the only message in play is the bot's own screen.
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
        max_price_by_mode=data.get("max_price_by_mode"),
        poll_interval_min=minutes,
        notification_mode=notification_mode,
    )
    await state.clear()
    await screen.show(
        f"Готово! Слежу за маршрутом «{route.title}».\n"
        f"Даты: {describe_selection(selection, date.today())}.\n"
        f"Порог: {describe_thresholds(route.max_price, route.max_price_currency, route.max_price_by_mode)}. "
        f"Проверяю каждые {minutes} мин.\n"
        f"Уведомления: {describe_notification_mode(route.notification_mode)}.",
        reply_markup=route_saved_kb(),
    )
