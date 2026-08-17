"""Minimal sd_notify client — no `sdnotify`/`cysystemd` dependency needed for a
one-datagram protocol. No-ops outside systemd (NOTIFY_SOCKET unset), which is the
common case in tests/dev — never raises.

`aviapars.service` is `Type=notify` with `WatchdogSec=300`: without READY=1 the
unit stays stuck in "activating" until systemd's start timeout kills it (looping
forever under Restart=on-failure even though the bot itself is fine); without
periodic WATCHDOG=1 pings, the same timeout kills an otherwise-healthy process
every WatchdogSec. Both are real failure modes this module exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import socket


def notify(state: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except (OSError, AttributeError):
        # AttributeError: AF_UNIX doesn't exist on this platform at all (e.g. dev/test
        # on Windows) — same "best-effort, never worth crashing startup over" as OSError.
        pass


async def watchdog_loop(interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        notify("WATCHDOG=1")
