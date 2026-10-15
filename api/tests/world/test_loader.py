import uuid

import pytest

from ic.graph import client
from ic.world.embed import HashingEmbedder
from ic.world.generator import generate_world
from ic.world.loader import load_world


@pytest.fixture
async def graph_name():
    client._db = None  # fresh connection bound to the current event loop
    name = f"test:{uuid.uuid4().hex[:8]}:world"
    yield name
    db = client.get_db()
    for n in await db.list_graphs():
        if n.startswith(name):
            await db.select_graph(n).delete()


async def _count(g, q: str) -> int:
    return (await g.ro_query(q)).result_set[0][0]


async def test_load_matches_model_and_is_idempotent(graph_name):
    world = generate_world(11, 40)
    embedder = HashingEmbedder(64)
    for _ in range(2):  # second pass proves re-seeding replaces rather than duplicates
        await load_world(world, graph_name, embedder)
        g = client.get_db().select_graph(graph_name)
        for label, n in world.node_counts().items():
            assert await _count(g, f"MATCH (n:{label}) RETURN count(n)") == n, label
        for rel, n in world.edge_counts().items():
            assert await _count(g, f"MATCH ()-[e:{rel}]->() RETURN count(e)") == n, rel
    assert graph_name + ":building" not in await client.get_db().list_graphs()
    inc = world.incidents[0]
    res = await g.ro_query(
        "MATCH (i:Incident {id:$id})-[:CAUSED_BY]->(c) RETURN c.id", {"id": inc.id}
    )
    assert res.result_set[0][0] == inc.cause_id
    res = await g.ro_query("MATCH (p:Postmortem) RETURN p.embedding LIMIT 1")
    assert len(res.result_set[0][0]) == 64
