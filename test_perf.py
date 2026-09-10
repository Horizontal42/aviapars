import asyncio
from datetime import date
import time
from unittest.mock import AsyncMock, MagicMock
from aviapars.adapters.base import Mode, PlaceRef, SearchQuery
from aviapars.adapters.tutu_avia_direct import TutuAviaDirectAdapter

async def run_benchmark(parallel=False):
    adapter = TutuAviaDirectAdapter()

    origin = PlaceRef(place_id=1, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)
    dest = PlaceRef(place_id=2, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)
    dates = (date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24))
    q = SearchQuery(origin=origin, dest=dest, dates=dates, pax=1, mode=Mode.PLANE)

    adapter._resolve_city_id = AsyncMock(side_effect=[("123", "ok"), ("456", "ok")])

    async def mock_search_day(session, query, origin_id, dest_id, depart_date):
        await asyncio.sleep(0.1)
        return [], "ok"

    adapter._search_day = AsyncMock(side_effect=mock_search_day)
    session = MagicMock()

    start_time = time.time()

    origin_id, origin_outcome = await adapter._resolve_city_id(session, q.origin.iata)
    dest_id, dest_outcome = await adapter._resolve_city_id(session, q.dest.iata)

    offers = []
    blocked = False
    partial = False

    if parallel:
        tasks = [
            adapter._search_day(session, q, origin_id, dest_id, depart_date)
            for depart_date in q.dates
        ]
        results = await asyncio.gather(*tasks)
        for day_offers, outcome in results:
            if outcome == "blocked":
                blocked = True
                continue
            if outcome == "error":
                partial = True
                continue
            offers.extend(day_offers)
    else:
        for depart_date in q.dates:
            day_offers, outcome = await adapter._search_day(session, q, origin_id, dest_id, depart_date)
            if outcome == "blocked":
                blocked = True
                continue
            if outcome == "error":
                partial = True
                continue
            offers.extend(day_offers)

    end_time = time.time()
    return end_time - start_time

async def main():
    seq = await run_benchmark(parallel=False)
    print(f"Sequential Duration: {seq:.4f}s")

    par = await run_benchmark(parallel=True)
    print(f"Parallel Duration: {par:.4f}s")

if __name__ == "__main__":
    asyncio.run(main())
