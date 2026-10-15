from types import SimpleNamespace

from ic.actions.who_to_page import choose
from ic.graph.client import query
from ic.graph.schema import OPEN_END

T = 5000

SETUP = """
CREATE (t1:Team {id: 't1', name: 'Core'}), (t2:Team {id: 't2', name: 'Edge'}),
       (t3:Team {id: 't3', name: 'Data'})
CREATE (a:Service {id: 'svc-a'}), (b:Service {id: 'svc-b'}), (c:Service {id: 'svc-c'})
CREATE (t1)-[:OWNS {valid_from: 0, valid_to: $open}]->(a)
CREATE (t2)-[:OWNS {valid_from: 0, valid_to: $open}]->(b)
CREATE (t3)-[:OWNS {valid_from: 0, valid_to: $open}]->(c)
CREATE (p1:Person {id: 'p1', name: 'Ann'})-[:ON_CALL {valid_from: 0, valid_to: $open}]->(t1)
CREATE (p2:Person {id: 'p2', name: 'Bob'}), (p3:Person {id: 'p3', name: 'Cy'})
CREATE (:Change {id: 'c-a'})-[:CHANGES]->(a)
CREATE (cb:Change {id: 'c-b'})-[:CHANGES]->(b)
CREATE (p2)-[:AUTHORED]->(cb)
CREATE (:Change {id: 'c-c'})-[:CHANGES]->(c)
CREATE (r:Change {id: 'r-old'})
CREATE (p3)-[:AUTHORED]->(r)
CREATE (:Incident {id: 'inc-old'})-[:REMEDIATED_BY]->(r)
"""


async def build(g):
    await query(g, SETUP, {"open": OPEN_END})


def inc(service="svc-b"):
    return SimpleNamespace(id="inc-new", opened_at=T, service_id=service)


async def test_on_call_at_t_for_owning_team(world_graph):
    await build(world_graph)
    t = await choose(world_graph, inc(), "c-a", [{"incident_id": "inc-old"}])
    assert (t.person_id, t.name) == ("p1", "Ann")
    assert "on-call at T" in t.reason and "Core" in t.reason


async def test_on_call_slot_must_cover_t(world_graph):
    await build(world_graph)
    await query(world_graph, "MATCH ()-[o:ON_CALL]->() SET o.valid_to = $v", {"v": T})  # ended
    t = await choose(world_graph, inc(), "c-a", None)
    assert t.person_id is None  # no author, no precedents, alert svc-b has no on-call


async def test_author_when_no_on_call(world_graph):
    await build(world_graph)
    t = await choose(world_graph, inc(), "c-b", [{"incident_id": "inc-old"}])
    assert (t.person_id, t.name) == ("p2", "Bob")
    assert "author" in t.reason and "c-b" in t.reason


async def test_precedent_resolver_when_no_author(world_graph):
    await build(world_graph)
    precedents = [{"incident_id": "inc-none"}, {"incident_id": "inc-old"}]
    t = await choose(world_graph, inc(), "c-c", precedents)
    assert (t.person_id, t.name) == ("p3", "Cy")
    assert "precedent" in t.reason and "inc-old" in t.reason


async def test_alert_service_on_call_last(world_graph):
    await build(world_graph)
    t = await choose(world_graph, inc("svc-a"), None, [])
    assert t.person_id == "p1" and "alert service" in t.reason


async def test_nobody(world_graph):
    await build(world_graph)
    t = await choose(world_graph, inc("svc-c"), "c-c", [])
    assert t.person_id is None and t.reason
