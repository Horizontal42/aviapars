from __future__ import annotations

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.worker import process_route
from aviapars.telegram.format import format_money
from aviapars.telegram.keyboards import RouteAction, route_card_kb

router = Router(name="routes")


@router.message(Command("routes"))
async def cmd_routes(message: Message, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    routes = await RoutesRepo(db).list_for_user(message.from_user.id)
    if not routes:
        await message.answer("Пока нет отслеживаемых маршрутов. /add — добавить.")
        return
    for route in routes:
        status = "▶️ активен" if route.active else "⏸ на паузе"
        text = (
            f"{route.title}\n"
            f"Порог: {format_money(route.max_price, route.max_price_currency)} · "
            f"каждые {route.poll_interval_min} мин · {status}"
        )
        await message.answer(text, reply_markup=route_card_kb(route.id, active=route.active))


@router.callback_query(RouteAction.filter())
async def on_route_action(
    query: CallbackQuery,
    callback_data: RouteAction,
    db: aiosqlite.Connection,
    registry: AdapterRegistry,
    notifier: Notifier,
) -> None:
    routes_repo = RoutesRepo(db)
    route = await routes_repo.get(callback_data.route_id)
    assert query.from_user is not None
    if route is None or route.user_id != query.from_user.id:
        await query.answer("Маршрут не найден.", show_alert=True)
        return

    if callback_data.action == "pause":
        await routes_repo.set_active(route.id, False)
        await query.answer("Поставил на паузу.")
        if isinstance(query.message, Message):
            await query.message.edit_reply_markup(reply_markup=route_card_kb(route.id, active=False))
    elif callback_data.action == "resume":
        await routes_repo.set_active(route.id, True)
        await query.answer("Возобновил.")
        if isinstance(query.message, Message):
            await query.message.edit_reply_markup(reply_markup=route_card_kb(route.id, active=True))
    elif callback_data.action == "delete":
        await routes_repo.delete(route.id)
        await query.answer("Удалил.")
        if isinstance(query.message, Message):
            await query.message.delete()
    elif callback_data.action == "check":
        await query.answer("Проверяю...")
        user = await UsersRepo(db).get(route.user_id)
        assert user is not None
        await process_route(
            route,
            user=user,
            registry=registry,
            places_repo=PlacesRepo(db),
            observations_repo=ObservationsRepo(db),
            notifier=notifier,
        )
        assert query.message is not None
        await query.message.answer("Проверил. Если что-то нашлось ниже порога — уведомление придёт отдельно.")
