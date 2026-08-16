from __future__ import annotations

from typing import Any

from aviapars.adapters.base import Mode

_ALL_MODES = frozenset(Mode)


def allowed_modes(route: Any) -> frozenset[Mode]:
    """Single indirection point for `routes.mode_pref` (see §4 schema note).

    Today `mode_pref` is a single value ('plane'|'train'|'auto'); every other
    piece of code asks this function rather than reading the column directly.
    When mode_pref becomes a JSON array (to support e.g. "plane or bus, not
    train"), this is the only function that needs to change.
    """
    pref = route.mode_pref
    if pref == "auto":
        return _ALL_MODES
    return frozenset({Mode(pref)})
