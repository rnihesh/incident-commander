"""verify_claim (R1, R2): one claimed root cause checked with the agent's own predicate on
the incident's snapshot, built from the hand-built world (see fixture_world)."""

import pytest
from fixture_world import SNAP_T, T0, write_world

from ic.graph import client, naming
from ic.graph.client import query
from ic.graph.snapshots import build_snapshots
from ic.tools.registry import ToolContext, call_tool, get_tool
from ic.tools.result import ToolError, ToolResult


@pytest.fixture(scope="module")
async def snaps(module_sid):
    world = naming.session_world(module_sid)
    await write_world(world)
    h = await build_snapshots(world, module_sid, "fx", SNAP_T)
    return module_sid, world, h


async def check(snaps, incident_id: str, entity_id: str) -> ToolResult:
    sid, world, h = snaps
    ctx = ToolContext(
        sid=sid,
        iid="fx",
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
    )
    return await call_tool(
        "verify_claim", {"incident_id": incident_id, "entity_id": entity_id}, ctx
    )


def decided_by(r: ToolResult) -> str:
    """The query that decided the verdict, checked against the evidence list."""
    q = r.result["query"]
    assert 0 <= q < len(r.cypher)
    assert r.result["query_text"] == r.cypher[q]
    return r.cypher[q]


async def test_true_root_is_verified_with_no_rivals(snaps):
    """AE1: the real root passes every clause and nothing else qualifies."""
    r = await check(snaps, "inc-main", "c-bad")
    res = r.result
    assert res["verdict"] == "VERIFIED"
    assert res["clause"] is None and res["rivals"] == []
    assert res["claim"]["ok"] is True and res["claim"]["entity"] == "Change"
    assert "failed_clause" in decided_by(r)  # the predicate query itself
    assert r.tool == "verify_claim" and r.nodes_touched and len(r.params) == len(r.cypher)


async def test_late_change_is_refuted_after_onset(snaps):
    r = await check(snaps, "inc-main", "c-late")
    res = r.result
    assert res["verdict"] == "REFUTED" and res["clause"] == "after_onset"
    assert str(T0 + 150) in res["detail"]
    assert "failed_clause" in decided_by(r)
    assert [x["id"] for x in res["rivals"]] == ["c-bad"]  # what does qualify


async def test_change_on_healthy_service_is_refuted_on_origin(snaps):
    r = await check(snaps, "inc-main", "c-disk")
    assert r.result["verdict"] == "REFUTED" and r.result["clause"] == "origin"
    assert "disk" in r.result["detail"]
    decided_by(r)


async def test_expiry_claims_are_checked_too(snaps):
    r = await check(snaps, "inc-exp", "x-cert")
    assert r.result["verdict"] == "VERIFIED" and r.result["claim"]["entity"] == "Expiry"
    r = await check(snaps, "inc-main", "x-cert")
    assert r.result["verdict"] == "REFUTED" and r.result["clause"] == "signature"


async def test_two_qualifying_candidates_are_rivals(snaps):
    for claim, rival in (("c-bad", "c-cfg"), ("c-cfg", "c-bad")):
        r = await check(snaps, "inc-two", claim)
        assert r.result["verdict"] == "RIVAL", claim
        assert r.result["clause"] is None
        assert [x["id"] for x in r.result["rivals"]] == [rival]
        assert {"id", "entity", "kind", "service_id", "event_time"} <= set(r.result["rivals"][0])
        assert rival in r.nodes_touched


async def test_change_outside_the_window_is_refuted_like_the_funnel(snaps):
    """c-old would pass every clause, but the funnel cuts it before the predicate runs."""
    r = await check(snaps, "inc-main", "c-old")
    assert r.result["verdict"] == "REFUTED" and r.result["clause"] == "outside_window"
    assert "window" in r.result["detail"]
    assert "valid_from" in decided_by(r)  # the windowed candidates query


async def test_service_claim_is_refuted_as_not_a_change(snaps):
    r = await check(snaps, "inc-main", "db")
    res = r.result
    assert res["verdict"] == "REFUTED" and res["clause"] == "not_a_change"
    assert res["claim"] is None
    assert [x["id"] for x in res["rivals"]] == ["c-bad"]
    assert "Service" in decided_by(r)


async def test_unknown_ids_are_tool_errors(snaps):
    with pytest.raises(ToolError, match="no Change or Expiry or Service"):
        await check(snaps, "inc-main", "chg-nope")
    with pytest.raises(ToolError, match="not in this snapshot"):
        await check(snaps, "inc-nope", "c-bad")


async def _counts(graphs: list[str]) -> dict:
    out = {}
    for g in graphs:
        n = await query(g, "MATCH (n) RETURN count(n)", None, read_only=True)
        e = await query(g, "MATCH ()-[r]->() RETURN count(r)", None, read_only=True)
        out[g] = (n.result_set[0][0], e.result_set[0][0])
    return out


async def test_verify_claim_writes_nothing(snaps):
    """R2: no proofs, ledger, memory or graph changes; every query is read-only."""
    sid = snaps[0]
    assert get_tool("verify_claim").read_only is True
    db = client.get_db()
    graphs = sorted(g for g in await db.list_graphs() if g.startswith(f"s:{sid}:"))
    before = await _counts(graphs)
    for inc, claim in (("inc-main", "c-bad"), ("inc-main", "c-late"), ("inc-two", "c-cfg")):
        await check(snaps, inc, claim)
    after_graphs = sorted(g for g in await db.list_graphs() if g.startswith(f"s:{sid}:"))
    assert after_graphs == graphs  # no ledger or scratch graph appeared
    assert not any(g == naming.ledger(sid) for g in after_graphs)
    assert await _counts(graphs) == before
