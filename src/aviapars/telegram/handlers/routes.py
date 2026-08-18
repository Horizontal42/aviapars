from __future__ import annotations

import time
from datetime import date

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.db.models import Route
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.worker import process_route
from aviapars.domain.dates import selection_from_route
from aviapars.telegram.format import describe_notification_mode, describe_selection, describe_thresholds
from aviapars.telegram.handlers import search as search_handlers
from aviapars.telegram.keyboards import RouteAction, menu_only_kb, route_card_kb, routes_list_kb
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import EditRoute

router = Router(name="routes")

# "🔍 Проверить сейчас" bypasses the scheduler entirely and calls process_route()
# in-process (unlike a scheduled poll, it never touches routes.last_run_at) — with
# nothing here, a user tapping it repeatedly fires a full multi-adapter search, S7's
# browser-solver included, on every tap. In-memory/per-process is fine: this is a
# UI-spam guard, not a durable rate limit, and a restart resetting it is harmless.
_CHECK_NOW_COOLDOWN_S = 60.0
_last_manual_check: dict[int, float] = {}

# Same sanity ceiling as the /add wizard's own interval step (handlers/add.py) — kept as a
# local constant there too rather than a shared import, matching this file's existing
# per-handler sanity-ceiling style (_CHECK_NOW_COOLDOWN_S above).
_MAX_INTERVAL_MIN = 43_200  # 30 days


@router.message(Command("routes"))
async def cmd_routes(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    await render_routes(await screen_for(message, state), message.from_user.id, db)


async def render_routes(screen: Screen, user_id: int, db: aiosqlite.Connection) -> None:
    routes = await RoutesRepo(db).list_for_user(user_id)
    if not routes:
        await screen.show("Пока нет отслеживаемых маршрутов.", reply_markup=routes_list_kb())
        return
    await screen.show(f"Отслеживаю маршрутов: {len(routes)}. Откройте любой:", reply_markup=routes_list_kb(routes))


async def render_card(screen: Screen, route: Route, *, note: str = "") -> None:
    text = _route_card_text(route, date.today())
    await screen.show(
        f"{note}\n\n{text}" if note else text,
        reply_markup=route_card_kb(route.id, active=route.active, notification_mode=route.notification_mode),
    )


def _route_card_text(route: Route, today: date) -> str:
    status = "▶️ активен" if route.active else "⏸ на паузе"
    return (
        f"{route.title}\n"
        f"Даты: {describe_selection(selection_from_route(route), today)}\n"
        f"Порог: {describe_thresholds(route.max_price, route.max_price_currency, route.max_price_by_mode)} · "
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
    screen = await screen_for(query, state)

    if callback_data.action == "open":
        await query.answer()
        await render_card(screen, route)
    elif callback_data.action == "pause":
        await routes_repo.set_active(route.id, False)
        await query.answer("Поставил на паузу.")
        await _rerender(screen, routes_repo, route.id)
    elif callback_data.action == "resume":
        await routes_repo.set_active(route.id, True)
        await query.answer("Возобновил.")
        await _rerender(screen, routes_repo, route.id)
    elif callback_data.action == "notify":
        new_mode = "digest" if route.notification_mode == "instant" else "instant"
        await routes_repo.set_notification_mode(route.id, new_mode)
        await query.answer("⚡ Буду присылать сразу." if new_mode == "instant" else "📬 Буду присылать раз в день.")
        await _rerender(screen, routes_repo, route.id)
    elif callback_data.action == "delete":
        await routes_repo.delete(route.id)
        await query.answer("Удалил.")
        await render_routes(screen, query.from_user.id, db)
    elif callback_data.action == "check":
        last_check = _last_manual_check.get(route.id)
        now = time.monotonic()
        if last_check is not None and now - last_check < _CHECK_NOW_COOLDOWN_S:
            wait_s = int(_CHECK_NOW_COOLDOWN_S - (now - last_check))
            await query.answer(f"Недавно уже проверял — подожди {wait_s} с.", show_alert=True)
            return
        _last_manual_check[route.id] = now
        await query.answer("Проверяю...")
        await screen.show("⏳ Проверяю цены по маршруту…")
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
        await _rerender(
            screen, routes_repo, route.id, note="Проверил. Если что-то нашлось ниже порога — уведомление придёт отдельно."
        )
    elif callback_data.action == "search":
        await query.answer()  # search below outlives Telegram's callback deadline
        await search_handlers.start_from_route(screen, state, db, registry, route)
    elif callback_data.action == "interval":
        await state.update_data(edit_route_id=route.id)
        await state.set_state(EditRoute.interval)
        await query.answer()
        await screen.show(
            f"Сейчас проверяю каждые {route.poll_interval_min} мин. Введите новый интервал в минутах "
            f"(от {settings.poll_interval_floor_min} до {_MAX_INTERVAL_MIN}).",
            reply_markup=menu_only_kb(),
        )


async def _rerender(screen: Screen, routes_repo: RoutesRepo, route_id: int, *, note: str = "") -> None:
    """Re-reads the route rather than patching the in-memory copy: the card
    prints several fields the action just changed, and a stale one would show
    the old value right next to the button that changed it."""
    updated = await routes_repo.get(route_id)
    if updated is None:
        return
    await render_card(screen, updated, note=note)


@router.message(EditRoute.interval)
async def on_interval_edit_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.text is not None
    screen = await screen_for(message, state)
    data = await state.get_data()
    route_id = data.get("edit_route_id")
    if route_id is None:
        await state.clear()
        return
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await screen.show("Не понял число. Введите интервал в минутах, например 60.", reply_markup=menu_only_kb())
        return
    if not settings.poll_interval_floor_min <= minutes <= _MAX_INTERVAL_MIN:
        await screen.show(
            f"Введите от {settings.poll_interval_floor_min} до {_MAX_INTERVAL_MIN} минут.",
            reply_markup=menu_only_kb(),
        )
        return

    assert message.from_user is not None
    routes_repo = RoutesRepo(db)
    route = await routes_repo.get(route_id)
    if route is None or route.user_id != message.from_user.id:
        await state.clear()
        await screen.show("Маршрут не найден.")
        return
    await routes_repo.set_poll_interval(route_id, minutes)
    await state.clear()
    await _rerender(screen, routes_repo, route_id, note=f"Готово, теперь каждые {minutes} мин.")
