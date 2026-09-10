import pytest
from datetime import datetime, timedelta, timezone
from aviapars.adapters.circuit import (
    CircuitState,
    is_open,
    record_success,
    record_failure,
    FAILURE_THRESHOLD,
    BACKOFF_STEPS_S,
    utcnow,
)

def test_circuit_state_initialization():
    state = CircuitState()
    assert state.status == "ok"
    assert state.consecutive_failures == 0
    assert state.circuit_open_until is None
    assert state.escalation == 0

def test_is_open():
    now = utcnow()
    # Default state
    assert not is_open(CircuitState(), now)

    # Circuit open until future
    future = now + timedelta(minutes=5)
    state_open = CircuitState(circuit_open_until=future)
    assert is_open(state_open, now)

    # Circuit open until past
    past = now - timedelta(minutes=5)
    state_closed = CircuitState(circuit_open_until=past)
    assert not is_open(state_closed, now)

def test_record_success():
    # Initial state is anything other than default
    state = CircuitState(
        status="down",
        consecutive_failures=5,
        circuit_open_until=utcnow() + timedelta(minutes=5),
        escalation=2
    )

    new_state = record_success(state)
    assert new_state.status == "ok"
    assert new_state.consecutive_failures == 0
    assert new_state.circuit_open_until is None
    assert new_state.escalation == 0

def test_record_failure_degraded():
    now = utcnow()
    state = CircuitState()

    # Failures less than threshold
    for i in range(1, FAILURE_THRESHOLD):
        state = record_failure(state, now)
        assert state.status == "degraded"
        assert state.consecutive_failures == i
        assert state.circuit_open_until is None
        assert state.escalation == 0

def test_record_failure_threshold_reached():
    now = utcnow()
    # State just before threshold
    state = CircuitState(consecutive_failures=FAILURE_THRESHOLD - 1)

    # Hit threshold
    new_state = record_failure(state, now)

    assert new_state.status == "down"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD
    assert new_state.escalation == 1

    expected_open_until = now + timedelta(seconds=BACKOFF_STEPS_S[0])
    assert new_state.circuit_open_until == expected_open_until

def test_record_failure_escalation_cap():
    now = utcnow()
    # Start with failure at threshold
    state = CircuitState(consecutive_failures=FAILURE_THRESHOLD - 1)

    # Iterate through all backoff steps and beyond to test capping
    # BACKOFF_STEPS_S has 3 items: index 0, 1, 2
    for i in range(len(BACKOFF_STEPS_S) + 2):
        state = record_failure(state, now)

        assert state.status == "down"
        assert state.consecutive_failures == FAILURE_THRESHOLD + i
        assert state.escalation == i + 1

        step = min(i, len(BACKOFF_STEPS_S) - 1)
        expected_open_until = now + timedelta(seconds=BACKOFF_STEPS_S[step])
        assert state.circuit_open_until == expected_open_until
