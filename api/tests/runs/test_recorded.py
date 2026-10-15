"""record_hero cleanup: a failed recording leaves no session graphs behind."""

from __future__ import annotations

import pytest

from ic.graph import client, naming
from ic.runs import recorded

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_client():
    client.reset_client()
    yield
    client.reset_client()


async def test_drop_keeps_going_past_a_failed_delete_and_reraises(monkeypatch):
    deleted: list[str] = []

    class Graph:
        def __init__(self, name):
            self.name = name

        async def delete(self):
            if self.name.endswith(":bad"):
                raise RuntimeError("delete failed")
            deleted.append(self.name)

    class Db:
        async def list_graphs(self):
            return ["s:x:bad", "s:x:a", "other", "s:x:b"]

        def select_graph(self, name):
            return Graph(name)

    monkeypatch.setattr(client, "_db", Db())
    with pytest.raises(RuntimeError, match="delete failed"):
        await recorded._drop("s:x:")
    assert deleted == ["s:x:a", "s:x:b"]


async def test_a_failed_world_build_is_cleaned_up(monkeypatch, tmp_path):
    sid = naming.new_session_id()
    monkeypatch.setattr(recorded.naming, "new_session_id", lambda: sid)

    async def half_built(sid_: str) -> str:
        world = naming.session_world(sid_)
        await client.query(world, "CREATE (:Service {id: 'half'})")
        raise RuntimeError("world build failed")

    monkeypatch.setattr(recorded, "create_session_world", half_built)
    with pytest.raises(RuntimeError, match="world build failed"):
        await recorded.record_hero(tmp_path / "hero.json")
    left = [g for g in await client.get_db().list_graphs() if g.startswith(f"s:{sid}:")]
    assert left == []
    assert not (tmp_path / "hero.json").exists()
