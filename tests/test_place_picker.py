from __future__ import annotations

from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from aviapars.db.models import Place
from aviapars.telegram import place_picker
from aviapars.telegram.place_picker import ask_place_pick, resolve_pick
from aviapars.telegram.states import AddRoute

from tests.test_screen import CHAT_ID, FakeBot

from aviapars.telegram.screen import Screen


def make_place(place_id: int, name: str) -> Place:
    return Place(
        id=place_id,
        kind="city",
        name_ru=name,
        name_en=name,
        iata=None,
        yandex_code=None,
        rzd_code=None,
        city_id=None,
        country="RU",
        region=None,
        lat=None,
        lon=None,
        popularity=0,
        is_hub_candidate=False,
    )


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=7))


def stub_places(monkeypatch: pytest.MonkeyPatch, places: list[Place]) -> None:
    class StubRepo:
        def __init__(self, db: Any) -> None: ...

        async def search_by_name(self, text: str) -> list[Place]:
            return places

    monkeypatch.setattr(place_picker, "PlacesRepo", StubRepo)


async def test_single_match_skips_the_keyboard(monkeypatch: pytest.MonkeyPatch, state: FSMContext) -> None:
    """One real option plus a «☰ Меню» exit is not a choice — it used to cost a
    tap for every unambiguous city, e.g. «Санкт-Петербург»."""
    stub_places(monkeypatch, [make_place(2, "Санкт-Петербург")])
    bot = FakeBot()
    picked: list[Place] = []

    async def on_picked(place: Place) -> None:
        picked.append(place)

    await ask_place_pick(
        Screen(bot, CHAT_ID, state),
        state,
        None,  # type: ignore[arg-type]
        title="➕ Новый маршрут",
        text="Санкт-Петербург",
        pick_state=AddRoute.origin_pick,
        storage_key="candidates_origin",
        on_picked=on_picked,
    )

    assert [p.id for p in picked] == [2]
    assert bot.sent == []  # the next step renders, not the picker
    assert await state.get_state() is None
    assert "candidates_origin" not in await state.get_data()


async def test_several_matches_still_ask(monkeypatch: pytest.MonkeyPatch, state: FSMContext) -> None:
    stub_places(monkeypatch, [make_place(2, "Санкт-Петербург"), make_place(3, "Пулково")])
    bot = FakeBot()

    async def on_picked(place: Place) -> None:
        raise AssertionError("must not auto-pick when there is a real choice")

    await ask_place_pick(
        Screen(bot, CHAT_ID, state),
        state,
        None,  # type: ignore[arg-type]
        title="➕ Новый маршрут",
        text="Питер",
        pick_state=AddRoute.origin_pick,
        storage_key="candidates_origin",
        on_picked=on_picked,
    )

    assert await state.get_state() == AddRoute.origin_pick.state
    assert len((await state.get_data())["candidates_origin"]) == 2
    assert "Питер" in bot.sent[0]


async def test_no_match_asks_again(monkeypatch: pytest.MonkeyPatch, state: FSMContext) -> None:
    stub_places(monkeypatch, [])
    bot = FakeBot()

    async def on_picked(place: Place) -> None:
        raise AssertionError("nothing to pick")

    await ask_place_pick(
        Screen(bot, CHAT_ID, state),
        state,
        None,  # type: ignore[arg-type]
        title="➕ Новый маршрут",
        text="Мухосранск",
        pick_state=AddRoute.origin_pick,
        storage_key="candidates_origin",
        on_picked=on_picked,
    )

    assert await state.get_state() is None
    assert "Мухосранск" in bot.sent[0]


def test_resolve_pick_rejects_a_stale_keyboard() -> None:
    places = [make_place(1, "Пенза")]
    assert resolve_pick(places, 0) is places[0]
    assert resolve_pick(places, 1) is None
    assert resolve_pick(places, -1) is None
    assert resolve_pick(None, 0) is None
    assert resolve_pick(["not a place"], 0) is None
