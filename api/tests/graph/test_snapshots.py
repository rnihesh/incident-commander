import pytest

from ic.graph import client, naming
from ic.graph.schema import OBSERVED_CALL_THRESHOLD, WORLD_NOW
from ic.graph.snapshots import build_snapshots, create_session_world, delete_snapshots
from ic.world.embed import HashingEmbedder
from ic.world.generator import generate_world
from ic.world.loader import load_world
from ic.world.model import impact_graph

T = 1_000_000


@pytest.fixture
async def sid():
    client.reset_client()
    s = naming.new_session_id()
    yield s
    db = client.get_db()
    for n in await db.list_graphs():
        if n.startswith(f"s:{s}:"):
            await db.select_graph(n).delete()
    client.reset_client()  # don't leak a loop-bound connection into other tests


async def rows(g, q, params=None):
    return (await client.query(g, q, params, read_only=True)).result_set


async def impacts(g):
    r = await rows(g, "MATCH (a)-[:IMPACTS]->(b) RETURN a.id, b.id")
    return {tuple(x) for x in r}


async def blank_world(sid, cypher: str) -> str:
    name = naming.session_world(sid)
    await client.query(name, cypher)
    return name


SVC = "CREATE (:Service {id:'A'}), (:Service {id:'B'}), (:Service {id:'C'}) "


async def test_future_dependency_absent_from_true(sid):
    w = await blank_world(
        sid,
        SVC + "WITH 1 AS x MATCH (a:Service {id:'A'}), (b:Service {id:'B'}), (c:Service {id:'C'}) "
        f"CREATE (a)-[:DEPENDS_ON {{valid_from:{T - 10}, valid_to:{T + 100}, recorded_at:{T - 10}}}]->(b), "
        f"(a)-[:DEPENDS_ON {{valid_from:{T + 5}, valid_to:{T + 100}, recorded_at:{T - 10}}}]->(c)",
    )
    h = await build_snapshots(w, sid, "i1", T)
    assert await impacts(h.true_graph) == {("B", "A")}
    assert await impacts(h.known_graph) == {("B", "A")}
    meta = await rows(h.true_graph, "MATCH (m:SnapshotMeta) RETURN m.t, m.view, m.source")
    assert meta == [[T, "true", w]]
    await delete_snapshots(h)
    assert not await client.graph_exists(h.true_graph)


async def test_late_recorded_deploy_true_only(sid):
    w = await blank_world(
        sid,
        SVC.replace(", (:Service {id:'B'}), (:Service {id:'C'})", "")
        + "WITH 1 AS x MATCH (a:Service {id:'A'}) "
        f"CREATE (:Change {{id:'late', valid_from:{T - 50}, recorded_at:{T + 50}}})-[:CHANGES]->(a), "
        f"(:Change {{id:'ok', valid_from:{T - 50}, recorded_at:{T - 40}}})-[:CHANGES]->(a), "
        f"(:Change {{id:'future', valid_from:{T + 1}, recorded_at:{T - 40}}})-[:CHANGES]->(a), "
        "(:Change {id:'handmade'})-[:CHANGES]->(a)",
    )
    h = await build_snapshots(w, sid, "i1", T)
    assert await impacts(h.true_graph) == {("late", "A"), ("ok", "A"), ("handmade", "A")}
    assert await impacts(h.known_graph) == {("ok", "A")}


async def test_handmade_edge_true_only(sid):
    w = await blank_world(
        sid,
        SVC + "WITH 1 AS x MATCH (a:Service {id:'A'}), (b:Service {id:'B'}) "
        "CREATE (a)-[:DEPENDS_ON]->(b)",
    )
    h = await build_snapshots(w, sid, "i1", T)
    assert await impacts(h.true_graph) == {("B", "A")}
    assert await impacts(h.known_graph) == set()


async def test_observed_call_threshold(sid):
    n = OBSERVED_CALL_THRESHOLD
    w = await blank_world(
        sid,
        SVC + "WITH 1 AS x MATCH (a:Service {id:'A'}), (b:Service {id:'B'}), (c:Service {id:'C'}) "
        f"CREATE (a)-[:OBSERVED_CALL {{first_seen:{T - 5}, last_seen:{T}, count:{n}}}]->(b), "
        f"(a)-[:OBSERVED_CALL {{first_seen:{T - 5}, last_seen:{T}, count:{n - 1}}}]->(c), "
        f"(b)-[:OBSERVED_CALL {{first_seen:{T + 5}, last_seen:{T + 9}, count:{n + 9}}}]->(c)",
    )
    h = await build_snapshots(w, sid, "i1", T)
    assert await impacts(h.true_graph) == {("B", "A")}
    left = await rows(h.true_graph, "MATCH ()-[o:OBSERVED_CALL]->() RETURN count(o)")
    assert left == [[2]]  # the not-yet-seen call was pruned


async def test_expiry_impacts(sid):
    w = await blank_world(
        sid,
        SVC.replace(", (:Service {id:'B'}), (:Service {id:'C'})", "")
        + "WITH 1 AS x MATCH (a:Service {id:'A'}) "
        f"CREATE (:Expiry {{id:'past', valid_to:{T - 1}, recorded_at:{T - 100}}})-[:EXPIRES_ON]->(a), "
        f"(:Expiry {{id:'edge', valid_to:{T}, recorded_at:{T - 100}}})-[:EXPIRES_ON]->(a), "
        f"(:Expiry {{id:'later', valid_to:{T + 1}, recorded_at:{T - 100}}})-[:EXPIRES_ON]->(a)",
    )
    h = await build_snapshots(w, sid, "i1", T)
    assert await impacts(h.true_graph) == {("past", "A"), ("edge", "A")}
    assert await rows(h.true_graph, "MATCH (e:Expiry) RETURN count(e)") == [[3]]  # nodes kept


async def test_future_incident_pruned(sid):
    w = await blank_world(
        sid,
        SVC.replace(", (:Service {id:'B'}), (:Service {id:'C'})", "")
        + "WITH 1 AS x MATCH (a:Service {id:'A'}) "
        f"CREATE (i:Incident {{id:'new', opened_at:{T + 10}}})-[:HAS]->(:Symptom {{id:'s1', onset:{T + 11}}})-[:ON]->(a), "
        "(pm:Postmortem {id:'pm'})-[:ABOUT]->(i), "
        "(pm)-[:RECOMMENDS]->(:Recommendation {id:'r1'}), "
        f"(i2:Incident {{id:'old', opened_at:{T - 10}}})-[:HAS]->(:Symptom {{id:'s2', onset:{T - 9}}})-[:ON]->(a), "
        f"(:Recommendation {{id:'r2', opened_at:{T + 3}}})",
    )
    h = await build_snapshots(w, sid, "i1", T)
    got = await rows(h.true_graph, "MATCH (n) WHERE n.id IS NOT NULL RETURN n.id ORDER BY n.id")
    ids = {r[0] for r in got}
    assert not ({"new", "s1", "r2", "pm", "r1"} & ids)
    assert {"old", "s2", "A"} <= ids


@pytest.mark.parametrize("seed", [3, 7])
async def test_cross_check_against_model(sid, seed):
    world = generate_world(seed, n_services=40)
    src = naming.session_world(sid)
    await load_world(world, src, HashingEmbedder(64))
    incs = sorted(world.incidents, key=lambda i: i.opened_at)
    picks = [incs[0], incs[len(incs) // 2], incs[-1]]
    for inc in picks:
        h = await build_snapshots(src, sid, inc.id.replace(":", "_"), inc.opened_at)
        for view, name in (("true", h.true_graph), ("known", h.known_graph)):
            expected = {
                (a, b)
                for a, b in impact_graph(world, inc.opened_at, as_known=(view == "known")).edges
            }
            got = await impacts(name)
            assert got == expected, (view, inc.id, sorted(got ^ expected)[:5])
        await delete_snapshots(h)


async def test_world_unchanged_by_snapshots(sid):
    world = generate_world(5, n_services=30)
    src = naming.session_world(sid)
    await load_world(world, src, HashingEmbedder(64))
    q_n = "MATCH (n) RETURN count(n)"
    q_e = "MATCH ()-[e]->() RETURN count(e)"
    before = (await rows(src, q_n), await rows(src, q_e))
    await build_snapshots(src, sid, "x", world.incidents[0].opened_at)
    await build_snapshots(src, sid, "y", WORLD_NOW, kind="verify")
    assert (await rows(src, q_n), await rows(src, q_e)) == before
    assert await rows(src, "MATCH ()-[e:IMPACTS]->() RETURN count(e)") == [[0]]


async def test_names_dont_collide_and_delete_is_scoped(sid):
    other = naming.new_session_id()
    try:
        w1 = await blank_world(sid, SVC)
        w2 = naming.session_world(other)
        await client.query(w2, SVC)
        h1 = await build_snapshots(w1, sid, "i1", T)
        h2 = await build_snapshots(w2, other, "i1", T)
        assert {h1.true_graph, h1.known_graph}.isdisjoint({h2.true_graph, h2.known_graph})
        v = await build_snapshots(w1, sid, "i1", T, kind="verify")
        assert v.true_graph != h1.true_graph
        await delete_snapshots(h1)
        assert not await client.graph_exists(h1.true_graph)
        assert not await client.graph_exists(h1.known_graph)
        for g in (h2.true_graph, h2.known_graph, v.true_graph, w1, w2):
            assert await client.graph_exists(g)
    finally:
        db = client.get_db()
        for n in await db.list_graphs():
            if n.startswith(f"s:{other}:"):
                await db.select_graph(n).delete()


async def test_create_session_world_copies_seed(sid):
    seed = naming.seed_world()
    if not await client.graph_exists(seed):
        pytest.skip("seed:world not loaded")
    name = await create_session_world(sid)
    q = "MATCH (n) RETURN count(n)"
    assert await rows(name, q) == await rows(seed, q)


@pytest.mark.slow
async def test_400_service_budget(sid):
    world = generate_world(1, n_services=400)
    src = naming.session_world(sid)
    await load_world(world, src, HashingEmbedder(64))
    inc = sorted(world.incidents, key=lambda i: i.opened_at)[len(world.incidents) // 2]
    h = await build_snapshots(src, sid, "slow", inc.opened_at)
    print(f"\nsnapshot build both views (400 svc): {h.build_ms} ms")
    assert h.build_ms < 2000
