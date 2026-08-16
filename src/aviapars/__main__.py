from __future__ import annotations

import asyncio
import logging

from aviapars.adapters.registry import AdapterRegistry
from aviapars.adapters.rzd_direct import RzdDirectAdapter
from aviapars.adapters.travelpayouts import TravelpayoutsAdapter
from aviapars.adapters.yandex_rasp import YandexRaspAdapter
from aviapars.config import settings
from aviapars.db.engine import connect
from aviapars.db.migrate import migrate
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.tick import run_scheduler_loop
from aviapars.telegram.bot import create_bot, create_dispatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aviapars")


async def main() -> None:
    log.info("aviapars starting up (db=%s)", settings.db_path)
    await migrate()

    async with connect() as db:
        registry = AdapterRegistry(
            db,
            http_concurrency_global=settings.http_concurrency_global,
            http_concurrency_interactive=settings.http_concurrency_interactive,
            http_concurrency_per_source=settings.http_concurrency_per_source,
        )
        registry.register(
            TravelpayoutsAdapter(
                token=settings.travelpayouts_token,
                marker=settings.travelpayouts_marker,
                cache_ttl_s=settings.cache_ttl_s.travelpayouts,
            )
        )
        registry.register(
            YandexRaspAdapter(
                api_key=settings.yandex_rasp_api_key,
                cache_ttl_s=settings.cache_ttl_s.yandex_rasp_price,
            )
        )
        registry.register(RzdDirectAdapter(cache_ttl_s=settings.cache_ttl_s.rzd_direct))

        bot = create_bot(settings.telegram_bot_token)
        dp = create_dispatcher()

        notifications_repo = NotificationsRepo(db)
        jobs_repo = JobsRepo(db)
        dedupe = DedupeGate(notifications_repo)
        notifier = Notifier(
            bot,
            notifications_repo,
            jobs_repo,
            dedupe,
            cooldown_h=settings.notification_dedupe_cooldown_h,
            daily_cap=settings.notifications_per_route_per_day,
        )

        scheduler_task = asyncio.create_task(run_scheduler_loop(db, registry, notifier))
        try:
            await dp.start_polling(bot, db=db, registry=registry, notifier=notifier)
        finally:
            scheduler_task.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
