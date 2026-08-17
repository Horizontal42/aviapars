from __future__ import annotations

from pathlib import Path

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
