from __future__ import annotations

import sys
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from aviapars.config import settings
from aviapars.db.repo_routes import RoutesRepo

router = Router(name="admin")

F = TypeVar("F", bound=Callable[..., Awaitable[None]])


def admin_only(handler: F) -> F:
    """§11: only ADMIN_TG_ID may run these — silently no-ops for everyone
    else (no error message revealing the command exists), enforced in the
    handler layer so it can't be bypassed by typing the command directly."""

    @wraps(handler)
    async def wrapper(message: Message, *args: Any, **kwargs: Any) -> None:
        if message.from_user is None or message.from_user.id != settings.admin_tg_id:
            return
        await handler(message, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


@router.message(Command("stop"))
@admin_only
async def cmd_stop(message: Message) -> None:
    """Clean exit(0) — the systemd unit uses Restart=on-failure (not
    'always'), so this stays stopped until the admin runs `systemctl start`
    over Tailscale/SSH. See deploy/INSTALL.md."""
    await message.answer("Останавливаюсь. Чтобы вернуть в строй: `systemctl start aviapars` на VDS.")
    sys.exit(0)


@router.message(Command("pause_all"))
@admin_only
async def cmd_pause_all(message: Message, db: aiosqlite.Connection) -> None:
    """Softer than /stop — stops polling without killing the process, so
    the bot stays responsive to commands (§11)."""
    async with db.execute("SELECT id FROM routes WHERE active = 1") as cursor:
        rows = list(await cursor.fetchall())
    routes_repo = RoutesRepo(db)
    for row in rows:
        await routes_repo.set_active(row[0], False)
    await message.answer(f"Поставил на паузу {len(rows)} маршрут(ов).")
