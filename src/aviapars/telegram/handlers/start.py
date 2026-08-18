from __future__ import annotations

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from aviapars.config import settings
from aviapars.db.repo_users import UsersRepo
from aviapars.telegram.keyboards import main_menu_kb
from aviapars.telegram.screen import screen_for

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    screen = await screen_for(message, state)
    await state.clear()
    users_repo = UsersRepo(db)
    is_admin = message.from_user.id == settings.admin_tg_id
    await users_repo.get_or_create(message.from_user.id, is_admin=is_admin)
    await screen.show(
        "Привет! Я слежу за ценами на авиа- и жд-билеты.\n\nВыберите, что нужно:",
        reply_markup=main_menu_kb(),
    )
