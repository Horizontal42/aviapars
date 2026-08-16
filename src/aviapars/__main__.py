from __future__ import annotations

import asyncio
import logging

from aviapars.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aviapars")


async def main() -> None:
    log.info("aviapars starting up (db=%s)", settings.db_path)
    # Phase A wiring lands here: db init/migrate, bot polling task, scheduler tick task.
    raise NotImplementedError("Phase A not yet implemented — see plans/squishy-scribbling-dawn.md")


if __name__ == "__main__":
    asyncio.run(main())
