import asyncio
import time
from datetime import date
from unittest.mock import AsyncMock
from aviapars.adapters.pobeda_direct import PobedaDirectAdapter
from aviapars.adapters.base import SearchQuery, PlaceRef, Mode

async def run_benchmark():
    adapter = PobedaDirectAdapter()

    # Mock _search_day to simulate network delay
    async def mock_search_day(session, q, depart_date):
        await asyncio.sleep(0.1) # Simulate 100ms latency
        return ([], "ok")

    adapter._search_day = AsyncMock(side_effect=mock_search_day)

    q = SearchQuery(
        origin=PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None),
        dest=PlaceRef(place_id=2, iata="SVX", yandex_code=None, rzd_code=None, city_id=None),
        dates=tuple(date(2023, 10, i) for i in range(1, 11)), # 10 dates
        pax=1,
        mode=Mode.PLANE
    )

    start = time.perf_counter()
    await adapter.search(q)
    end = time.perf_counter()

    print(f"Time taken for {len(q.dates)} dates: {end - start:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
