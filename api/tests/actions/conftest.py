import pytest

from ic.graph import client, naming


@pytest.fixture
async def world_graph():
    client.reset_client()
    sid = naming.new_session_id()
    yield naming.session_world(sid)
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()
    client.reset_client()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    from ic.actions import retry

    monkeypatch.setattr(retry, "BASE_DELAY_S", 0.0)
