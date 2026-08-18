from __future__ import annotations

from typing import Any

from aviapars.adapters.base import Mode

_ALL_MODES = frozenset(Mode)


def modes_for_pref(pref: str) -> frozenset[Mode]:
    """Single indirection point for a `mode_pref` value (see §4 schema note).

    Today it is a single value ('plane'|'train'|'auto'); every other piece of
    code asks this function rather than interpreting the string itself. When
    mode_pref becomes a JSON array (to support e.g. "plane or bus, not
    train"), this is the only function that needs to change.

    Takes the bare value, not a route, because `/search` picks a mode the same
    way `/add` does but has no route row to read it off — the choice lives in
    FSM state until (and unless) the user taps «Отслеживать».
    """
    if pref == "auto":
        return _ALL_MODES
    return frozenset({Mode(pref)})


def allowed_modes(route: Any) -> frozenset[Mode]:
    return modes_for_pref(route.mode_pref)
