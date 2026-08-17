from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddRoute(StatesGroup):
    """§10 /add wizard: origin, destination, mode, dates (exact / ±3 дня /
    диапазон / любая), threshold, poll interval. Hub-routing filters
    (max_hops/max_transfer_min/max_total_hours) are still Phase D/E (§13) and
    keep their schema defaults."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    mode = State()
    date_mode = State()
    date_value = State()
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
    date_mode = State()
    date_value = State()
    results = State()
