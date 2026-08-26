import asyncio
import time
from datetime import datetime, timezone, timedelta
import aiosqlite
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.models import Route

async def main():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("""
            CREATE TABLE routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                origin_place_id INTEGER NOT NULL,
                dest_place_id INTEGER NOT NULL,
                mode_pref TEXT NOT NULL,
                date_mode TEXT NOT NULL,
                fixed_dates TEXT,
                window_start_date TEXT,
                window_start_offset_days INTEGER,
                window_length_days INTEGER,
                date_cursor INTEGER,
                max_price INTEGER NOT NULL,
                max_price_currency TEXT NOT NULL,
                max_price_by_mode TEXT,
                pax INTEGER NOT NULL DEFAULT 1,
                allow_hub_routing INTEGER NOT NULL DEFAULT 1,
                hub_place_ids TEXT,
                max_hops INTEGER NOT NULL DEFAULT 1,
                max_transfer_min INTEGER,
                max_total_hours REAL,
                poll_interval_min INTEGER NOT NULL DEFAULT 60,
                notification_mode TEXT NOT NULL DEFAULT 'instant',
                active INTEGER NOT NULL DEFAULT 1,
                next_run_at TEXT NOT NULL,
                last_run_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()

        repo = RoutesRepo(db)
        # insert 100 routes
        routes = []
        for i in range(100):
            r = await repo.create(
                user_id=1, title="Test", origin_place_id=1, dest_place_id=2, mode_pref="any",
                date_mode="fixed", fixed_dates=["2023-01-01"], max_price=1000, max_price_currency="RUB"
            )
            routes.append(r)

        now = datetime.now(timezone.utc)

        # Benchmark sequential
        start = time.perf_counter()
        for r in routes:
            next_run = now + timedelta(minutes=r.poll_interval_min)
            await repo.mark_run(r.id, next_run_at=next_run, ran_at=now)
        end = time.perf_counter()
        print(f"Sequential 100 updates: {end - start:.4f}s")

        # Benchmark batch
        # Let's add batch method dynamically to test
        async def mark_run_batch(updates):
            if not updates: return
            await db.executemany(
                "UPDATE routes SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                [(n.isoformat(), r.isoformat(), i) for i, n, r in updates]
            )
            await db.commit()

        repo.mark_run_batch = mark_run_batch

        start = time.perf_counter()
        updates = [(r.id, now + timedelta(minutes=r.poll_interval_min), now) for r in routes]
        await repo.mark_run_batch(updates)
        end = time.perf_counter()
        print(f"Batch 100 updates: {end - start:.4f}s")

asyncio.run(main())
