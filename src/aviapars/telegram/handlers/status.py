from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.repo_jobs import JobsRepo
from aviapars.telegram.keyboards import menu_only_kb

router = Router(name="status")

_STATUS_EMOJI = {"ok": "✅", "degraded": "⚠️", "down": "❌"}


@router.message(Command("status"))
async def cmd_status(message: Message, db: aiosqlite.Connection, registry: AdapterRegistry) -> None:
    await render_status(message, db, registry)


async def render_status(message: Message, db: aiosqlite.Connection, registry: AdapterRegistry) -> None:
    lines = ["<b>Источники:</b>"]
    async with db.execute(
        "SELECT source_id, status, last_success_at, consecutive_failures FROM source_health"
    ) as cursor:
        rows = {row[0]: row for row in await cursor.fetchall()}

    for adapter in registry.all():
        row = rows.get(adapter.id)
        if row is None:
            lines.append(f"{_STATUS_EMOJI['ok']} {adapter.display_name} — ещё не опрашивался")
            continue
        _, status, last_success, failures = row
        emoji = _STATUS_EMOJI.get(status, "❓")
        detail = f"последний успех: {last_success}" if last_success else f"подряд ошибок: {failures}"
        lines.append(f"{emoji} {adapter.display_name} — {detail}")

    jobs_repo = JobsRepo(db)
    due_jobs = await jobs_repo.due(now=datetime.now(timezone.utc), limit=100)
    lines.append(f"\nОжидают обработки: {len(due_jobs)}")

    await message.answer("\n".join(lines), reply_markup=menu_only_kb())
