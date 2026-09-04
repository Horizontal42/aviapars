from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Update
from aiogram.types import User as TgUser

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.config import settings
from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.domain.combiner import Itinerary
from aviapars.domain.dates import exact_date
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.telegram.handlers import admin as admin_handlers
from aviapars.telegram.handlers import menu as menu_handlers
from aviapars.telegram.handlers import routes as routes_handlers
from aviapars.telegram.handlers import search as search_handlers
from aviapars.telegram.handlers import start as start_handlers
from aviapars.telegram.handlers import settings as settings_handlers
from aviapars.telegram.handlers import status as status_handlers
from aviapars.telegram.handlers.add import on_origin_text as add_on_origin_text
from aviapars.telegram.handlers.add import on_threshold_text
from aviapars.telegram.bot import create_dispatcher
from aviapars.telegram.keyboards import MenuAction, RouteAction, SearchTrack
from aviapars.telegram.screen import SCREEN_KEY, Screen, screen_for
from aviapars.telegram.states import AddRoute, EditRoute, EditSettings, SearchFlow

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


# --- unmatched free text: no wizard open, or FSM state lost to a restart --------


async def test_unmatched_message_shows_a_reply_instead_of_dying_silently() -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.mode)  # leftover state, e.g. from a crashed/aborted flow
    message = make_text_message(CHAT_ID, "прив", bot)

    await menu_handlers.on_unmatched_message(message, state)

    assert any("Не понял" in t for t in bot.all_shown)
    assert await state.get_state() is None


class _IdBot(FakeScreenBot):
    """feed_update() derives the FSM StorageKey from `bot.id` — a real aiogram Bot always
    has one, FakeScreenBot doesn't need it for direct handler calls but the dispatcher-level
    test below has to line up with the same key aiogram computes internally."""

    id = 1


async def test_dispatcher_prefers_wizard_state_over_the_fallback(db: aiosqlite.Connection) -> None:
    """The routing-order regression this fix could introduce: fallback_router is included
    last precisely so it never wins over an in-progress wizard's own state-specific handler.
    Verified through the real Dispatcher, not by calling handlers directly, because the bug
    this guards against is about *registration order*, which only the real router chain can
    prove."""
    dp = create_dispatcher()
    bot = _IdBot()

    def feed(bot_: _IdBot, text: str, update_id: int) -> Update:
        message = make_text_message(CHAT_ID, text, bot_)
        object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
        return Update(update_id=update_id, message=message)

    # No state: the fallback must catch it.
    await dp.feed_update(
        bot, feed(bot, "нет активного шага", 1), db=db, registry=DummyRegistry(), notifier=None, shutdown_event=asyncio.Event()
    )
    assert any("Не понял" in t for t in bot.all_shown)

    # Now seed AddRoute.origin for the same chat/user and feed another text message —
    # the wizard's own on_origin_text must win, not the fallback.
    bot2 = _IdBot()
    fsm = FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot2.id, chat_id=CHAT_ID, user_id=USER_ID))
    await fsm.set_state(AddRoute.origin)
    await dp.feed_update(
        bot2,
        feed(bot2, "Пенза", 2),
        db=db,
        registry=DummyRegistry(),
        notifier=None,
        shutdown_event=asyncio.Event(),
    )
    assert not any("Не понял" in t for t in bot2.all_shown)
    # State stayed inside the wizard (origin lookup found nothing in this empty test db and
    # re-prompted) — the proof this needs is that on_origin_text ran at all, not the fallback.
    assert await fsm.get_state() == AddRoute.origin.state


# --- /search results: "show all" pagination and the "💾 Следить за" bridge --------


def _make_itinerary(price_minor: int = 500000) -> Itinerary:
    origin = PlaceRef(place_id=1, iata="PEZ", yandex_code=None, rzd_code=None, city_id=None)
    dest = PlaceRef(place_id=2, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
    now = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
    leg = Leg(mode=Mode.PLANE, from_place=origin, to_place=dest, depart_utc=now, depart_local=now, carrier="Победа")
    offer = Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=None,
        source_id="pobeda_direct",
        tier=Tier.CONFIRMATION,
        observed_at=now,
        source_cached_at=now,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=f"hash{price_minor}",
    )
    return Itinerary(offers=(offer,), price_minor=price_minor, currency="RUB")


async def _seeded_search_results_state(bot: FakeScreenBot, n: int = 8) -> FSMContext:
    """Mimics what `_run_search` stashes in state right before rendering results — enough
    for `on_search_show_all`/`on_search_track` to run without actually calling a real adapter."""
    state = make_state(bot)
    await state.set_state(SearchFlow.results)
    itineraries = [_make_itinerary(500000 + i * 1000) for i in range(n)]
    await state.update_data(
        origin_place_id=1,
        origin_title="Пенза",
        dest_place_id=2,
        dest_title="Сочи",
        mode_pref="plane",
        results_all_itineraries=itineraries,
        results_found_total=n,
        results_route_title="Пенза → Сочи",
        results_title="🔎 Пенза → Сочи",
        results_selection=exact_date(datetime(2026, 9, 18).date()),
        results_checked="проверено: 18.09.26",
        results_source_names={"pobeda_direct": "Победа"},
        results_itineraries=itineraries[:5],
    )
    return state


async def test_search_show_all_expands_and_stale_state_shows_alert() -> None:
    bot = FakeScreenBot()
    state = await _seeded_search_results_state(bot, n=8)
    query = make_query(bot, "sall")

    await search_handlers.on_search_show_all(query, state)

    assert any("Показываю" in t for t in bot.all_shown)
    assert "8" in bot.all_shown[-1]

    # A stale/cleared search (state.clear() ran, or the bot restarted) must alert, not crash.
    bot2 = FakeScreenBot()
    state2 = make_state(bot2)
    query2 = make_query(bot2, "sall")
    await search_handlers.on_search_show_all(query2, state2)  # must not raise
    assert bot2.all_shown == []


async def test_search_track_bridges_into_add_threshold_step() -> None:
    bot = FakeScreenBot()
    state = await _seeded_search_results_state(bot, n=3)
    query = make_query(bot, "st:0")

    await search_handlers.on_search_track(query, SearchTrack(idx=0), state)

    assert await state.get_state() == AddRoute.threshold.state
    assert any("Ниже какой цены" in t for t in bot.all_shown)


# --- full user journeys: menu screen survives a deal-notification burst -----
#
# Regression coverage for two bugs reported live: a typed /menu right after the
# scheduler had fired a batch of deal alerts appeared to "eat" the command and
# show nothing. Root causes (see screen.py): (1) TelegramRetryAfter, Telegram's
# per-chat flood control tripped by the burst, wasn't caught anywhere and blew
# up the render silently; (2) even when the render succeeded, it edited the old
# anchor message the burst had buried, which is invisible from the chat's
# bottom. These tests drive the real handlers end to end, the way an actual
# Telegram session would.


async def test_menu_after_notification_burst_gets_a_fresh_message(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)

    start_msg = make_text_message(CHAT_ID, "/start", bot, message_id=1)
    object.__setattr__(start_msg, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await start_handlers.cmd_start(start_msg, state, db)

    menu_msg = make_text_message(CHAT_ID, "/menu", bot, message_id=2)
    object.__setattr__(menu_msg, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await menu_handlers.cmd_menu(menu_msg, state)

    anchor = (await state.get_data())[SCREEN_KEY]
    assert anchor is not None
    edits_so_far = len(bot.edited)
    sends_so_far = len(bot.sent)

    # The scheduler's Notifier sends deal alerts straight to the chat, outside of
    # Screen entirely (notify/notifier.py) — a realistic burst after the menu opened.
    for i in range(15):
        await bot.send_message(chat_id=CHAT_ID, text=f"🔥 Дело #{i}: цена упала")

    # The user, looking at the bottom of the chat where nothing changed, types /menu
    # again — this must not silently edit the now-buried anchor.
    menu_msg_2 = make_text_message(CHAT_ID, "/menu", bot, message_id=anchor + 16)
    object.__setattr__(menu_msg_2, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await menu_handlers.cmd_menu(menu_msg_2, state)

    assert len(bot.edited) == edits_so_far  # no invisible edit to the buried anchor
    assert len(bot.sent) == sends_so_far + 15 + 1  # the burst itself, plus the fresh menu
    assert "Главное меню" in bot.sent[-1][1]
    new_anchor = (await state.get_data())[SCREEN_KEY]
    assert new_anchor != anchor  # re-anchored on the fresh message


async def test_menu_shortly_after_a_couple_messages_still_edits_in_place(db: aiosqlite.Connection) -> None:
    """Contrast case: a handful of intervening messages must not force a fresh
    message on every /menu tap — only a real burst that buries the anchor should."""
    bot = FakeScreenBot()
    state = make_state(bot)

    start_msg = make_text_message(CHAT_ID, "/start", bot, message_id=1)
    object.__setattr__(start_msg, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await start_handlers.cmd_start(start_msg, state, db)

    menu_msg = make_text_message(CHAT_ID, "/menu", bot, message_id=2)
    object.__setattr__(menu_msg, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await menu_handlers.cmd_menu(menu_msg, state)
    anchor = (await state.get_data())[SCREEN_KEY]

    await bot.send_message(chat_id=CHAT_ID, text="🔥 Дело: цена упала")  # just one notification

    menu_msg_2 = make_text_message(CHAT_ID, "/menu", bot, message_id=anchor + 2)
    object.__setattr__(menu_msg_2, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    sends_before = len(bot.sent)
    await menu_handlers.cmd_menu(menu_msg_2, state)

    assert len(bot.sent) == sends_before  # still an edit, not a fresh message
    assert bot.edited[-1][1] == anchor


async def test_notify_toggle_button_edits_the_tapped_message_regardless_of_burst_size(
    db: aiosqlite.Connection,
) -> None:
    """A button tap re-anchors on the message actually tapped (screen_for's
    CallbackQuery branch) — unlike a typed command, it is never "buried" by a
    burst, since the user is looking straight at the message they just tapped."""
    route_id = await make_route(db)
    bot = FakeScreenBot()

    for i in range(20):  # a burst far bigger than the typed-command staleness threshold
        await bot.send_message(chat_id=CHAT_ID, text=f"🔥 Дело #{i}")

    tapped_message = make_text_message(CHAT_ID, "card", bot, message_id=99)
    query = CallbackQuery(
        id="q1",
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="U"),
        chat_instance="ci",
        data=f"r:notify:{route_id}",
        message=tapped_message,
    )
    query.as_(bot)  # type: ignore[arg-type]
    object.__setattr__(query, "answer", _noop_answer)

    state = make_state(bot)
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
        query, RouteAction(action="notify", route_id=route_id), state, db, DummyRegistry(), notifier  # type: ignore[arg-type]
    )

    assert bot.edited[-1][1] == 99  # edited the exact message the user tapped, burst or not
    route = await RoutesRepo(db).get(route_id)
    assert route is not None and route.notification_mode == "digest"


async def test_search_track_stale_index_shows_alert_not_crash() -> None:
    bot = FakeScreenBot()
    state = await _seeded_search_results_state(bot, n=3)
    query = make_query(bot, "st:99")

    await search_handlers.on_search_track(query, SearchTrack(idx=99), state)  # must not raise
    assert await state.get_state() == SearchFlow.results.state  # untouched — the bridge refused


# --- /settings: quiet hours and digest time ---------------------------------


async def test_settings_screen_shows_current_delivery_preferences(db: aiosqlite.Connection) -> None:
    await UsersRepo(db).get_or_create(USER_ID)
    bot = FakeScreenBot()
    state = make_state(bot)
    message = make_text_message(CHAT_ID, "/settings", bot)
    message.as_(bot)
    object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))

    await settings_handlers.cmd_settings(message, state, db)

    shown = bot.all_shown[-1]
    assert "23:00–08:00" in shown  # schema defaults
    assert "конец тихих часов" in shown  # digest_time unset == the old behaviour


async def test_digest_time_can_be_set_and_reset(db: aiosqlite.Connection) -> None:
    users = UsersRepo(db)
    await users.get_or_create(USER_ID)
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditSettings.digest_time)

    message = make_text_message(CHAT_ID, "9:30", bot)
    message.as_(bot)
    object.__setattr__(message, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await settings_handlers.on_digest_time_text(message, state, db)

    user = await users.get(USER_ID)
    assert user is not None and user.digest_time == "09:30"  # normalised, typed as "9:30"
    assert await state.get_state() is None

    await state.set_state(EditSettings.digest_time)
    reset = make_text_message(CHAT_ID, "-", bot)
    reset.as_(bot)
    object.__setattr__(reset, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await settings_handlers.on_digest_time_text(reset, state, db)

    user = await users.get(USER_ID)
    assert user is not None and user.digest_time is None


async def test_quiet_hours_edit_rejects_junk_without_changing_anything(db: aiosqlite.Connection) -> None:
    users = UsersRepo(db)
    await users.get_or_create(USER_ID)
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditSettings.quiet_hours)

    junk = make_text_message(CHAT_ID, "ночью", bot)
    junk.as_(bot)
    object.__setattr__(junk, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await settings_handlers.on_quiet_hours_text(junk, state, db)

    assert "⚠️" in bot.all_shown[-1]
    assert await state.get_state() == EditSettings.quiet_hours.state  # still waiting
    user = await users.get(USER_ID)
    assert user is not None and (user.quiet_hours_start, user.quiet_hours_end) == ("23:00", "08:00")

    good = make_text_message(CHAT_ID, "22:00 - 7:15", bot)
    good.as_(bot)
    object.__setattr__(good, "from_user", TgUser(id=USER_ID, is_bot=False, first_name="U"))
    await settings_handlers.on_quiet_hours_text(good, state, db)

    user = await users.get(USER_ID)
    assert user is not None and (user.quiet_hours_start, user.quiet_hours_end) == ("22:00", "07:15")
