from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddRoute(StatesGroup):
    """§10 /add wizard: origin, destination, mode, dates (exact / ±3 дня /
    диапазон / любая), threshold, poll interval, notification mode. Hub-routing
    filters (max_hops/max_transfer_min/max_total_hours) are still Phase D/E
    (§13) and keep their schema defaults."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    mode = State()
    date_mode = State()
    date_value = State()
    threshold = State()
    interval = State()
    notify_mode = State()
    confirm = State()


class SearchFlow(StatesGroup):
    """§10 /search — one-shot interactive search, no route saved unless the
    user taps "Отслеживать". Same step order as AddRoute up to the dates
    (origin, destination, mode), then straight to results — no threshold,
    interval or notification mode, since nothing is being tracked. No L0/L1
    cache-then-live split and no manual leg picker yet (§8/§13 Phase D/E)."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    mode = State()
    date_mode = State()
    date_value = State()
    results = State()
