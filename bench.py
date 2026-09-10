import asyncio
import time
from datetime import date
from aviapars.adapters.yandex_rasp import YandexRaspAdapter
from aviapars.adapters.base import SearchQuery, PlaceRef, Mode

async def main():
    adapter = YandexRaspAdapter("fake-key")

    async def fake_fetch_day(self, session, origin, dest, d):
        await asyncio.sleep(0.1)
        return [], True

    YandexRaspAdapter._fetch_day = fake_fetch_day

    q = SearchQuery(
        origin=PlaceRef(place_id=1, iata="A", yandex_code="c1", rzd_code=None, city_id=None),
        dest=PlaceRef(place_id=2, iata="B", yandex_code="c2", rzd_code=None, city_id=None),
        dates=tuple(date(2023, 1, i) for i in range(1, 11)), # 10 dates
        pax=1,
        mode=Mode.TRAIN
    )

    start = time.time()
    await adapter.search(q)
    end = time.time()

    print(f"Baseline Time: {end - start:.4f}s")

if __name__ == "__main__":
    asyncio.run(main())
