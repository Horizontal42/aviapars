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


class SearchFlow(StatesGroup):
    """§10 /search — one-shot interactive search, no route saved unless the
    user taps "Отслеживать". Current scope (one adapter, direct offers only):
    single synchronous pass, no L0/L1 cache-then-live split, no hub/mixed
    results, no manual leg picker — those need more than one leg-capable
    source to mean anything (§8/§13 Phase D/E)."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    date = State()
    results = State()
