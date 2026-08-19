from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import pytest
from aiogram.types import CallbackQuery, Chat
from aiogram.types import User as TgUser

from aviapars.adapters.base import Mode
from aviapars.config import settings
from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.telegram.handlers import admin as admin_handlers
from aviapars.telegram.handlers import menu as menu_handlers
from aviapars.telegram.handlers import routes as routes_handlers
from aviapars.telegram.handlers import start as start_handlers
from aviapars.telegram.handlers import status as status_handlers
from aviapars.telegram.handlers.add import on_origin_text as add_on_origin_text
from aviapars.telegram.handlers.add import on_threshold_text
from aviapars.telegram.keyboards import MenuAction, RouteAction
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import AddRoute, EditRoute

from tests.conftest import DummyRegistry, FakeScreenBot, make_state, make_text_message

CHAT_ID = 555
USER_ID = 555


async def _noop_answer(*args: object, **kwargs: object) -> bool:
    return True


def make_query(bot: FakeScreenBot, data_action: str, *, user_id: int = USER_ID) -> CallbackQuery:
    query = CallbackQuery(
        id="q1",
        from_user=TgUser(id=user_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        data=data_action,
        message=None,
    )
    query.as_(bot)  # type: ignore[arg-type]
    # query.answer() calls bot(AnswerCallbackQuery(...)) — FakeScreenBot only implements
    # ScreenTransport (send/edit/delete), not the full Bot callable protocol. Handlers only
    # use query.answer() for the little toast/spinner Telegram shows, never its return value.
    object.__setattr__(query, "answer", _noop_answer)
    return query


async def make_place(db: aiosqlite.Connection, name: str = "Пенза") -> Place:
    # Places come from the seeded RU table in real use; tests insert directly since
    # migrations don't seed data.
    cursor = await db.execute(
        "INSERT INTO places (kind, name_ru, name_en, country, popularity, is_hub_candidate) "
        "VALUES ('city', ?, ?, 'RU', 10, 0)",
        (name, name),
    )
    await db.commit()
    place = await PlacesRepo(db).get(cursor.lastrowid)  # type: ignore[arg-type]
    assert place is not None
    return place


async def make_route(db: aiosqlite.Connection, *, user_id: int = USER_ID) -> int:
    await UsersRepo(db).get_or_create(user_id)
    origin = await make_place(db, "Пенза")
    dest = await make_place(db, "Сочи")
    route = await RoutesRepo(db).create(
        user_id=user_id,
        title="Пенза → Сочи",
        origin_place_id=origin.id,
        dest_place_id=dest.id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-18"],
        max_price=800000,
        max_price_currency="RUB",
    )
    return route.id


# --- /start, /menu, /help ---------------------------------------------------


async def test_cmd_start_creates_user_and_shows_welcome(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    message = make_text_message(CHAT_ID, "/start", bot)
    message.as_(bot)
    object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))

    await start_handlers.cmd_start(message, state, db)

    assert any("Привет" in t for t in bot.all_shown)
    user = await UsersRepo(db).get(USER_ID)
    assert user is not None


async def test_cmd_menu_and_help_render_without_state() -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state("some:leftover:state")

    message = make_text_message(CHAT_ID, "/menu", bot)
    await menu_handlers.cmd_menu(message, state)
    assert any("Главное меню" in t for t in bot.all_shown)
    assert await state.get_state() is None

    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    message2 = make_text_message(CHAT_ID, "/help", bot2)
    await menu_handlers.cmd_help(message2, state2)
    assert any("как это работает" in t.lower() for t in bot2.all_shown)


@pytest.mark.parametrize("action", ["add", "search", "routes", "status", "help", "root", "bogus"])
async def test_menu_action_never_crashes_for_any_action(db: aiosqlite.Connection, action: str) -> None:
    """`on_menu_action` has no state filter (reachable from any wizard step) and
    branches on a free-text `action` field carried over the wire — an unknown
    value must degrade to the main menu, not raise."""
    bot = FakeScreenBot()
    state = make_state(bot)
    query = make_query(bot, f"mn:{action}")
    callback_data = MenuAction(action=action)

    await menu_handlers.on_menu_action(query, callback_data, state, db, DummyRegistry())  # type: ignore[arg-type]

    assert bot.all_shown  # something was rendered, never a silent crash


# --- /status -----------------------------------------------------------------


def _fake_adapter(id_: str, display_name: str, modes: frozenset[Mode]) -> object:
    return SimpleNamespace(id=id_, display_name=display_name, modes=modes)


class _FakeRegistryWithAdapters:
    def __init__(self, adapters: list[object]) -> None:
        self._adapters = adapters

    def all(self) -> list[object]:
        return self._adapters


async def test_status_admin_view_covers_every_row_shape(db: aiosqlite.Connection) -> None:
    """Admin rendering branches on: no source_health row yet (⚪), a row with
    failures>0 (+ error kind), and a row with a clean last success — all three
    must render without crashing."""
    adapters = [
        _fake_adapter("never_polled", "Никогда не спрашивал", frozenset({Mode.PLANE})),
        _fake_adapter("failing", "Падает", frozenset({Mode.PLANE})),
        _fake_adapter("healthy", "Здоров", frozenset({Mode.TRAIN})),
    ]
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO source_health (source_id, status, last_success_at, consecutive_failures, last_error_kind) "
        "VALUES ('failing', 'down', ?, 5, 'blocked')",
        (now_iso,),
    )
    await db.execute(
        "INSERT INTO source_health (source_id, status, last_success_at, consecutive_failures, last_error_kind) "
        "VALUES ('healthy', 'ok', ?, 0, NULL)",
        (now_iso,),
    )
    await db.commit()

    bot = FakeScreenBot()
    state = make_state(bot)
    await status_handlers.render_status(
        await screen_for(make_text_message(CHAT_ID, "/status", bot), state),
        db,
        _FakeRegistryWithAdapters(adapters),  # type: ignore[arg-type]
        is_admin=True,
    )

    shown = "\n".join(bot.all_shown)
    assert "ещё не опрашивал" in shown
    assert "blocked" in shown
    assert "отвечал" in shown


async def test_status_non_admin_view_groups_by_mode(db: aiosqlite.Connection) -> None:
    adapters = [_fake_adapter("healthy", "Здоров", frozenset({Mode.PLANE}))]
    bot = FakeScreenBot()
    state = make_state(bot)

    await status_handlers.render_status(
        await screen_for(make_text_message(CHAT_ID, "/status", bot), state),
        db,
        _FakeRegistryWithAdapters(adapters),  # type: ignore[arg-type]
        is_admin=False,
    )
    shown = "\n".join(bot.all_shown)
    assert "источников отвечают" in shown
    # non-admin view must not leak adapter internals
    assert "blocked" not in shown


async def test_cmd_status_dispatches_admin_vs_non_admin(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    message = make_text_message(CHAT_ID, "/status", bot)
    object.__setattr__(message, "from_user", TgUser(id=settings.admin_tg_id, is_bot=False, first_name="A"))
    await status_handlers.cmd_status(message, state, db, DummyRegistry())  # type: ignore[arg-type]
    assert any("Уведомлений в очереди" in t for t in bot.all_shown)  # admin-only line

    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    message2 = make_text_message(CHAT_ID, "/status", bot2)
    object.__setattr__(message2, "from_user", TgUser(id=settings.admin_tg_id + 1, is_bot=False, first_name="U"))
    await status_handlers.cmd_status(message2, state2, db, DummyRegistry())  # type: ignore[arg-type]
    assert not any("Уведомлений в очереди" in t for t in bot2.all_shown)


# --- /routes -------------------------------------------------------------


async def test_render_routes_empty_and_populated(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await routes_handlers.render_routes(Screen(bot, CHAT_ID, state), USER_ID, db)
    assert any("Пока ни одного" in t for t in bot.all_shown)

    route_id = await make_route(db)
    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    await routes_handlers.render_routes(Screen(bot2, CHAT_ID, state2), USER_ID, db)
    # FakeScreenBot only records text, not keyboard markup — the per-route titles live on
    # the buttons (routes_list_kb), so the non-empty branch is distinguished by its own
    # copy text, not by a place name showing up in the message body.
    assert any("Откройте любой" in t for t in bot2.all_shown)
    assert route_id > 0


async def _route_action(
    db: aiosqlite.Connection, route_id: int, action: str, *, user_id: int = USER_ID
) -> tuple[FakeScreenBot, CallbackQuery]:
    bot = FakeScreenBot()
    state = make_state(bot)
    query = make_query(bot, f"r:{action}:{route_id}", user_id=user_id)
    callback_data = RouteAction(action=action, route_id=route_id)

    notifications_repo = NotificationsRepo(db)
    jobs_repo = JobsRepo(db)
    notifier = Notifier(
        bot,  # type: ignore[arg-type]
        notifications_repo,
        jobs_repo,
        DedupeGate(notifications_repo),
        cooldown_h=12,
        daily_cap=3,
    )
    await routes_handlers.on_route_action(
        query, callback_data, state, db, DummyRegistry(), notifier  # type: ignore[arg-type]
    )
    return bot, query


async def test_route_not_found_or_not_owned_shows_alert(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db)
    bot = FakeScreenBot()
    state = make_state(bot)
    query = make_query(bot, "r:open:99999")
    await routes_handlers.on_route_action(
        query, RouteAction(action="open", route_id=99999), state, db, DummyRegistry(), None  # type: ignore[arg-type]
    )
    assert bot.all_shown == []  # never got past the not-found alert

    # Owned by a different user
    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    query2 = make_query(bot2, f"r:open:{route_id}", user_id=USER_ID + 1)
    await routes_handlers.on_route_action(
        query2, RouteAction(action="open", route_id=route_id), state2, db, DummyRegistry(), None  # type: ignore[arg-type]
    )
    assert bot2.all_shown == []


async def test_route_open_pause_resume_notify_delete(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db)

    bot, _ = await _route_action(db, route_id, "open")
    assert any("Пенза" in t for t in bot.all_shown)

    bot, _ = await _route_action(db, route_id, "pause")
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.active is False

    bot, _ = await _route_action(db, route_id, "resume")
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.active is True

    bot, _ = await _route_action(db, route_id, "notify")
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.notification_mode == "digest"

    bot, _ = await _route_action(db, route_id, "delete")
    assert await RoutesRepo(db).get(route_id) is None


async def test_route_check_now_runs_and_cooldown_blocks_repeat_taps(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db)
    bot, _ = await _route_action(db, route_id, "check")
    assert any("Проверил" in t for t in bot.all_shown)

    # Second tap within the cooldown window must not crash and must show the wait alert
    # instead of re-running the search (module-level `_last_manual_check` persists across
    # calls in this process, which is exactly what the cooldown relies on).
    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    query2 = make_query(bot2, f"r:check:{route_id}")
    notifications_repo = NotificationsRepo(db)
    jobs_repo = JobsRepo(db)
    notifier = Notifier(
        bot2,  # type: ignore[arg-type]
        notifications_repo,
        jobs_repo,
        DedupeGate(notifications_repo),
        cooldown_h=12,
        daily_cap=3,
    )
    await routes_handlers.on_route_action(
        query2, RouteAction(action="check", route_id=route_id), state2, db, DummyRegistry(), notifier  # type: ignore[arg-type]
    )
    assert bot2.all_shown == []  # blocked before any screen.show — only query.answer(), which FakeScreenBot doesn't record


async def test_route_search_bridge_and_interval_prompt(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db)
    bot, _ = await _route_action(db, route_id, "search")
    assert any("Ищу варианты" in t or "Пенза" in t for t in bot.all_shown)

    bot, _ = await _route_action(db, route_id, "interval")
    assert any("Интервал проверки" in t for t in bot.all_shown)


async def test_interval_edit_missing_route_id_clears_state(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditRoute.interval)
    message = make_text_message(CHAT_ID, "60", bot)
    await routes_handlers.on_interval_edit_text(message, state, db)
    assert await state.get_state() is None
    assert bot.all_shown == []


async def test_interval_edit_deleted_route_reports_not_found(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db)
    await RoutesRepo(db).delete(route_id)
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditRoute.interval)
    await state.update_data(edit_route_id=route_id)
    message = make_text_message(CHAT_ID, "60", bot)
    object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await routes_handlers.on_interval_edit_text(message, state, db)
    assert any("уже удалён" in t for t in bot.all_shown)


@pytest.mark.parametrize("text", ["0", "-5", "abc", "99999999", "1.5", ""])
async def test_interval_edit_rejects_bad_values(db: aiosqlite.Connection, text: str) -> None:
    route_id = await make_route(db)
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditRoute.interval)
    await state.update_data(edit_route_id=route_id)
    message = make_text_message(CHAT_ID, text, bot)
    object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await routes_handlers.on_interval_edit_text(message, state, db)
    # Stays in the edit state — none of these are valid interval values
    assert await state.get_state() == EditRoute.interval.state
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.poll_interval_min == 60  # unchanged


# --- admin_only gate ---------------------------------------------------------


async def test_admin_only_silently_noops_for_non_admin(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    message = make_text_message(CHAT_ID, "/pause_all", bot)
    object.__setattr__(message, "from_user", TgUser(id=settings.admin_tg_id + 1, is_bot=False, first_name="U"))
    await admin_handlers.cmd_pause_all(message, db)  # type: ignore[call-arg]
    # message.answer would raise (no bot session) if this ever got past the guard —
    # not raising here is itself the assertion that admin_only short-circuited.


async def test_pause_all_pauses_only_active_routes_for_admin(db: aiosqlite.Connection) -> None:
    route_id = await make_route(db, user_id=settings.admin_tg_id)
    bot = FakeScreenBot()
    message = make_text_message(CHAT_ID, "/pause_all", bot)
    message.as_(bot)
    object.__setattr__(message, "from_user", TgUser(id=settings.admin_tg_id, is_bot=False, first_name="A"))

    sent: list[str] = []

    async def fake_answer(text: str) -> None:
        sent.append(text)

    object.__setattr__(message, "answer", fake_answer)
    await admin_handlers.cmd_pause_all(message, db)

    assert sent and "1" in sent[0]
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.active is False


# --- free-text edge cases: empty / very long / unicode / emoji ---------------


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty string is still `text is not None` — a distinct path from the sticker/photo guard
        " ",
        "🚀🚀🚀✈️🚂",  # emoji only, no place name in it at all
        pytest.param("Пенза" * 2000, id="10k_chars"),  # regex normalize + SQL LIKE must not choke or hang
    ],
)
async def test_origin_text_degrades_gracefully_on_garbage_input(db: aiosqlite.Connection, text: str) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.origin)
    message = make_text_message(CHAT_ID, text, bot)
    await add_on_origin_text(message, state, db)  # must not raise
    assert bot.all_shown  # some re-prompt or "not found" screen was rendered
    assert await state.get_state() in (AddRoute.origin.state, AddRoute.origin_pick.state)


@pytest.mark.parametrize(
    "text,should_pass",
    [
        ("0", False),
        ("-100", False),
        ("999999999999999999999999999999", False),  # way past _MAX_PRICE_RUB, but a valid Python int
        ("8000.50", False),  # decimal not accepted — int() rejects it
        ("8 000", True),  # spaces are stripped, this is a legitimate way to type a price
        ("100000000", True),  # exactly _MAX_PRICE_RUB — the ceiling itself is inclusive (`>`, not `>=`)
        ("100000001", False),  # one over the ceiling
        ("🙂", False),
    ],
)
async def test_threshold_text_edge_cases(text: str, should_pass: bool) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.threshold)
    await state.update_data(origin_title="Пенза", dest_title="Сочи", mode_pref="plane")
    message = make_text_message(CHAT_ID, text, bot)
    await on_threshold_text(message, state)  # must not raise regardless of input shape
    if should_pass:
        assert await state.get_state() == AddRoute.interval.state
    else:
        assert await state.get_state() == AddRoute.threshold.state
        assert any("⚠️" in t for t in bot.all_shown)
