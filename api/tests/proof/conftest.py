"""Fixtures for the proof tests (helpers live in proof_fixture)."""

from __future__ import annotations

import pytest
from proof_fixture import Incident, drop_session, setup_incident

from ic.graph import client, naming
from ic.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ic.sqlite3")


@pytest.fixture
async def sid():
    client.reset_client()
    s = naming.new_session_id()
    yield s
    await drop_session(s)
    client.reset_client()


@pytest.fixture
async def incident(sid, store) -> Incident:
    return await setup_incident(sid, store)
