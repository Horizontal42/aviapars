from __future__ import annotations

import time
from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.models import Route
from aviapars.db.repo_digests import DigestsRepo
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.worker import process_route
from aviapars.domain.dates import selection_from_route
from aviapars.telegram.format import describe_notification_mode, describe_selection, format_money
from aviapars.telegram.handlers import search as search_handlers
from aviapars.telegram.keyboards import RouteAction, route_card_kb, routes_list_kb

router = Router(name="routes")

# "🔍 Проверить сейчас" bypasses the scheduler entirely and calls process_route()
# in-process (unlike a scheduled poll, it never touches routes.last_run_at) — with
# nothing here, a user tapping it repeatedly fires a full multi-adapter search, S7's
# browser-solver included, on every tap. In-memory/per-process is fine: this is a
# UI-spam guard, not a durable rate limit, and a restart resetting it is harmless.
_CHECK_NOW_COOLDOWN_S = 60.0
_last_manual_check: dict[int, float] = {}


@router.message(Command("routes"))
async def cmd_routes(message: Message, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    await render_routes(message, message.from_user.id, db)


async def render_routes(message: Message, user_id: int, db: aiosqlite.Connection) -> None:
    routes = await RoutesRepo(db).list_for_user(user_id)
    if not routes:
        await message.answer("Пока нет отслеживаемых маршрутов.", reply_markup=routes_list_kb())
        return
    today = date.today()
    for route in routes:
        await message.answer(
            _route_card_text(route, today),
            reply_markup=route_card_kb(route.id, active=route.active, notification_mode=route.notification_mode),
        )


def _route_card_text(route: Route, today: date) -> str:
    status = "▶️ активен" if route.active else "⏸ на паузе"
    return (
        f"{route.title}\n"
        f"Даты: {describe_selection(selection_from_route(route), today)}\n"
        f"Порог: {format_money(route.max_price, route.max_price_currency)} · "
        f"каждые {route.poll_interval_min} мин · {status}\n"
        f"Уведомления: {describe_notification_mode(route.notification_mode)}"
    )


@router.callback_query(RouteAction.filter())
async def on_route_action(
    query: CallbackQuery,
    callback_data: RouteAction,
    state: FSMContext,
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
            await query.message.edit_reply_markup(
                reply_markup=route_card_kb(route.id, active=False, notification_mode=route.notification_mode)
            )
    elif callback_data.action == "resume":
        await routes_repo.set_active(route.id, True)
        await query.answer("Возобновил.")
        if isinstance(query.message, Message):
            await query.message.edit_reply_markup(
                reply_markup=route_card_kb(route.id, active=True, notification_mode=route.notification_mode)
            )
    elif callback_data.action == "notify":
        new_mode = "digest" if route.notification_mode == "instant" else "instant"
        await routes_repo.set_notification_mode(route.id, new_mode)
        if new_mode == "instant":
            # Whatever was staged belongs to a digest window that will now never be
            # delivered — leaving it would surface a stale price if the user switches back.
            await DigestsRepo(db).clear(route.id)
        await query.answer("⚡ Буду присылать сразу." if new_mode == "instant" else "📬 Буду присылать раз в день.")
        if isinstance(query.message, Message):
            updated = await routes_repo.get(route.id)
            assert updated is not None
            await query.message.edit_text(
                _route_card_text(updated, date.today()),
                reply_markup=route_card_kb(route.id, active=updated.active, notification_mode=new_mode),
            )
    elif callback_data.action == "delete":
        await routes_repo.delete(route.id)
        await query.answer("Удалил.")
        if isinstance(query.message, Message):
            await query.message.delete()
    elif callback_data.action == "check":
        last_check = _last_manual_check.get(route.id)
        now = time.monotonic()
        if last_check is not None and now - last_check < _CHECK_NOW_COOLDOWN_S:
            wait_s = int(_CHECK_NOW_COOLDOWN_S - (now - last_check))
            await query.answer(f"Недавно уже проверял — подожди {wait_s} с.", show_alert=True)
            return
        _last_manual_check[route.id] = now
        await query.answer("Проверяю...")
        user = await UsersRepo(db).get(route.user_id)
        assert user is not None
        await process_route(
            route,
            user=user,
            registry=registry,
            places_repo=PlacesRepo(db),
            routes_repo=routes_repo,
            observations_repo=ObservationsRepo(db),
            notifier=notifier,
        )
        assert query.message is not None
        await query.message.answer("Проверил. Если что-то нашлось ниже порога — уведомление придёт отдельно.")
    elif callback_data.action == "search":
        await query.answer()  # search below outlives Telegram's callback deadline
        if isinstance(query.message, Message):
            await search_handlers.start_from_route(query.message, state, db, registry, route)
