from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


class BrowserSolverClient:
    """Spawns browser_solver.py as an isolated subprocess — never in-process — so a
    stuck/runaway browser can't block the bot's own event loop. Never raises: any
    failure (patchright not installed, timeout, challenge unsolved, bad JSON) returns
    None, and the caller (s7_direct) treats that exactly like "cheap path also failed".
    """

    def __init__(self, *, timeout_s: float = 90) -> None:
        self._timeout_s = timeout_s

    async def solve(self, target: str, params: dict[str, str]) -> dict[str, Any] | None:
        cmd = [sys.executable, "-m", "aviapars.solver.browser_solver"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            input_data = json.dumps({"target": target, "params": params}).encode()
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=input_data), timeout=self._timeout_s
            )
        except (TimeoutError, OSError):
            return None

        if proc.returncode != 0:
            return None
        try:
            result: dict[str, Any] = json.loads(stdout.decode())
        except ValueError:
            return None
        if "error" in result:
            return None
        return result
