from __future__ import annotations

from dataclasses import dataclass

from aviapars.adapters.base import Mode


@dataclass(frozen=True, slots=True)
class TransferPolicy:
    min_transfer_min: int
    transfer_cost_minor: int


# Global-default tier only (§8's three-tier fallback is hub_place_id -> country -> global
# default; nothing at the more specific tiers is seeded yet, so every hub uses this today).
# Same-mode assumes a same-terminal-ish connection (plane-plane, train-train); cross-mode
# assumes an inter-city transfer in Moscow specifically (Aeroexpress + metro between an
# airport and a rail terminal), since Moscow is the only hub this project realistically
# routes through right now. Revisit once a second country/hub city is in real use.
_SAME_MODE = TransferPolicy(min_transfer_min=90, transfer_cost_minor=0)
_CROSS_MODE = TransferPolicy(min_transfer_min=240, transfer_cost_minor=60000)  # 600 RUB in kopecks


def transfer_policy(mode_a: Mode, mode_b: Mode) -> TransferPolicy:
    return _SAME_MODE if mode_a == mode_b else _CROSS_MODE
