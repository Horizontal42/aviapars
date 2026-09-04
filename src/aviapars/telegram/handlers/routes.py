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
from aviapars.telegram.format import format_route_card, screen_text
from aviapars.telegram.handlers import search as search_handlers
from aviapars.telegram.handlers.constants import MAX_INTERVAL_MIN
from aviapars.telegram.keyboards import RouteAction, menu_only_kb, route_card_kb, routes_list_kb
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import EditRoute

router = Router(name="routes")

TITLE = "📋 Мои маршруты"

# "🔍 Проверить сейчас" bypasses the scheduler entirely and calls process_route()
# in-process (unlike a scheduled poll, it never touches routes.last_run_at) — with
# nothing here, a user tapping it repeatedly fires a full multi-adapter search, S7's
# browser-solver included, on every tap. In-memory/per-process is fine: this is a
# UI-spam guard, not a durable rate limit, and a restart resetting it is harmless.
_CHECK_NOW_COOLDOWN_S = 60.0
_last_manual_check: dict[int, float] = {}


@router.message(Command("routes"))
async def cmd_routes(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    await render_routes(await screen_for(message, state), message.from_user.id, db)


async def render_routes(screen: Screen, user_id: int, db: aiosqlite.Connection) -> None:
    routes = await RoutesRepo(db).list_for_user(user_id)
    if not routes:
        await screen.show(
            screen_text(
                TITLE,
                "Пока ни одного. Заведите первый — и я напишу, как только цена упадёт ниже вашего порога.",
            ),
            reply_markup=routes_list_kb(),
        )
        return
    await screen.show(
        screen_text(TITLE, "Откройте любой — там пауза, интервал, проверка сейчас и удаление."),
        reply_markup=routes_list_kb(routes),
    )


async def render_card(screen: Screen, route: Route, *, note: str = "") -> None:
    card = format_route_card(route, date.today())
    await screen.show(
        f"{note}\n\n{card}" if note else card,
        reply_markup=route_card_kb(route.id, active=route.active, notification_mode=route.notification_mode),
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
        await screen.show(screen_text(route.title, "⏳ Проверяю цены прямо сейчас…"))
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
            screen,
            routes_repo,
            route.id,
            note="✅ Проверил. Если нашлось что-то ниже порога — уведомление придёт отдельным сообщением.",
        )
    elif callback_data.action == "search":
        await query.answer()  # search below outlives Telegram's callback deadline
        await search_handlers.start_from_route(screen, state, db, registry, route)
    elif callback_data.action == "interval":
        await state.update_data(edit_route_id=route.id)
        await state.set_state(EditRoute.interval)
        await query.answer()
        await screen.show(
            screen_text(
                "⏱ Интервал проверки",
                route.title,
                f"Сейчас проверяю каждые {route.poll_interval_min} мин.\n"
                f"Напишите новый интервал в минутах — от {settings.poll_interval_floor_min} "
                f"до {MAX_INTERVAL_MIN}.",
            ),
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
    screen = await screen_for(message, state)
    data = await state.get_data()
    route_id = data.get("edit_route_id")
    if route_id is None:
        await state.clear()
        return
    if message.text is None:
        # Same class of bug as handlers/add.py/search.py: no content-type filter here, so a
        # sticker/photo mid-edit used to hit an unguarded assert and silently kill the update.
        await screen.show(
            screen_text("⏱ Интервал проверки", "⚠️ Не понял. Пришлите ответ текстом."),
            reply_markup=menu_only_kb(),
        )
        return
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await screen.show(
            screen_text("⏱ Интервал проверки", "⚠️ Нужно целое число минут, например 60."),
            reply_markup=menu_only_kb(),
        )
        return
    if not settings.poll_interval_floor_min <= minutes <= MAX_INTERVAL_MIN:
        await screen.show(
            screen_text(
                "⏱ Интервал проверки",
                f"⚠️ Нужно число от {settings.poll_interval_floor_min} до {MAX_INTERVAL_MIN} минут.",
            ),
            reply_markup=menu_only_kb(),
        )
        return

    assert message.from_user is not None
    routes_repo = RoutesRepo(db)
    route = await routes_repo.get(route_id)
    if route is None or route.user_id != message.from_user.id:
        await state.clear()
        await screen.show(screen_text(TITLE, "⚠️ Маршрут не найден — возможно, он уже удалён."))
        return
    await routes_repo.set_poll_interval(route_id, minutes)
    await state.clear()
    await _rerender(screen, routes_repo, route_id, note=f"✅ Готово, теперь каждые {minutes} мин.")
