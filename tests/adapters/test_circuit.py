from datetime import datetime, timedelta, timezone

from aviapars.adapters.circuit import (
    BACKOFF_STEPS_S,
    FAILURE_THRESHOLD,
    CircuitState,
    is_open,
    record_failure,
    record_success,
)


def test_is_open() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # circuit_open_until is None
    state = CircuitState(circuit_open_until=None)
    assert not is_open(state, now)

    # now is less than circuit_open_until
    state = CircuitState(circuit_open_until=now + timedelta(seconds=1))
    assert is_open(state, now)

    # now is greater than circuit_open_until
    state = CircuitState(circuit_open_until=now - timedelta(seconds=1))
    assert not is_open(state, now)

    # now is equal to circuit_open_until
    state = CircuitState(circuit_open_until=now)
    assert not is_open(state, now)


def test_record_success() -> None:
    # Various initial states
    states = [
        CircuitState(status="degraded", consecutive_failures=2, escalation=0),
        CircuitState(
            status="down",
            consecutive_failures=FAILURE_THRESHOLD,
            circuit_open_until=datetime.now(tz=timezone.utc),
            escalation=1,
        ),
        CircuitState(
            status="down",
            consecutive_failures=10,
            circuit_open_until=datetime.now(tz=timezone.utc),
            escalation=5,
        ),
    ]

    for state in states:
        new_state = record_success(state)
        assert new_state.status == "ok"
        assert new_state.consecutive_failures == 0
        assert new_state.circuit_open_until is None
        assert new_state.escalation == 0


def test_record_failure() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Below FAILURE_THRESHOLD
    state = CircuitState(status="ok", consecutive_failures=0)
    for i in range(1, FAILURE_THRESHOLD):
        state = record_failure(state, now)
        assert state.status == "degraded"
        assert state.consecutive_failures == i
        assert state.circuit_open_until is None
        assert state.escalation == 0

    # Exactly at FAILURE_THRESHOLD
    state = record_failure(state, now)
    assert state.status == "down"
    assert state.consecutive_failures == FAILURE_THRESHOLD
    assert state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[0])
    assert state.escalation == 1

    # Escalation within bounds
    state = CircuitState(
        status="down",
        consecutive_failures=FAILURE_THRESHOLD,
        circuit_open_until=now,
        escalation=1,
    )
    state = record_failure(state, now)
    assert state.status == "down"
    assert state.consecutive_failures == FAILURE_THRESHOLD + 1
    assert state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[1])
    assert state.escalation == 2

    state = record_failure(state, now)
    assert state.status == "down"
    assert state.consecutive_failures == FAILURE_THRESHOLD + 2
    assert state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[2])
    assert state.escalation == 3

    # Escalation exceeds bounds (should cap at max)
    state = CircuitState(
        status="down",
        consecutive_failures=10,
        circuit_open_until=now,
        escalation=5,
    )
    state = record_failure(state, now)
    assert state.status == "down"
    assert state.consecutive_failures == 11
    assert state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[-1])
    assert state.escalation == 6
