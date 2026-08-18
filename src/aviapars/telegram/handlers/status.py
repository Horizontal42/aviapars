from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.repo_jobs import JobsRepo
from aviapars.telegram.format import describe_age, screen_text
from aviapars.telegram.keyboards import menu_only_kb
from aviapars.telegram.screen import Screen, screen_for

router = Router(name="status")

TITLE = "📊 Источники"

_STATUS_EMOJI = {"ok": "✅", "degraded": "⚠️", "down": "❌"}


@router.message(Command("status"))
async def cmd_status(
    message: Message, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    await render_status(await screen_for(message, state), db, registry)


async def render_status(screen: Screen, db: aiosqlite.Connection, registry: AdapterRegistry) -> None:
    now = datetime.now(timezone.utc)
    lines = []
    async with db.execute(
        "SELECT source_id, status, last_success_at, consecutive_failures FROM source_health"
    ) as cursor:
        rows = {row[0]: row for row in await cursor.fetchall()}

    for adapter in registry.all():
        row = rows.get(adapter.id)
        if row is None:
            lines.append(f"⚪ {adapter.display_name} — ещё не опрашивал")
            continue
        _, status, last_success, failures = row
        emoji = _STATUS_EMOJI.get(status, "❓")
        moment = _parse_moment(last_success)
        detail = f"отвечал {describe_age(moment, now)}" if moment else f"ошибок подряд: {failures}"
        lines.append(f"{emoji} {adapter.display_name} — {detail}")

    due_jobs = await JobsRepo(db).due(now=now, limit=100)
    await screen.show(
        screen_text(TITLE, "\n".join(lines), f"Уведомлений в очереди: {len(due_jobs)}."),
        reply_markup=menu_only_kb(),
    )


def _parse_moment(value: object) -> datetime | None:
    """`source_health.last_success_at` is written as an ISO string; anything the
    column can hold that isn't one just means "no known last success"."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
