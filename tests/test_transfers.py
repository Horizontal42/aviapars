import pytest

from aviapars.adapters.base import Mode
from aviapars.domain.transfers import _CROSS_MODE, _SAME_MODE, TransferPolicy, transfer_policy


@pytest.mark.parametrize(
    ("mode_a", "mode_b", "expected_policy"),
    [
        (Mode.PLANE, Mode.PLANE, _SAME_MODE),
        (Mode.TRAIN, Mode.TRAIN, _SAME_MODE),
        (Mode.PLANE, Mode.TRAIN, _CROSS_MODE),
        (Mode.TRAIN, Mode.PLANE, _CROSS_MODE),
    ],
)
def test_transfer_policy_modes(mode_a: Mode, mode_b: Mode, expected_policy: TransferPolicy) -> None:
    policy = transfer_policy(mode_a, mode_b)
    assert policy is expected_policy


def test_transfer_policy_same_mode_values() -> None:
    policy = transfer_policy(Mode.PLANE, Mode.PLANE)
    assert policy.min_transfer_min == 90
    assert policy.transfer_cost_minor == 0


def test_transfer_policy_cross_mode_values() -> None:
    policy = transfer_policy(Mode.PLANE, Mode.TRAIN)
    assert policy.min_transfer_min == 240
    assert policy.transfer_cost_minor == 60000
