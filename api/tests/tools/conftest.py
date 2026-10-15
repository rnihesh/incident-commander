import pytest

from ic.graph import client, naming


async def drop_session(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


@pytest.fixture
async def sid():
    client.reset_client()
    s = naming.new_session_id()
    yield s
    await drop_session(s)
    client.reset_client()  # don't leak a loop-bound connection into other tests


@pytest.fixture(scope="module")
async def module_sid():
    client.reset_client()  # an earlier test may have bound the client to a closed loop
    s = naming.new_session_id()
    yield s
    await drop_session(s)
    client.reset_client()
