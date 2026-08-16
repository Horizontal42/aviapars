from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddRoute(StatesGroup):
    """§10 /add wizard. Flexible dates and hub-routing filters are Phase D/E
    additions (§13) — this covers the walking-skeleton subset: origin,
    destination, mode, a single fixed date, threshold, poll interval."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    mode = State()
    date = State()
    threshold = State()
    interval = State()
    confirm = State()
