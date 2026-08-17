from __future__ import annotations

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.telegram.handlers import add, routes, search, status
from aviapars.telegram.keyboards import MenuAction, main_menu_kb

router = Router(name="menu")

MENU_TEXT = "Главное меню — выберите, что нужно:"

HELP_TEXT = (
    "Что я умею:\n\n"
    "➕ <b>Добавить маршрут</b> — слежу за ценой в фоне и пишу, когда она упадёт ниже порога.\n"
    "📋 <b>Мои маршруты</b> — пауза, проверка прямо сейчас, удаление.\n"
    "🔎 <b>Разовый поиск</b> — показать варианты прямо сейчас, ничего не сохраняя.\n"
    "📊 <b>Состояние источников</b> — какие источники цен живы.\n\n"
    "Даты можно задавать не только точно: «±3 дня», «диапазон» и «любая дата» "
    "тоже поддерживаются — точную дату проверяю целиком, широкое окно фоновое "
    "отслеживание обходит по частям, каждый цикл новый кусок.\n\n"
    "Те же действия доступны командами: /add, /routes, /search, /status, /menu.\n\n"
    "Источники цен — кэш агрегаторов (может быть неточно свежим) плюс проверка "
    "напрямую у перевозчиков там, где это подключено."
)


async def show_menu(message: Message) -> None:
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_menu(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


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
    """
    await state.clear()
    assert query.message is not None
    assert query.from_user is not None
    message = query.message
    if not isinstance(message, Message):
        await query.answer()
        return

    if callback_data.action == "add":
        await add.start_wizard(message, state)
    elif callback_data.action == "search":
        await search.start_wizard(message, state)
    elif callback_data.action == "routes":
        await routes.render_routes(message, query.from_user.id, db)
    elif callback_data.action == "status":
        await status.render_status(message, db, registry)
    elif callback_data.action == "help":
        await message.answer(HELP_TEXT, reply_markup=main_menu_kb())
    else:
        await show_menu(message)
    await query.answer()
