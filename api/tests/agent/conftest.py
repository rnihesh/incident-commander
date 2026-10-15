"""Fixtures for the triage tests (helpers live in triage_world)."""

from __future__ import annotations

import pytest
from triage_world import Base, build_base, clone, drop_session

from ic.graph import client
from ic.store import Store


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> Store:
    return Store(tmp_path_factory.mktemp("agent") / "ic.sqlite3")


@pytest.fixture(scope="module")
async def base(store) -> Base:
    client.reset_client()
    b = await build_base(store)
    yield b
    await drop_session(b.sid)


@pytest.fixture(scope="module")
async def base_ae1(store) -> Base:
    client.reset_client()
    b = await build_base(store, second_qualifying=True)
    yield b
    await drop_session(b.sid)


@pytest.fixture
async def cloner():
    """``await cloner(base)`` -> a fresh session copy; every copy is dropped afterwards."""
    made: list[str] = []

    async def make(b: Base):
        c = await clone(b)
        made.append(c.sid)
        return c

    yield make
    for sid in made:
        await drop_session(sid)
