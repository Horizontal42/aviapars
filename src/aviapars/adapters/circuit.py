from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

FAILURE_THRESHOLD = 5
BACKOFF_STEPS_S = (15 * 60, 60 * 60, 4 * 60 * 60)  # 15min → 1h → 4h (§5)


@dataclass(frozen=True, slots=True)
class CircuitState:
    """Mirrors source_health columns (§4) — the registry persists this via a
    repo; this module only holds the pure transition logic so it's testable
    without a DB.
    """

    status: str = "ok"  # ok|degraded|down
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    escalation: int = 0  # how many times the circuit has re-opened in a row


def is_open(state: CircuitState, now: datetime) -> bool:
    return state.circuit_open_until is not None and now < state.circuit_open_until


def record_success(state: CircuitState) -> CircuitState:
    return CircuitState(status="ok", consecutive_failures=0, circuit_open_until=None, escalation=0)


def record_failure(state: CircuitState, now: datetime) -> CircuitState:
    failures = state.consecutive_failures + 1
    if failures < FAILURE_THRESHOLD:
        return replace(state, status="degraded", consecutive_failures=failures)

    step = min(state.escalation, len(BACKOFF_STEPS_S) - 1)
    open_until = now + timedelta(seconds=BACKOFF_STEPS_S[step])
    return CircuitState(
        status="down",
        consecutive_failures=failures,
        circuit_open_until=open_until,
        escalation=state.escalation + 1,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
