import pytest

from aviapars.adapters.base import Mode
from aviapars.domain.transfers import _CROSS_MODE, _SAME_MODE, transfer_policy


def test_transfer_policy_same_mode():
    policy1 = transfer_policy(Mode.PLANE, Mode.PLANE)
    assert policy1 == _SAME_MODE
    assert policy1.min_transfer_min == 90
    assert policy1.transfer_cost_minor == 0

    policy2 = transfer_policy(Mode.TRAIN, Mode.TRAIN)
    assert policy2 == _SAME_MODE


def test_transfer_policy_cross_mode():
    policy1 = transfer_policy(Mode.PLANE, Mode.TRAIN)
    assert policy1 == _CROSS_MODE
    assert policy1.min_transfer_min == 240
    assert policy1.transfer_cost_minor == 60000

    policy2 = transfer_policy(Mode.TRAIN, Mode.PLANE)
    assert policy2 == _CROSS_MODE
