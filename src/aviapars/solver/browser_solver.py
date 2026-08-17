"""Standalone entry point: `python -m aviapars.solver.browser_solver <target> k=v k=v ...`

Runs as its own OS process (spawned by BrowserSolverClient, never imported into the
bot's event loop) so patchright/chromium's ~300-400MB footprint is contained to one
short-lived subprocess rather than the bot's own memory. Solves a JS-challenge bootstrap
ONLY — never reads flight/price content itself. Prints one JSON object to stdout and
exits 0 on success; prints {"error": ...} and exits 1 on any failure. Never raises past
main() — a stack trace on stdout would poison the caller's JSON parse.

patchright is an optional dependency (`pip install aviapars[solver]`) — importing this
module must not fail when it isn't installed; only running it should.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

_TARGETS: dict[str, dict[str, Any]] = {
    "s7": {
        "url": "https://ibe.s7.ru/air",
        "timeout_ms": 45000,
        "exec_pattern": re.compile(r'__r_execution\s*=\s*"([^"]+)"'),
        "conv_pattern": re.compile(r'__r_ibe_conversation\s*=\s*"([^"]+)"'),
    },
}


async def solve(target: str, params: dict[str, str]) -> dict[str, Any] | None:
    cfg = _TARGETS.get(target)
    if cfg is None:
        return None

    try:
        from patchright.async_api import async_playwright  # type: ignore[import-not-found]  # optional dep, see module docstring
    except ImportError:
        return None

    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{cfg['url']}?{query}" if query else cfg["url"]
    timeout_ms = cfg["timeout_ms"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-gpu", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout_ms / 1000
            execution: str | None = None
            conversation: str | None = None
            while loop.time() < deadline:
                content = await page.content()
                m_exec = cfg["exec_pattern"].search(content)
                m_conv = cfg["conv_pattern"].search(content)
                if m_exec and m_conv:
                    execution, conversation = m_exec.group(1), m_conv.group(1)
                    break
                await page.wait_for_timeout(1000)

            if execution is None or conversation is None:
                return None

            cookies = await page.context.cookies()
            user_agent = await page.evaluate("navigator.userAgent")
            return {
                "execution": execution,
                "ibe_conversation": conversation,
                "cookies": {c["name"]: c["value"] for c in cookies},
                "user_agent": user_agent,
            }
        finally:
            await browser.close()


async def _main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: browser_solver.py <target> [k=v ...]"}))
        return 1
    target = sys.argv[1]
    params = dict(p.split("=", 1) for p in sys.argv[2:] if "=" in p)

    try:
        result = await solve(target, params)
    except Exception as exc:  # never let a stack trace hit stdout — see module docstring
        print(json.dumps({"error": str(exc)}))
        return 1

    if result is None:
        print(json.dumps({"error": "solve failed or timed out"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
