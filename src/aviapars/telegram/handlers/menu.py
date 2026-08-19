from __future__ import annotations

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.telegram.format import screen_text
from aviapars.telegram.handlers import add, routes, search, status
from aviapars.telegram.keyboards import MenuAction, main_menu_kb
from aviapars.telegram.screen import Screen, screen_for

router = Router(name="menu")

# Separate from `router` on purpose: `create_dispatcher()` includes `menu.router` FIRST so its
# state-agnostic MenuAction callback stays reachable mid-wizard (see its own docstring below) —
# a catch-all *message* handler in that same router would win that same first-refusal and
# swallow every wizard's typed steps before they ever saw the message. `fallback_router` is
# included LAST instead, so every state-specific handler gets first try and this only fires
# once nothing else claimed the update — e.g. free text with no wizard open (a plain "привет"),
# or a wizard's FSM state lost to a bot restart (in-memory storage, no persistence — see
# ARCHITECTURE.md). Before this, that update matched no filter at all and aiogram dropped it
# with no reply and no trace — from the user's side, the bot looked dead.
fallback_router = Router(name="fallback")

MENU_TEXT = screen_text(
    "☰ Главное меню",
    "Слежу за ценами на авиа- и жд-билеты и пишу, когда цена опускается ниже вашего порога.",
)

HELP_TEXT = screen_text(
    "❓ Как это работает",
    "➕ <b>Новый маршрут</b> — слежу за ценой в фоне и пишу, когда она упадёт ниже порога.\n"
    "📋 <b>Мои маршруты</b> — пауза, проверка сейчас, интервал, удаление.\n"
    "🔎 <b>Разовый поиск</b> — показываю варианты прямо сейчас, ничего не сохраняя.\n"
    "📊 <b>Источники</b> — какие источники цен сейчас отвечают.",
    "<b>Даты.</b> Точная, ±3 дня вокруг опорной, диапазон «с … по …» или окно на N дней вперёд. "
    "Точную дату проверяю целиком; широкое окно фоновый опрос обходит по частям — каждый цикл новый кусок.",
    "<b>Цены.</b> Часть источников отдаёт кэш агрегатора — такие варианты помечены «⚠️ не подтверждено». "
    "Остальные спрашивают перевозчика напрямую, это «✅ подтверждено».",
    "То же самое командами: /add, /routes, /search, /status, /menu.",
)

_UNMATCHED_TEXT = screen_text(
    "🤔 Не понял",
    "Так текстом не отвечу — я слежу за маршрутом по шагам через кнопки и команды. Если был "
    "на середине шага — попробуйте начать заново.",
)


async def show_menu(screen: Screen) -> None:
    await screen.show(MENU_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    screen = await screen_for(message, state)
    await state.clear()
    await show_menu(screen)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    screen = await screen_for(message, state)
    await screen.show(HELP_TEXT, reply_markup=main_menu_kb())


@router.callback_query(MenuAction.filter())
async def on_menu_action(
    query: CallbackQuery,
    callback_data: MenuAction,
    state: FSMContext,
    db: aiosqlite.Connection,
    registry: AdapterRegistry,
) -> None:
    """Deliberately registered without a state filter, so «☰ Меню» works from
    any step of any wizard. It drops the in-flight wizard without asking for
    confirmation: nothing is written to the DB until the wizard's last step,
    so there is nothing to lose, and a "точно выйти?" prompt on every tap
    would be pure friction.

    A flow boundary keeps the same screen rather than starting a fresh message:
    the bot owns exactly one evolving message per chat, so /search → ☰ → /add
    rewrites what is already on screen instead of stacking three of them.
    """
    assert query.from_user is not None
    screen = await screen_for(query, state)
    await state.clear()

    if callback_data.action == "add":
        await add.start_wizard(screen, state)
    elif callback_data.action == "search":
        await search.start_wizard(screen, state)
    elif callback_data.action == "routes":
        await routes.render_routes(screen, query.from_user.id, db)
    elif callback_data.action == "status":
        await status.render_status(screen, db, registry, is_admin=query.from_user.id == settings.admin_tg_id)
    elif callback_data.action == "help":
        await screen.show(HELP_TEXT, reply_markup=main_menu_kb())
    else:
        await show_menu(screen)
    await query.answer()


@fallback_router.message()
async def on_unmatched_message(message: Message, state: FSMContext) -> None:
    """Registered last (see `fallback_router`'s own docstring above) — catches whatever no
    wizard step and no command claimed. Same screen anchor as every other reply (`screen_for`
    re-anchors on the stored message if one exists), so this doesn't spray a new message into
    the chat on top of whatever was already on screen."""
    await state.clear()
    await (await screen_for(message, state)).show(_UNMATCHED_TEXT, reply_markup=main_menu_kb())
