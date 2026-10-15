"""rehearse_rollback (U12, KTD14) on the hand-built world (see fixture_world).

Besides the fixture's incidents this module adds ``inc-ae4``: a second fault on ``api``
whose symptoms partly overlap inc-main's shape. db and api both fail at T0+100, web at
T0+300. c-bad (deploy on db) still explains every symptom (api ties db, which the order
clause allows), c-api (deploy on api, T0-100) fails coverage on the full set (it cannot
reach db) but explains api and web on its own branch, and api failed no later than db,
so it is not a downstream echo of c-bad: rolling back c-bad does not clear inc-ae4.
"""

from __future__ import annotations

import networkx as nx
import pytest
from fixture_world import OPENED, SNAP_T, T0, write_world

from ic.graph import client, naming
from ic.graph.snapshots import build_snapshots
from ic.tools import rehearse
from ic.tools.registry import ScopeError, ToolContext, call_tool
from ic.world import impact_reader as R
from ic.world.causality import qualifies

AE4 = "inc-ae4"
AE4_SYMPTOMS = [("db", T0 + 100), ("api", T0 + 100), ("web", T0 + 300)]


@pytest.fixture(scope="module")
async def snaps(module_sid):
    world = naming.session_world(module_sid)
    await write_world(world)
    await client.query(
        world,
        "CREATE (:Incident {id: $iid, opened_at: $at}) WITH 1 AS one "
        "UNWIND $rows AS r MATCH (i:Incident {id: $iid}), (s:Service {id: r[0]}) "
        "CREATE (i)-[:HAS]->(:Symptom {id: $iid + '-' + r[0], service_id: r[0], onset: r[1], "
        "signature: 'latency'})-[:ON]->(s)",
        {"iid": AE4, "at": OPENED, "rows": [list(r) for r in AE4_SYMPTOMS]},
    )
    # c-bad superseded c-old on db's deploy chain: the rollback makes c-old current again.
    await client.query(
        world,
        "MATCH (c:Change {id: 'c-bad'}), (q:Change {id: 'c-old'}) "
        "CREATE (c)-[:PREVIOUS_VERSION]->(q) SET q.valid_to = c.valid_from",
    )
    h = await build_snapshots(world, module_sid, "fx", SNAP_T)
    return module_sid, world, h


def ctx_for(snaps, **kw) -> ToolContext:
    sid, world, h = snaps
    return ToolContext(
        sid=sid,
        iid="fx",
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        view="true",
        **kw,
    )


async def run(snaps, incident_id: str, change_id: str, **kw):
    args = {"incident_id": incident_id, "change_id": change_id}
    return await call_tool("rehearse_rollback", args, ctx_for(snaps, **kw))


async def test_correct_rollback_clears_and_leaves_no_clone(snaps):
    sid, _, h = snaps
    tracked: list[str] = []
    r = await run(snaps, "inc-main", "c-bad", track_graph=tracked.append)
    res = r.result
    assert res["cleared"] is True and res["still_explains"] == []
    assert res["before"] == {
        "qualifies": True,
        "clause": None,
        "explained": ["api", "db", "web"],
        "anchor_onset": T0 + 100,
        "query": res["before"]["query"],
    }
    assert res["rollback"]["impacts_removed"] == 1
    assert res["rollback"]["previous_version"] == "c-old"
    # every windowed candidate of the true view except the rolled-back change
    ids = {v["id"] for v in res["verdicts"]}
    assert ids == {"x-cert", "c-disk", "c-cfg", "c-api", "c-late", "c-unk"}
    assert res["checked"] == len(ids) and res["clone_ms"] > 0
    by = {v["id"]: v for v in res["verdicts"]}
    # decoys on symptomatic callers: a cause on their own branch, but downstream echoes
    assert by["c-api"]["partial_clause"] == "independent"
    assert by["c-unk"]["partial_clause"] == "independent"
    assert by["c-late"]["partial_clause"] == "after_onset"
    assert by["c-cfg"]["partial_clause"] == "signature"

    clone = naming.rehearse(sid, "inc-main")
    assert tracked == [clone] and res["clone"] == clone and res["source"] == h.true_graph
    assert not await client.graph_exists(clone)
    # read-only on the snapshot: c-bad still impacts db there, c-old still superseded
    rows = await R._rows(
        h.true_graph,
        "MATCH (c:Change {id: 'c-bad'})-[:IMPACTS]->(s) "
        "MATCH (q:Change {id: 'c-old'}) RETURN s.id, c.valid_to > c.valid_from, q.valid_to",
    )
    assert rows == [["db", True, T0 - 600]]
    # the evidence replays on a copy of the snapshot: writes included, in order
    assert len(r.cypher) == len(r.params)
    assert any(c.startswith("MATCH (c:Change {id: $entity_id})-[e:IMPACTS]") for c in r.cypher)


async def test_partial_overlap_second_fault_is_refused_naming_its_path(snaps):
    # premise: triage would diagnose c-bad here (it qualifies, c-api fails coverage)
    ex = await call_tool("exonerate", {"incident_id": AE4, "change_id": "c-bad"}, ctx_for(snaps))
    assert ex.result["qualifies"] is True
    ex = await call_tool("exonerate", {"incident_id": AE4, "change_id": "c-api"}, ctx_for(snaps))
    assert ex.result["verdict"]["clause"] == "coverage"

    r = await run(snaps, AE4, "c-bad")
    res = r.result
    assert res["cleared"] is False
    [s] = res["still_explains"]
    assert s["entity_id"] == "c-api" and s["full"] is False
    assert s["services"] == ["api", "web"]
    assert s["path"] == ["c-api", "api", "web"]
    assert r.cypher[s["query"]].startswith("MATCH (c:Change {id: $entity_id})")
    assert "c-api still explains api, web via c-api > api > web" in res["detail"]
    assert rehearse.surviving_text(res["still_explains"]) == [
        "c-api explains api, web via c-api > api > web"
    ]
    assert not await client.graph_exists(naming.rehearse(snaps[0], AE4))


async def test_rolling_back_the_wrong_change_does_not_clear(snaps):
    res = (await run(snaps, "inc-main", "c-api")).result
    assert res["cleared"] is False and res["before"]["qualifies"] is False
    full = [s for s in res["still_explains"] if s["full"]]
    assert [s["entity_id"] for s in full] == ["c-bad"]
    assert full[0]["services"] == ["api", "db", "web"]


@pytest.mark.parametrize(("incident_id", "change_id"), [("inc-main", "c-bad"), (AE4, "c-bad")])
async def test_cypher_agrees_with_the_python_twin(snaps, incident_id, change_id):
    _, _, h = snaps
    res = (await run(snaps, incident_id, change_id)).result
    g = await R.read_impact_graph(h.true_graph)
    syms = await R.read_symptoms(h.true_graph, incident_id)
    ents = await R.read_window_entities(h.true_graph, min(s.onset for s in syms))
    hops = nx.single_source_shortest_path_length(g, change_id)
    explained = {s.service_id for s in syms if s.service_id in hops}
    anchor = min(s.onset for s in syms if s.service_id in explained)
    assert (
        sorted(explained) == res["before"]["explained"] and anchor == res["before"]["anchor_onset"]
    )
    g.remove_node(change_id)  # the rollback: the change impacts nothing
    by = {v["id"]: v for v in res["verdicts"]}
    assert set(by) == {e.id for e in ents} - {change_id}
    for e in ents:
        if e.id == change_id:
            continue
        v = by[e.id]
        ok, clause, _ = qualifies(e, g, syms)
        assert (v["qualifies"], v["clause"]) == (ok, clause), e.id
        if not ok:
            p_ok, p_clause, overlap = rehearse.qualifies_subset(e, g, syms, explained, anchor)
            assert (v["partial"], v["partial_clause"]) == (p_ok, p_clause), e.id
            assert v["services"] == overlap, e.id


async def test_clone_is_deleted_when_the_rehearsal_fails(snaps, monkeypatch):
    sid = snaps[0]
    clone = naming.rehearse(sid, "inc-main")
    seen = []

    async def boom(log, label, entity_id):
        seen.append(await client.graph_exists(clone))
        raise RuntimeError("rollback exploded")

    monkeypatch.setattr(rehearse, "_rollback", boom)
    with pytest.raises(RuntimeError, match="rollback exploded"):
        await run(snaps, "inc-main", "c-bad")
    assert seen == [True]  # it got as far as the clone
    assert not await client.graph_exists(clone)


async def test_needs_a_session_scope(snaps):
    _, world, h = snaps
    ctx = ToolContext(snapshot_true=h.true_graph, snapshot_known=h.known_graph, view="true")
    with pytest.raises(ScopeError):
        await call_tool("rehearse_rollback", {"incident_id": "inc-main", "change_id": "c-bad"}, ctx)
    other = ctx_for(snaps)
    other.sid = naming.new_session_id()  # another session's scope may not read this snapshot
    with pytest.raises(ScopeError):
        await call_tool(
            "rehearse_rollback", {"incident_id": "inc-main", "change_id": "c-bad"}, other
        )
