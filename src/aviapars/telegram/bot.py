from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from aviapars.telegram.handlers import add, admin, routes, search, start, status


def create_bot(token: str, proxy_url: str | None = None) -> Bot:
    session = AiohttpSession(proxy=proxy_url) if proxy_url else None
    return Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(add.router)
    dp.include_router(search.router)
    dp.include_router(routes.router)
    dp.include_router(status.router)
    return dp
