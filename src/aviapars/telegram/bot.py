from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from aviapars.telegram.handlers import add, admin, menu, routes, search, start, status

_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="add", description="Добавить маршрут"),
    BotCommand(command="routes", description="Мои маршруты"),
    BotCommand(command="search", description="Разовый поиск"),
    BotCommand(command="status", description="Состояние источников"),
    BotCommand(command="help", description="Что я умею"),
]


def create_bot(token: str, proxy_url: str | None = None) -> Bot:
    session = AiohttpSession(proxy=proxy_url) if proxy_url else None
    return Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def setup_commands(bot: Bot) -> None:
    """Populates the client's ☰ button. Commands stay as a parallel entry
    path — the inline menu is the primary one, but Telegram users expect the
    slash list to exist and some prefer typing."""
    await bot.set_my_commands(_COMMANDS)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # menu first: its callback handler carries no state filter, so it has to be
    # reachable from inside every wizard step (see handlers/menu.py).
    dp.include_router(menu.router)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(add.router)
    dp.include_router(search.router)
    dp.include_router(routes.router)
    dp.include_router(status.router)
    return dp
