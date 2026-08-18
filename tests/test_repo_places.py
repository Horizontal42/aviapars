from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from aviapars.db.engine import connect
from aviapars.db.repo_places import PlacesRepo

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


@pytest.fixture
async def repo(tmp_path: Path) -> PlacesRepo:
    path = tmp_path / "places.db"
    async with connect(str(path)) as db:
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await db.executescript(migration.read_text(encoding="utf-8"))
        await db.execute(
            "INSERT INTO places (id, kind, name_ru, name_en, iata, country, is_hub_candidate) VALUES "
            "(1, 'city', 'Пенза', 'Penza', 'PEZ', 'RU', 0), "
            "(2, 'city', 'Санкт-Петербург', 'Saint Petersburg', 'LED', 'RU', 1), "
            "(3, 'airport', 'Пулково', 'Pulkovo Airport', 'LED', 'RU', 0)"
        )
        await db.commit()
        yield PlacesRepo(db)


@pytest.mark.parametrize(
    "query",
    ["Пенза", "пенза", "ПЕНЗА", "Penza", "penza"],
)
async def test_search_finds_city_regardless_of_case(repo: PlacesRepo, query: str) -> None:
    results = await repo.search_by_name(query)
    assert any(p.id == 1 for p in results)


@pytest.mark.parametrize(
    "query",
    ["Санкт-Петербург", "санкт-петербург", "санкт петербург", "saint-petersburg", "saint petersburg"],
)
async def test_search_folds_hyphen_and_space_as_equivalent(repo: PlacesRepo, query: str) -> None:
    results = await repo.search_by_name(query)
    assert any(p.id == 2 for p in results)


async def test_search_by_iata_code(repo: PlacesRepo) -> None:
    results = await repo.search_by_name("led")
    ids = {p.id for p in results}
    assert {2, 3} <= ids


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "test.db")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


async def _insert_place(
    db: aiosqlite.Connection,
    *,
    place_id: int,
    kind: str = "airport",
    city_id: int | None,
    is_hub_candidate: bool = False,
    country: str = "RU",
    popularity: int = 0,
) -> None:
    await db.execute(
        """
        INSERT INTO places (id, kind, name_ru, city_id, country, popularity, is_hub_candidate)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (place_id, kind, f"place-{place_id}", city_id, country, popularity, int(is_hub_candidate)),
    )
    await db.commit()


async def test_hub_candidates_near_excludes_same_city_as_endpoint(db: aiosqlite.Connection) -> None:
    # regression: hub_candidates_near used to exclude by place id only, so a hub
    # candidate sharing origin's *city* (but a different place row — e.g. origin's own
    # train station when origin is an airport) was wrongly offered as an intermediate hub.
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, kind="airport", city_id=100)  # origin: an airport in city 100
    await _insert_place(db, place_id=2, kind="airport", city_id=200)  # dest: a different city
    # same city as origin (100), different place row, marked as a hub candidate
    await _insert_place(db, place_id=3, kind="rail_station", city_id=100, is_hub_candidate=True)
    # a genuinely distinct city, also a hub candidate
    await _insert_place(db, place_id=4, kind="airport", city_id=300, is_hub_candidate=True, popularity=10)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest)

    assert [c.id for c in candidates] == [4]


async def test_hub_candidates_near_handles_null_city_id(db: aiosqlite.Connection) -> None:
    # a NULL origin/dest city_id must not poison the exclusion filter and silently
    # drop every candidate (SQL three-valued logic with a plain NOT IN would do exactly
    # that) — see the IS NOT-based rewrite in repo_places.py.
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=None)
    await _insert_place(db, place_id=2, city_id=None)
    await _insert_place(db, place_id=3, city_id=300, is_hub_candidate=True, popularity=10)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest)

    assert [c.id for c in candidates] == [3]
