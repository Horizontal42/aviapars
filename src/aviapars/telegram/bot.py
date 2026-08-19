from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, TelegramObject

from aviapars.config import settings
from aviapars.db.repo_users import UsersRepo
from aviapars.telegram.handlers import add, admin, menu, routes, search, start, status

_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="add", description="Добавить маршрут"),
    BotCommand(command="routes", description="Мои маршруты"),
    BotCommand(command="search", description="Разовый поиск"),
    BotCommand(command="status", description="Состояние источников"),
    BotCommand(command="help", description="Что я умею"),
]


class _EnsureUserMiddleware(BaseMiddleware):
    """Upserts the `users` row for whoever sent this update, before any handler runs.

    Without this, any entry point that skips /start (menu button, typing /add
    directly — Telegram's ☰ command list works without ever pressing start)
    hits routes.user_id/search_sessions.user_id's FK to users(tg_user_id) at
    the very last step of a wizard and dies with an unhandled IntegrityError,
    silently, after the user already answered every prompt.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        db = data.get("db")
        if user is not None and isinstance(db, aiosqlite.Connection):
            await UsersRepo(db).get_or_create(user.id, is_admin=user.id == settings.admin_tg_id)
        return await handler(event, data)


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
    dp.update.outer_middleware(_EnsureUserMiddleware())
    # menu first: its callback handler carries no state filter, so it has to be
    # reachable from inside every wizard step (see handlers/menu.py).
    dp.include_router(menu.router)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(add.router)
    dp.include_router(search.router)
    dp.include_router(routes.router)
    dp.include_router(status.router)
    # Last on purpose — see menu.fallback_router's own docstring: every state-specific
    # handler above gets first refusal, this only fires once nothing else claimed the update.
    dp.include_router(menu.fallback_router)
    return dp
