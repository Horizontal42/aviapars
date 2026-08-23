from __future__ import annotations

import re

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aviapars.db.models import User
from aviapars.db.repo_users import UsersRepo
from aviapars.telegram.format import screen_text
from aviapars.telegram.keyboards import SettingsAction, menu_only_kb, settings_kb
from aviapars.telegram.screen import Screen, screen_for
from aviapars.telegram.states import EditSettings

router = Router(name="settings")

TITLE = "⚙️ Настройки"

_HHMM = re.compile(r"^([01]?\d|2[0-3])[:.\s]?([0-5]\d)$")
_RANGE_SEPARATORS = re.compile(r"\s*(?:-|—|–|до|,)\s*")
_RESET_WORDS = {"-", "—", "авто", "сброс", "по умолчанию", "как тихие часы"}


def describe_settings(user: User) -> str:
    quiet = f"{user.quiet_hours_start}–{user.quiet_hours_end}"
    digest = f"{user.digest_time}" if user.digest_time else f"{user.quiet_hours_end} (конец тихих часов)"
    return "\n".join(
        [
            f"🌙 Тихие часы: {quiet} ({user.tz})",
            "В это время я молчу — всё найденное придерживаю до конца окна.",
            "",
            f"📬 Дайджест приходит в {digest}",
            "Одним сообщением за раз, все находки суток подряд. Касается только маршрутов "
            "в режиме «раз в день»; «сразу» пишет по каждой находке отдельно.",
        ]
    )


async def render_settings(screen: Screen, user_id: int, db: aiosqlite.Connection, *, note: str = "") -> None:
    user = await UsersRepo(db).get(user_id)
    if user is None:
        await screen.show(screen_text(TITLE, "⚠️ Не нашёл ваш профиль. Напишите /start."))
        return
    await screen.show(screen_text(TITLE, note, describe_settings(user)), reply_markup=settings_kb())


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    await state.clear()
    await render_settings(await screen_for(message, state), message.from_user.id, db)


@router.callback_query(SettingsAction.filter())
async def on_settings_action(
    query: CallbackQuery, callback_data: SettingsAction, state: FSMContext, db: aiosqlite.Connection
) -> None:
    assert query.from_user is not None
    screen = await screen_for(query, state)
    await query.answer()

    if callback_data.action == "quiet":
        await state.set_state(EditSettings.quiet_hours)
        await screen.show(
            screen_text(
                "🌙 Тихие часы",
                "Напишите начало и конец через дефис, например <code>23:00-08:00</code>.\n"
                "Чтобы отключить тишину совсем — одинаковое время с обеих сторон: <code>00:00-00:00</code>.",
            ),
            reply_markup=menu_only_kb(),
        )
        return
    if callback_data.action == "digest":
        await state.set_state(EditSettings.digest_time)
        await screen.show(
            screen_text(
                "📬 Время дайджеста",
                "Напишите время в вашем часовом поясе, например <code>09:30</code>.\n"
                "«<code>-</code>» вернёт как было: дайджест придёт в конце тихих часов.\n\n"
                "Если названное время попадёт внутрь тихих часов, отправлю по их окончании — "
                "тишина важнее.",
            ),
            reply_markup=menu_only_kb(),
        )
        return
    await render_settings(screen, query.from_user.id, db)


@router.message(EditSettings.quiet_hours)
async def on_quiet_hours_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    screen = await screen_for(message, state)
    parsed = _parse_range(message.text)
    if parsed is None:
        await screen.show(
            screen_text("🌙 Тихие часы", "⚠️ Не разобрал. Нужны два времени, например <code>23:00-08:00</code>."),
            reply_markup=menu_only_kb(),
        )
        return
    start, end = parsed
    await UsersRepo(db).set_quiet_hours(message.from_user.id, start=start, end=end)
    await state.clear()
    await render_settings(screen, message.from_user.id, db, note=f"✅ Тихие часы теперь {start}–{end}.")


@router.message(EditSettings.digest_time)
async def on_digest_time_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    assert message.from_user is not None
    screen = await screen_for(message, state)
    raw = (message.text or "").strip().lower()
    if raw in _RESET_WORDS:
        await UsersRepo(db).set_digest_time(message.from_user.id, None)
        await state.clear()
        await render_settings(screen, message.from_user.id, db, note="✅ Дайджест снова по концу тихих часов.")
        return
    value = _parse_time(message.text)
    if value is None:
        await screen.show(
            screen_text("📬 Время дайджеста", "⚠️ Не разобрал. Нужно время, например <code>09:30</code>."),
            reply_markup=menu_only_kb(),
        )
        return
    await UsersRepo(db).set_digest_time(message.from_user.id, value)
    await state.clear()
    await render_settings(screen, message.from_user.id, db, note=f"✅ Дайджест буду присылать в {value}.")


def _parse_time(raw: str | None) -> str | None:
    """«9:30», «09.30», «0930» — one field, typed on a phone. Returns canonical HH:MM."""
    if raw is None:
        return None
    m = _HHMM.match(raw.strip())
    if m is None:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_range(raw: str | None) -> tuple[str, str] | None:
    if raw is None:
        return None
    parts = [p for p in _RANGE_SEPARATORS.split(raw.strip()) if p]
    if len(parts) != 2:
        return None
    start, end = _parse_time(parts[0]), _parse_time(parts[1])
    if start is None or end is None:
        return None
    return start, end
