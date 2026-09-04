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


async def test_search_by_iata_code_excludes_airport_rows(repo: PlacesRepo) -> None:
    # LED matches both the Saint Petersburg city row and the Пулково airport row by
    # iata; only the city row must come back — kind = 'city' filter in search_by_name.
    results = await repo.search_by_name("led")
    assert [p.id for p in results] == [2]


async def test_search_never_surfaces_an_airport_row(repo: PlacesRepo) -> None:
    # regression: airport-kind rows are never backfilled with rzd_code/yandex_code
    # (only city rows are, per seed_places.py), so letting a pick resolve to an
    # airport row silently breaks rail search for that leg. A query that matches
    # only an airport (no city of the same name) must now find nothing at all.
    results = await repo.search_by_name("Пулково")
    assert results == []


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
    name: str | None = None,
    coords: tuple[float, float] | None = None,
) -> None:
    lat, lon = coords if coords else (None, None)
    await db.execute(
        """
        INSERT INTO places (id, kind, name_ru, city_id, country, popularity, is_hub_candidate, lat, lon)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            place_id,
            kind,
            name or f"place-{place_id}",
            city_id,
            country,
            popularity,
            int(is_hub_candidate),
            lat,
            lon,
        ),
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


# Real approximate coordinates — the whole point of these tests is that actual RU geography,
# not fixture-convenient numbers, produces the ranking the user expects.
_EKB = (56.8389, 60.6057)
_SOCHI = (43.5855, 39.7231)
_PENZA = (53.2007, 45.0046)
_SAMARA = (53.1959, 50.1002)
_MOSCOW = (55.7558, 37.6173)
_TURA = (64.2833, 100.2167)  # tiny Siberian airport; popularity 70 in the live seed


async def test_hub_candidates_near_prefers_a_hub_that_is_on_the_route(db: aiosqlite.Connection) -> None:
    # regression: ranking was popularity-only, so Moscow (90) plus whatever tied for second
    # (Tura, 70 — thousands of km off any European-Russia route) came back for every single
    # route in the country. Samara sits almost exactly on the Ekaterinburg→Sochi line and has
    # to win despite far lower popularity; Tura has to disappear entirely.
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=10, name="Екатеринбург", coords=_EKB)
    await _insert_place(db, place_id=2, city_id=20, name="Сочи", coords=_SOCHI)
    await _insert_place(db, place_id=3, city_id=30, name="Москва", is_hub_candidate=True, popularity=90, coords=_MOSCOW)
    await _insert_place(db, place_id=4, city_id=40, name="Самара", is_hub_candidate=True, popularity=50, coords=_SAMARA)
    await _insert_place(db, place_id=5, city_id=50, name="Тура", is_hub_candidate=True, popularity=70, coords=_TURA)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest, limit=3)

    assert [c.id for c in candidates] == [4, 3]


async def test_hub_candidates_near_drops_hubs_far_off_a_short_route(db: aiosqlite.Connection) -> None:
    # Penza→Samara is 339 km; routing it through Moscow costs 1069 km of detour, well past the
    # allowance. Popularity-only ranking offered Moscow here too.
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=10, name="Пенза", coords=_PENZA)
    await _insert_place(db, place_id=2, city_id=20, name="Самара", coords=_SAMARA)
    await _insert_place(db, place_id=3, city_id=30, name="Москва", is_hub_candidate=True, popularity=90, coords=_MOSCOW)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    assert await repo.hub_candidates_near(origin, dest) == []


async def test_hub_candidates_near_breaks_ties_on_popularity(db: aiosqlite.Connection) -> None:
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=10, coords=_EKB)
    await _insert_place(db, place_id=2, city_id=20, coords=_SOCHI)
    await _insert_place(db, place_id=3, city_id=30, is_hub_candidate=True, popularity=40, coords=_SAMARA)
    await _insert_place(db, place_id=4, city_id=40, is_hub_candidate=True, popularity=60, coords=_SAMARA)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest, limit=2)

    assert [c.id for c in candidates] == [4, 3]


async def test_hub_candidates_near_ranks_coordless_candidates_last(db: aiosqlite.Connection) -> None:
    # a missing lat/lon is not "on the route" — an unmapped candidate must not win by default
    # just because it can't be scored, but it stays usable as filler.
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=10, coords=_EKB)
    await _insert_place(db, place_id=2, city_id=20, coords=_SOCHI)
    await _insert_place(db, place_id=3, city_id=30, is_hub_candidate=True, popularity=99, coords=None)
    await _insert_place(db, place_id=4, city_id=40, is_hub_candidate=True, popularity=10, coords=_SAMARA)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest, limit=2)

    assert [c.id for c in candidates] == [4, 3]


async def test_hub_candidates_near_falls_back_to_popularity_without_endpoint_coords(
    db: aiosqlite.Connection,
) -> None:
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=1, city_id=10, coords=None)
    await _insert_place(db, place_id=2, city_id=20, coords=None)
    await _insert_place(db, place_id=3, city_id=30, is_hub_candidate=True, popularity=10, coords=_SAMARA)
    await _insert_place(db, place_id=4, city_id=40, is_hub_candidate=True, popularity=90, coords=_TURA)

    origin = await repo.get(1)
    dest = await repo.get(2)
    assert origin is not None and dest is not None

    candidates = await repo.hub_candidates_near(origin, dest, limit=2)

    assert [c.id for c in candidates] == [4, 3]


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

async def test_get_many_preserves_order_and_skips_missing(db: aiosqlite.Connection) -> None:
    repo = PlacesRepo(db)
    await _insert_place(db, place_id=10, city_id=1, name="City A")
    await _insert_place(db, place_id=20, city_id=2, name="City B")
    await _insert_place(db, place_id=30, city_id=3, name="City C")

    # Ask for [30, 999, 10] -> Should return [Place(30), Place(10)] in that order
    places = await repo.get_many([30, 999, 10])

    assert len(places) == 2
    assert places[0].id == 30
    assert places[1].id == 10

async def test_get_many_empty_list(db: aiosqlite.Connection) -> None:
    repo = PlacesRepo(db)
    places = await repo.get_many([])
    assert places == []
