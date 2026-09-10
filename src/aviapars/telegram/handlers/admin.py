from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from aviapars.config import settings
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_sessions import SessionsRepo

router = Router(name="admin")

# `cfidsw-s7-ibe` (the antibot trust cookie, per live capture) carried a ~1-year
# Expires header — 180 days is a conservative fraction of that. Doesn't matter much
# either way: s7_direct falls back to BLOCKED and the admin gets asked to refresh
# well before this TTL is the actual limiting factor in practice.
S7_SESSION_TTL_S = 60 * 60 * 24 * 180

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
async def cmd_stop(message: Message, shutdown_event: asyncio.Event) -> None:
    """Signals __main__.main() to exit cleanly (0) — the systemd unit uses
    Restart=on-failure (not 'always'), so this stays stopped until the admin
    runs `systemctl start` over Tailscale/SSH. See deploy/INSTALL.md.

    Setting an Event rather than calling sys.exit() directly matters: exit()
    from inside a handler coroutine raises SystemExit inside whatever asyncio
    task aiogram is running it on, not in main()'s own task — it unwinds
    through the event loop instead of through main()'s try/finally, so
    bot.session.close() and the scheduler/watchdog task cancellation only ran
    incidentally, by accident of how the interpreter happened to unwind.
    """
    await message.answer("Останавливаюсь. Чтобы вернуть в строй: `systemctl start aviapars` на VDS.")
    shutdown_event.set()


@router.message(Command("pause_all"))
@admin_only
async def cmd_pause_all(message: Message, db: aiosqlite.Connection) -> None:
    """Softer than /stop — stops polling without killing the process, so
    the bot stays responsive to commands (§11)."""
    routes_repo = RoutesRepo(db)
    paused_count = await routes_repo.pause_all()
    await message.answer(f"Поставил на паузу {paused_count} маршрут(ов).")


def _parse_cookie_payload(text: str) -> dict[str, str]:
    """Accepts a Cookie-Editor-style JSON export (list of {"name", "value", ...}
    objects), a plain {"name": "value"} JSON object, or a raw `a=b; c=d` Cookie
    header string — whatever's fastest for the admin to copy out of devtools."""
    try:
        data = json.loads(text)
    except ValueError:
        cookies: dict[str, str] = {}
        for part in text.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                cookies[name.strip()] = value.strip()
        return cookies

    if isinstance(data, list):
        return {c["name"]: c["value"] for c in data if "name" in c and "value" in c}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


@router.message(Command("s7_session"))
@admin_only
async def cmd_s7_session(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    """Currently inert: `s7_direct` is disabled (`settings.s7_enabled = False`, see
    ARCHITECTURE.md "S7") because cookie replay alone doesn't work — confirmed live
    that even a real human browser's fresh cookies get BLOCKED when replayed through a
    different HTTP client, since ibe.s7.ru signs each request with per-call headers a
    saved cookie jar can't reproduce. Command left in place for if S7 is ever re-enabled
    through a legitimate path; saving a session here has no effect while it's off."""
    payload = (command.args or "").strip()
    if not payload:
        await message.answer(
            "Пришли куки следом за командой — JSON-экспорт (Cookie-Editor) или строку `a=b; c=d`."
        )
        return

    cookies = _parse_cookie_payload(payload)
    if not cookies:
        await message.answer("Не смог распарсить — ни JSON, ни `a=b; c=d` не подошли.")
        return

    sessions_repo = SessionsRepo(db)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=S7_SESSION_TTL_S)
    await sessions_repo.save("s7_direct", {"cookies": cookies}, expires_at)
    await message.answer(f"Сохранил {len(cookies)} кук для S7 — годны до {expires_at.date().isoformat()}.")
