from __future__ import annotations

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from aviapars.config import settings
from aviapars.db.repo_users import UsersRepo

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    users_repo = UsersRepo(db)
    is_admin = message.from_user.id == settings.admin_tg_id
    await users_repo.get_or_create(message.from_user.id, is_admin=is_admin)
    await message.answer(
        "Привет! Я слежу за ценами на авиа- и жд-билеты.\n\n"
        "/add — добавить маршрут для отслеживания\n"
        "/routes — список маршрутов\n"
        "/help — что я умею"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/add — добавить маршрут (откуда, куда, режим, дата, порог цены)\n"
        "/routes — список маршрутов: пауза, проверить сейчас, удалить\n\n"
        "Уведомление придёт, только когда цена окажется ниже заданного порога. "
        "Источники цен — кэш агрегаторов (может быть неточно свежим) плюс "
        "проверка напрямую у перевозчиков там, где это подключено."
    )
