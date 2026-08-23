from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddRoute(StatesGroup):
    """§10 /add wizard: origin, destination, mode, dates (exact / ±3 дня /
    диапазон / любая), threshold, poll interval, notification mode. Hub-routing
    filters (max_hops/max_transfer_min/max_total_hours) are still Phase D/E
    (§13) and keep their schema defaults.

    The threshold step is one state for a single-mode route and three for a
    'auto' one, which first gets asked whether it wants one shared ceiling or a
    separate one per transport (§9) — a plane/train route never sees the extra
    steps at all."""

    origin = State()
    origin_pick = State()
    destination = State()
    destination_pick = State()
    mode = State()
    date_mode = State()
    date_value = State()
    threshold_scope = State()
    threshold = State()
    threshold_plane = State()
    threshold_train = State()
    interval = State()
    notify_mode = State()
    confirm = State()


class EditRoute(StatesGroup):
    """Post-creation edits from a route card (§10) — currently just the poll
    interval. A separate tiny group rather than reusing AddRoute.interval:
    that state means "finishing wizard creation", this one means "editing an
    existing route", and the two need different handlers on the same text
    (the wizard's continues into the notify-mode step; this one just saves
    and re-renders the card)."""

    interval = State()


class EditSettings(StatesGroup):
    """§9 delivery preferences, typed as free text: when to stay quiet, and when
    the daily digest should land. Separate states because the two parse
    differently — a range against a single time."""

    quiet_hours = State()
    digest_time = State()


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
