from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from aviapars.adapters.base import Mode
from aviapars.adapters.registry import AdapterRegistry
from aviapars.config import settings
from aviapars.db.repo_jobs import JobsRepo
from aviapars.telegram.format import describe_age, screen_text
from aviapars.telegram.keyboards import MODE_LABEL, menu_only_kb
from aviapars.telegram.screen import Screen, screen_for

router = Router(name="status")

TITLE = "📊 Источники"

_STATUS_EMOJI = {"ok": "✅", "degraded": "⚠️", "down": "❌"}


@router.message(Command("status"))
async def cmd_status(
    message: Message, state: FSMContext, db: aiosqlite.Connection, registry: AdapterRegistry
) -> None:
    assert message.from_user is not None
    is_admin = message.from_user.id == settings.admin_tg_id
    await render_status(await screen_for(message, state), db, registry, is_admin=is_admin)


async def render_status(
    screen: Screen, db: aiosqlite.Connection, registry: AdapterRegistry, *, is_admin: bool = False
) -> None:
    now = datetime.now(timezone.utc)
    async with db.execute(
        "SELECT source_id, status, last_success_at, consecutive_failures, last_error_kind FROM source_health"
    ) as cursor:
        rows = {row[0]: row for row in await cursor.fetchall()}

    if not is_admin:
        await _render_simple(screen, registry, rows)
        return

    lines = []
    for adapter in registry.all():
        row = rows.get(adapter.id)
        if row is None:
            lines.append(f"⚪ {adapter.display_name} — ещё не опрашивал")
            continue
        _, status, last_success, failures, last_error_kind = row
        emoji = _STATUS_EMOJI.get(status, "❓")
        moment = _parse_moment(last_success)
        # A source can have an old last_success_at from before it started failing
        # (e.g. banned mid-project) — showing "отвечал N назад" for a currently
        # "down"/"degraded" source read as "it's fine" next to the ❌/⚠️ emoji and
        # buried the fact that every recent attempt has failed. Failures currently
        # in progress take priority over a stale success timestamp, and the reason
        # (last_error_kind, already written by the registry but never shown before)
        # is what actually lets the reader tell "blocked" apart from "rate_limited"
        # apart from "timeout" instead of just a bare number.
        if failures > 0:
            detail = f"ошибок подряд: {failures}"
            if last_error_kind:
                detail += f" ({last_error_kind})"
            if moment:
                detail += f" · отвечал {describe_age(moment, now)}"
        elif moment:
            detail = f"отвечал {describe_age(moment, now)}"
        else:
            detail = f"ошибок подряд: {failures}"
        lines.append(f"{emoji} {adapter.display_name} — {detail}")

    due_jobs = await JobsRepo(db).due(now=now, limit=100)
    await screen.show(
        screen_text(TITLE, "\n".join(lines), f"Уведомлений в очереди: {len(due_jobs)}."),
        reply_markup=menu_only_kb(),
    )


async def _render_simple(screen: Screen, registry: AdapterRegistry, rows: dict[str, Any]) -> None:
    """Non-admin view (§11-adjacent, but a soft degrade rather than admin_only's
    silent no-op — everyone's allowed to know "is search working right now",
    just not the adapter-level internals that only mean something to whoever
    can actually go fix a source: no adapter names, no error_kind, no raw
    failure counts). Grouped by transport, matching what a user actually
    searches for (mode_picker_kb) rather than by internal adapter id."""
    lines = []
    for mode in Mode:
        adapters = [a for a in registry.all() if mode in a.modes]
        if not adapters:
            continue
        working = sum(1 for a in adapters if (rows.get(a.id) or (None, None))[1] == "ok")
        emoji = "✅" if working == len(adapters) else ("⚠️" if working > 0 else "❌")
        lines.append(f"{emoji} {MODE_LABEL.get(mode.value, mode.value)}: {working} из {len(adapters)} источников отвечают.")
    lines.append("")
    lines.append("Если источник недоступен — ищу через остальные, ничего делать не нужно.")
    await screen.show(screen_text(TITLE, "\n".join(lines)), reply_markup=menu_only_kb())


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
