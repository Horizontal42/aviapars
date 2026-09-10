import asyncio
import time
from datetime import date
from typing import Any
from aviapars.adapters.azimuth_direct import AzimuthDirectAdapter
from aviapars.adapters.base import Mode, PlaceRef, SearchQuery

def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)

def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)

class FakeInitResp:
    status_code = 200
    headers = {"Authorization": "Bearer test-token"}

class FakeAvailResp:
    status_code = 200
    def json(self) -> Any:
        return [{"method": "get-availability", "result": {"directions": []}}]

class FakeSession:
    def __init__(self):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.1) # Init
            return FakeInitResp()
        await asyncio.sleep(0.5) # Avail
        return FakeAvailResp()

async def run_benchmark():
    import aviapars.adapters.azimuth_direct as module
    module.AsyncSession = lambda **kw: FakeSession()

    adapter = AzimuthDirectAdapter()
    dates = (date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 14))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.PLANE)

    start = time.perf_counter()
    result = await adapter.search(q)
    end = time.perf_counter()

    print(f"Elapsed time: {end - start:.2f}s")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
