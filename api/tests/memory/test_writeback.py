"""Memory write-back (U13, KTD8, R20, F3) on real FalkorDB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memory_world import drop_session, edge, rows

from ic.api.events import EventType
from ic.graph import client, naming
from ic.graph.schema import OBSERVED_CALL_THRESHOLD
from ic.graph.snapshots import build_view
from ic.incidents import state as st
from ic.memory import writeback as wb
from ic.proof.verify import verify_incident

SRC = Path(wb.__file__).resolve().parents[1]


# --- the two-incident sequence ------------------------------------------------------------


async def test_learned_edge_created_with_initial_confidence(coupled):
    first = coupled.rows[0]
    assert first["verdict"] == "DIAGNOSED" and first["correct"]
    assert first["final_state"] == "LEARNED"
    e = coupled.edge_after_first
    assert e["confidence"] == pytest.approx(wb.INITIAL_CONFIDENCE)
    assert e["confirmations"] == 0 and e["contradictions"] == 0
    assert e["first_learned_incident"] == coupled.faults[0].incident_id
    # it really is undeclared: observed traffic, no DEPENDS_ON
    obs = await rows(
        coupled.world,
        "MATCH (a:Service {id: $a}), (b:Service {id: $b}) "
        "RETURN size([(a)-[:DEPENDS_ON]->(b) | 1]), "
        "[(a)-[o:OBSERVED_CALL]->(b) | o.count]",
        {"a": coupled.caller, "b": coupled.callee},
    )
    assert obs[0][0] == 0 and max(obs[0][1]) >= OBSERVED_CALL_THRESHOLD


async def test_resolution_written_into_the_session_world(coupled):
    f = coupled.faults[0]
    root = f.answer.root_id
    world = coupled.world
    [[status, resolved_at, opened_at]] = await rows(
        world,
        "MATCH (i:Incident {id: $iid}) RETURN i.status, i.resolved_at, i.opened_at",
        {"iid": f.incident_id},
    )
    assert status == "resolved" and resolved_at > opened_at == f.alert_time
    caused = await rows(
        world, "MATCH (:Incident {id: $iid})-[:CAUSED_BY]->(c) RETURN c.id", {"iid": f.incident_id}
    )
    assert [r[0] for r in caused] == [root]
    [[rb]] = await rows(
        world,
        "MATCH (:Incident {id: $iid})-[:REMEDIATED_BY]->(rb:Change)-[:PREVIOUS_VERSION]->"
        "(root:Change {id: $root}) MATCH (rb)-[:CHANGES]->(s)<-[:CHANGES]-(root) "
        "RETURN properties(rb)",
        {"iid": f.incident_id, "root": root},
    )
    before = coupled.root_after_first
    assert rb["kind"] == before["kind"] and rb["value"] == (before["prev_value"] or "")
    assert rb["prev_value"] == before["value"] and rb["valid_from"] == resolved_at
    # still current, or superseded by the next fault injected on the same version chain
    assert rb["valid_to"] > rb["valid_from"] and rb["summary"].startswith("Rollback ")
    assert before["valid_to"] == resolved_at  # the bad change ends at the rollback
    [[pm, has_vec, title]] = await rows(
        world,
        "MATCH (pm:Postmortem)-[:ABOUT]->(:Incident {id: $iid}) "
        "RETURN pm.id, pm.embedding IS NOT NULL, pm.title",
        {"iid": f.incident_id},
    )
    assert pm and has_vec and title
    # never the seed world
    seed = await rows(naming.seed_world(), "MATCH ()-[l:LEARNED_DEPENDS_ON]->() RETURN count(l)")
    assert seed[0][0] == 0


async def test_memory_event_shape_and_state(coupled):
    run = coupled.sess.history[-2]
    [mem] = run.of(EventType.MEMORY)
    assert isinstance(mem["written"], list) and mem["written"]
    assert isinstance(mem["detail"], str) and coupled.caller in mem["detail"]
    assert mem["cause_id"] == coupled.faults[0].answer.root_id
    assert "root_id" not in mem  # answer-key names never appear in events (judge leak test)
    assert [e["action"] for e in mem["learned"]][:1] == ["created"]
    states = [d["state"] for d in run.of(EventType.STATE)]
    assert states[-2:] == ["RESOLVED", "LEARNED"]
    types = run.types
    assert types.index("memory") > max(i for i, t in enumerate(types) if t == "action")
    lg = naming.ledger(coupled.sid)
    assert await st.get_state(lg, coupled.faults[0].incident_id) == st.State.LEARNED


async def test_second_alert_resolves_in_fewer_tool_calls(coupled):
    """F3: memory seeds the first hypothesis; the proofs are all still required."""
    first, second = coupled.rows
    assert second["verdict"] == "DIAGNOSED" and second["correct"]
    assert second["recall_used"] and second["memory_seeded"]
    assert second["tool_calls"] < first["tool_calls"]
    assert coupled.faults[1].incident_id != coupled.faults[0].incident_id
    run = coupled.sess.history[-1]
    v = run.of(EventType.VERDICT)[0]
    assert v["tool_calls"] == second["tool_calls"] and v["steps"] == 0
    assert v["memory"]["seed_path"] and coupled.faults[1].answer.root_id in v["memory"]["seeds"]
    kinds = {
        (p["kind"], p["state"])
        for p in run.of(EventType.PROOF)
        if p["suspect"] == coupled.faults[1].answer.root_id
    }
    assert {(k, "HOLDS") for k in wb.REQUIRED_KINDS} <= kinds
    # the second resolution confirmed the same edge: c + (1 - c) * 0.5
    e = await edge(coupled.world, coupled.caller, coupled.callee)
    assert e["confidence"] == pytest.approx(0.6 + 0.4 * 0.5)
    assert e["confirmations"] == 1


async def test_learned_edges_are_never_proof_premises(coupled):
    lg = naming.ledger(coupled.sid)
    proofs = await rows(lg, "MATCH (p:Proof) RETURN p.cypher, p.nodes_touched, p.graph")
    assert proofs
    for cypher, _, graph in proofs:
        assert "LEARNED" not in json.dumps(cypher)
        assert graph.startswith(f"s:{coupled.sid}:asof:")
    # and the producing code never reads them: only recall (advisory) and the write-back
    for path in [
        *(SRC / "tools").glob("*.py"),
        SRC / "proof" / "ledger.py",
        SRC / "graph" / "snapshots.py",
    ]:
        if path.name != "recall.py":
            assert "LEARNED_DEPENDS_ON" not in path.read_text(), path


async def test_snapshots_keep_learned_edges_but_never_derive_impacts(coupled):
    """In incident 2's as-of snapshot the learned edge is present, and every IMPACTS hop
    between services is backed by DEPENDS_ON or an above-threshold OBSERVED_CALL."""
    snap = naming.asof(coupled.sid, coupled.faults[1].incident_id, "known")
    assert (await rows(snap, "MATCH ()-[l:LEARNED_DEPENDS_ON]->() RETURN count(l)"))[0][0] >= 1
    unbacked = await rows(
        snap,
        "MATCH (b:Service)-[:IMPACTS]->(a:Service) "
        "WHERE NOT (a)-[:DEPENDS_ON]->(b) "
        "AND size([(a)-[o:OBSERVED_CALL]->(b) WHERE o.count >= $thr | 1]) = 0 "
        "RETURN count(*)",
        {"thr": OBSERVED_CALL_THRESHOLD},
    )
    assert unbacked[0][0] == 0


async def test_learned_only_pair_gets_no_impacts(sid_graphs):
    """A learned edge with nothing else behind it survives the snapshot and stays inert."""
    src, dst = sid_graphs
    await client.query(
        src,
        "CREATE (:Service {id: 'a'})-[:LEARNED_DEPENDS_ON {confidence: 0.9}]->(:Service {id: 'b'})",
    )
    await build_view(src, dst, 100, "known")
    kept = await rows(dst, "MATCH (:Service {id:'a'})-[l:LEARNED_DEPENDS_ON]->() RETURN count(l)")
    impacts = await rows(dst, "MATCH ()-[e:IMPACTS]->() RETURN count(e)")
    assert kept[0][0] == 1 and impacts[0][0] == 0


async def test_verify_still_holds_after_write_back(coupled):
    report = await verify_incident(coupled.sid, coupled.faults[0].incident_id, coupled.sess.store)
    assert report.status in ("VERIFIED", "DRIFTED")
    assert report.status == "VERIFIED"


# --- confirm / contradict arithmetic ----------------------------------------------------


@pytest.fixture
async def sid_graphs():
    client.reset_client()
    sid = naming.new_session_id()
    yield naming.session_world(sid), naming.asof(sid, "x", "known")
    await drop_session(sid)
    client.reset_client()


async def test_confirmations_approach_but_never_exceed_one(sid_graphs):
    world, _ = sid_graphs
    await client.query(world, "CREATE (:Service {id: 'a'}), (:Service {id: 'b'})")
    first = await wb.confirm_edge(world, "a", "b", "inc-0", 1)
    assert first["action"] == "created" and first["confidence"] == pytest.approx(0.6)
    again = await wb.confirm_edge(world, "a", "b", "inc-0", 2)  # same incident: no change
    assert again["confidence"] == pytest.approx(0.6) and again["confirmations"] == 0
    prev = 0.6
    for n in range(1, 61):
        e = await wb.confirm_edge(world, "a", "b", f"inc-{n}", 2 + n)
        assert e["action"] == "confirmed" and e["confirmations"] == n
        assert prev <= e["confidence"] <= 1.0
        if n <= 10:
            assert e["confidence"] == pytest.approx(prev + (1 - prev) * 0.5)
            assert e["confidence"] < 1.0
        prev = e["confidence"]
    count = await rows(world, "MATCH ()-[l:LEARNED_DEPENDS_ON]->() RETURN count(l)")
    assert count[0][0] == 1  # one MERGE per update, never a duplicate edge


async def test_contradiction_halves_confidence(sid_graphs):
    world, _ = sid_graphs
    await client.query(world, "CREATE (:Service {id: 'a'}), (:Service {id: 'b'})")
    await wb.confirm_edge(world, "a", "b", "inc-1", 1)
    e = await wb.contradict_edge(world, "a", "b", "inc-2", 2)
    assert e["action"] == "contradicted" and e["confidence"] == pytest.approx(0.3)
    assert e["contradictions"] == 1
    e = await wb.contradict_edge(world, "a", "b", "inc-2", 3)  # once per incident
    assert e["confidence"] == pytest.approx(0.3) and e["contradictions"] == 1
    e = await wb.contradict_edge(world, "a", "b", "inc-3", 4)
    assert e["confidence"] == pytest.approx(0.15)


def test_plan_updates_confirms_on_path_and_contradicts_off_path():
    hops = [
        {"caller": "api", "callee": "db", "declared": True, "observed": True},
        {"caller": "web", "callee": "api", "declared": False, "observed": True},  # hidden
    ]
    existing = {("web", "api"), ("cache", "db")}  # both ends symptomatic this time
    confirm, contradict = wb.plan_updates(hops, existing)
    assert confirm == [("web", "api")]
    assert contradict == [("cache", "db")]  # its ends hurt, the cleared path skipped it
    assert wb.plan_updates(hops, set()) == ([("web", "api")], [])


# --- never after INCONCLUSIVE / REFUSED / FAILED ------------------------------------------


@pytest.mark.parametrize("state", ["INCONCLUSIVE", "REFUSED", "FAILED"])
async def test_no_write_back_from_blocked_states(sid_graphs, state):
    world, _ = sid_graphs
    sid = world.split(":")[1]
    await client.query(
        naming.ledger(sid), "CREATE (:LedgerIncident {id: 'inc-1', state: $s})", {"s": state}
    )
    await client.query(world, "CREATE (:Incident {id: 'inc-1', opened_at: 1})")
    for trigger in ("resolved", "reveal"):
        with pytest.raises(wb.WriteBackRefused):
            await wb.write_back(sid=sid, iid="inc-1", resolved_at=5, trigger=trigger)
    assert await wb.write_back_after_reveal(sid=sid, iid="inc-1", match=True, resolved_at=5) is None
    left = await rows(world, "MATCH (i:Incident {id: 'inc-1'}) RETURN i.status, i.memory_written")
    assert left == [[None, None]]


async def test_resolved_without_cleared_rehearsal_is_refused(sid_graphs):
    world, _ = sid_graphs
    sid = world.split(":")[1]
    lg = naming.ledger(sid)
    await client.query(lg, "CREATE (:LedgerIncident {id: 'inc-1', state: 'RESOLVED'})")
    with pytest.raises(wb.WriteBackRefused, match="CLEARED"):
        await wb.write_back(sid=sid, iid="inc-1", resolved_at=5)
    await client.query(
        lg,
        "MATCH (li:LedgerIncident {id: 'inc-1'}) "
        "CREATE (:Rehearsal {result: 'REFUSED', at: 1})-[:FOR]->(li)",
    )
    with pytest.raises(wb.WriteBackRefused, match="CLEARED"):
        await wb.write_back(sid=sid, iid="inc-1", resolved_at=5)
