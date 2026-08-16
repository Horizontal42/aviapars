from __future__ import annotations

from datetime import datetime

import aiosqlite

from aviapars.adapters.circuit import CircuitState, utcnow


class HealthRepo:
    """Persists CircuitState (adapters/circuit.py) into source_health (§4) so
    circuit-breaker state survives a restart instead of resetting every time.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get_state(self, source_id: str) -> CircuitState:
        async with self._db.execute(
            "SELECT status, consecutive_failures, circuit_open_until FROM source_health WHERE source_id = ?",
            (source_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return CircuitState()
        status, failures, open_until = row
        return CircuitState(
            status=status,
            consecutive_failures=failures,
            circuit_open_until=datetime.fromisoformat(open_until) if open_until else None,
        )

    async def save_state(self, source_id: str, state: CircuitState, *, error_kind: str | None = None, error_msg: str | None = None) -> None:
        now = utcnow().isoformat()
        success = state.status == "ok"
        await self._db.execute(
            """
            INSERT INTO source_health (source_id, status, consecutive_failures, circuit_open_until,
                                        last_success_at, last_error_at, last_error_kind, last_error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                status = excluded.status,
                consecutive_failures = excluded.consecutive_failures,
                circuit_open_until = excluded.circuit_open_until,
                last_success_at = COALESCE(excluded.last_success_at, source_health.last_success_at),
                last_error_at = COALESCE(excluded.last_error_at, source_health.last_error_at),
                last_error_kind = excluded.last_error_kind,
                last_error_msg = excluded.last_error_msg
            """,
            (
                source_id,
                state.status,
                state.consecutive_failures,
                state.circuit_open_until.isoformat() if state.circuit_open_until else None,
                now if success else None,
                None if success else now,
                error_kind,
                error_msg,
            ),
        )
        await self._db.commit()
