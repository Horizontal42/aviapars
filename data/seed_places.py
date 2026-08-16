"""Minimal starter seed — enough for /add to work on day one (Penza, SPb,
Moscow + its three airports as a hub candidate). The full ~15-20k row RU
seed from Travelpayouts' city/airport dump + Yandex's station list (§4) is
a separate, larger task — this is deliberately not that, just unblocks
testing and real usage immediately.

Run: python -m aviapars.db.migrate && python data/seed_places.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aviapars.config import settings  # noqa: E402
from aviapars.db.engine import connect  # noqa: E402

# (id, kind, name_ru, name_en, iata, yandex_code, city_id, country, region, lat, lon, popularity, is_hub_candidate)
PLACES = [
    (1, "city", "Пенза", "Penza", "PEZ", "c11009", None, "RU", "Пензенская область", 53.2, 45.0, 30, 0),
    (2, "city", "Санкт-Петербург", "Saint Petersburg", "LED", "c2", None, "RU", "Ленинградская область", 59.93, 30.33, 90, 0),
    (3, "city", "Москва", "Moscow", "MOW", "c213", None, "RU", "Москва", 55.75, 37.62, 100, 1),
    (4, "airport", "Шереметьево", "Sheremetyevo", "SVO", "s9600213", 3, "RU", "Москва", 55.97, 37.41, 95, 1),
    (5, "airport", "Домодедово", "Domodedovo", "DME", "s9600216", 3, "RU", "Москва", 55.41, 37.90, 90, 1),
    (6, "airport", "Внуково", "Vnukovo", "VKO", "s9600219", 3, "RU", "Москва", 55.60, 37.27, 70, 1),
]


async def main() -> None:
    async with connect(settings.db_path) as db:
        await db.executemany(
            """
            INSERT INTO places (id, kind, name_ru, name_en, iata, yandex_code, city_id, country, region, lat, lon, popularity, is_hub_candidate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            PLACES,
        )
        await db.commit()
        print(f"seeded {len(PLACES)} places")


if __name__ == "__main__":
    asyncio.run(main())
