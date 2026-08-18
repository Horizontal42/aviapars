from __future__ import annotations

from datetime import date
from html import escape
from typing import Awaitable, Callable

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State

from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.telegram.format import describe_draft, screen_text
from aviapars.telegram.keyboards import menu_only_kb, place_picker_kb
from aviapars.telegram.screen import Screen


async def ask_place_pick(
    screen: Screen,
    state: FSMContext,
    db: aiosqlite.Connection,
    *,
    title: str,
    text: str,
    pick_state: State,
    storage_key: str,
    on_picked: Callable[[Place], Awaitable[None]],
) -> None:
    """Shared by /add and /search (§10: "reuses the exact same resolution
    widget"). Local `places` table lookup, sub-millisecond, no API call.

    `on_picked` is the very same continuation the pick callback runs, so a
    single match can be applied without duplicating "what happens after a
    choice" per wizard.
    """
    places = await PlacesRepo(db).search_by_name(text)
    # Whatever the user typed goes back onto an HTML-parsed screen, so it is escaped here
    # rather than trusted — a stray "<" would otherwise make Telegram reject the message.
    quoted = f"«{escape(text.strip(), quote=False)}»"
    data = await state.get_data()
    draft = describe_draft(data, date.today())
    if not places:
        await screen.show(
            screen_text(title, draft, f"⚠️ Ничего не нашёл по запросу {quoted}. Попробуйте другое название."),
            reply_markup=menu_only_kb(),
        )
        return
    if len(places) == 1:
        # A keyboard with one real option is not a choice, it is an extra tap: typing
        # «Санкт-Петербург» resolves to exactly one place, so it just goes through.
        await on_picked(places[0])
        return
    await state.update_data({storage_key: places})
    await state.set_state(pick_state)
    await screen.show(
        screen_text(title, draft, f"По запросу {quoted} нашлось несколько мест. Какое нужно?"),
        reply_markup=place_picker_kb(places),
    )


def resolve_pick(candidates: object, idx: int) -> Place | None:
    """The candidate list round-trips through FSM state, so a stale keyboard (bot
    restarted, or a screen scrolled back to) can point at nothing — that is a
    normal outcome to render, not an assertion to trip over."""
    if not isinstance(candidates, list) or not 0 <= idx < len(candidates):
        return None
    place = candidates[idx]
    return place if isinstance(place, Place) else None
