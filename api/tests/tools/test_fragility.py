"""fragility_map (U9, KTD9, R13) on the hand-built world (see fixture_world).

inc-main's root c-bad (deploy on db) is VERIFIED in the as-known snapshot. Its canonical
causal path to the farthest symptom is c-bad > db > api > web:

  db > api   the only IMPACTS route to the symptomatic api (backed by api's DEPENDS_ON and
             its OBSERVED_CALL on db): removing it fails coverage, so the verdict flips
  api > web  web is still reached in three hops through api-b: no flip

A deploy planted on db (the first service to fail, upstream of the nearest eliminated
candidates) qualifies too, so the root becomes a RIVAL; moving c-bad past the onset refutes
it on after_onset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fixture_world import SNAP_T, T0, write_world

from ic.graph import client, naming
from ic.graph.snapshots import build_snapshots
from ic.tools import fragility
from ic.tools.registry import ScopeError, ToolContext, call_tool

ONSET = T0 + 100


@pytest.fixture(scope="module")
async def snaps(module_sid):
    world = naming.session_world(module_sid)
    await write_world(world)
    h = await build_snapshots(world, module_sid, "fx", SNAP_T)
    ledger = naming.ledger(module_sid)
    await client.query(ledger, "CREATE (:Proof {incident_id: 'inc-main', seq: 0})")
    return module_sid, world, h


def ctx_for(snaps, tracked: list[str] | None = None, **kw) -> ToolContext:
    sid, world, h = snaps
    return ToolContext(
        sid=sid,
        iid="fx",
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        ledger_graph=naming.ledger(sid),
        track_graph=tracked.append if tracked is not None else None,
        **kw,
    )


async def run(snaps, incident_id="inc-main", root_id="c-bad", tracked=None, **kw):
    args = {"incident_id": incident_id, "root_id": root_id}
    return await call_tool("fragility_map", args, ctx_for(snaps, tracked, **kw))


async def fingerprint(graph: str) -> tuple:
    """Everything the predicate reads, plus the totals: any write shows up here."""

    async def rows(cypher: str) -> list:
        return (await client.query(graph, cypher, read_only=True)).result_set

    return (
        await rows("MATCH (n) RETURN count(n)"),
        await rows("MATCH ()-[r]->() RETURN count(r)"),
        sorted(map(tuple, await rows("MATCH (a)-[:IMPACTS]->(b) RETURN a.id, b.id"))),
        sorted(map(tuple, await rows("MATCH (c:Change) RETURN c.id, c.valid_from"))),
    )


def by_kind(res: dict, kind: str) -> list[dict]:
    return [e for e in res["tried"] if e["kind"] == kind]


async def test_removing_the_only_causal_hop_flips_and_is_reported(snaps):
    res = (await run(snaps)).result
    assert res["applicable"] is True and res["baseline"]["verdict"] == "VERIFIED"
    assert res["path"] == ["c-bad", "db", "api", "web"]
    hops = {(e["hop"]["from"], e["hop"]["to"]): e for e in by_kind(res, "remove_edge")}
    assert set(hops) == {("db", "api"), ("api", "web")}

    only = hops[("db", "api")]
    assert only["flips"] is True and only["verdict"] == "REFUTED"
    assert only["clause"] == "coverage" and "api" in only["detail"]
    # The judge's tamper names the dependency (caller -> callee); in the world it removes
    # both relationships that materialize this one IMPACTS hop.
    assert only["tamper"] == {
        "action": "remove_edge",
        "from_service_id": "api",
        "to_service_id": "db",
    }
    assert only["prediction"]["sources"] == ["DEPENDS_ON", "OBSERVED_CALL"]
    assert only["prediction"]["exact"] is True

    other = hops[("api", "web")]
    assert other["flips"] is False and other["verdict"] == "VERIFIED"

    assert only in res["flips"] and other not in res["flips"]


async def test_planted_deploy_and_shifted_timestamp_flip_with_tamper_payloads(snaps):
    res = (await run(snaps)).result
    [dep] = by_kind(res, "add_deploy_upstream")
    assert dep["flips"] is True and dep["verdict"] == "RIVAL"
    assert dep["tamper"] == {
        "action": "add_deploy_upstream",
        "service_id": "db",
        "minutes_before_onset": fragility.DEPLOY_MINUTES_BEFORE_ONSET,
        "as_if_known_at_alert": True,
    }
    assert dep["rivals"] == [dep["edit"]["planted_id"]]  # the new qualifier, screened
    assert dep["near"]["service_id"] in ("db", "api")

    [ts] = by_kind(res, "change_timestamp")
    assert ts["flips"] is True and ts["verdict"] == "REFUTED" and ts["clause"] == "after_onset"
    assert ts["tamper"] == {
        "action": "change_timestamp",
        "change_id": "c-bad",
        "new_valid_from": ONSET + 60,
    }
    assert res["earliest_onset"] == ONSET
    assert {e["kind"] for e in res["flips"]} == {
        "remove_edge",
        "add_deploy_upstream",
        "change_timestamp",
    }


async def test_every_copy_is_tracked_deleted_and_capped_at_ten(snaps, monkeypatch):
    tracked: list[str] = []
    res = (await run(snaps, tracked=tracked)).result
    assert 0 < res["copies"] == len(tracked) == len(res["tried"]) <= fragility.MAX_EDITS == 10
    assert all(g.startswith(f"s:{snaps[0]}:storm:") for g in tracked)
    assert len(set(tracked)) == len(tracked)
    assert res["copy_ms"] and all(e["copy_ms"] >= 0 for e in res["tried"])
    left = set(await client.get_db().list_graphs())
    assert not left & set(tracked)

    monkeypatch.setattr(fragility, "MAX_EDITS", 2)
    tracked.clear()
    res = (await run(snaps, tracked=tracked)).result
    assert res["copies"] == len(tracked) == 2 and res["skipped"] == 2
    assert not set(await client.get_db().list_graphs()) & set(tracked)


async def test_copies_are_deleted_when_an_edit_fails(snaps, monkeypatch):
    tracked: list[str] = []
    real = fragility.verify_claim

    async def boom(graph, *a, **k):
        if ":storm:" in graph:  # the baseline on the snapshot runs; the first copy fails
            raise RuntimeError("verify blew up")
        return await real(graph, *a, **k)

    monkeypatch.setattr(fragility, "verify_claim", boom)
    with pytest.raises(RuntimeError, match="blew up"):
        await run(snaps, tracked=tracked)
    assert tracked and not set(await client.get_db().list_graphs()) & set(tracked)


async def test_every_clone_is_deleted_when_a_later_edit_fails(snaps, monkeypatch):
    """Two copies tried, the third fails mid-edit (after its copy exists): none is left."""
    tracked: list[str] = []
    real = fragility.query
    copies = {"n": 0}
    real_copy = fragility.copy_graph

    async def counting_copy(src, dst):
        copies["n"] += 1
        await real_copy(src, dst)

    async def boom(graph, cypher, params=None, **kw):
        if ":storm:" in graph and copies["n"] == 3:
            raise RuntimeError("edit blew up")
        return await real(graph, cypher, params, **kw)

    monkeypatch.setattr(fragility, "copy_graph", counting_copy)
    monkeypatch.setattr(fragility, "query", boom)
    with pytest.raises(RuntimeError, match="edit blew up"):
        await run(snaps, tracked=tracked)
    assert len(tracked) == 3 and copies["n"] == 3
    assert not set(await client.get_db().list_graphs()) & set(tracked)


async def test_wall_clock_budget_stops_early_with_what_was_tried(snaps, monkeypatch):
    full = (await run(snaps)).result
    planned = len(full["tried"]) + full["skipped"]
    real = fragility._try
    seen = {"n": 0}

    async def then_out_of_time(*a, **kw):
        seen["n"] += 1
        out = await real(*a, **kw)
        monkeypatch.setattr(fragility, "BUDGET_S", -1.0)  # the next check is over budget
        return out

    monkeypatch.setattr(fragility, "_try", then_out_of_time)
    tracked: list[str] = []
    res = (await run(snaps, tracked=tracked)).result
    assert seen["n"] == 1 and len(res["tried"]) == res["copies"] == len(tracked) == 1
    assert res["skipped"] == planned - 1 and res["out_of_time"] is True
    assert "time budget" in res["detail"]
    assert not set(await client.get_db().list_graphs()) & set(tracked)


async def test_expiry_root_has_no_timestamp_edit(snaps):
    res = (await run(snaps, incident_id="inc-exp", root_id="x-cert")).result
    assert res["applicable"] is True, res["baseline"]
    assert res["tried"] and "change_timestamp" not in {e["kind"] for e in res["tried"]}
    actions = {(e["tamper"] or {}).get("action") for e in res["tried"]}
    assert "change_timestamp" not in actions


async def test_no_eliminated_candidate_means_no_planted_deploy(sid):
    """A world whose only change in the window is the root: nothing was eliminated, so no
    deploy is planted upstream of one."""
    world = naming.session_world(sid)
    await write_world(world)
    await client.query(world, "MATCH (c:Change) WHERE c.id <> 'c-bad' DETACH DELETE c")
    await client.query(world, "MATCH (e:Expiry) DETACH DELETE e")
    h = await build_snapshots(world, sid, "fx", SNAP_T)
    res = (await run((sid, world, h))).result
    assert res["applicable"] is True and res["baseline"]["rivals"] == []
    kinds = {e["kind"] for e in res["tried"]}
    assert "add_deploy_upstream" not in kinds and {"remove_edge", "change_timestamp"} <= kinds


def _screening(onset: int, t: int | None):
    return SimpleNamespace(facts=SimpleNamespace(earliest_onset=onset, snapshot_t=t))


@pytest.mark.parametrize(
    ("t", "vf"),
    [
        (None, ONSET + fragility.SHIFT_PAST_ONSET_S),  # no snapshot time: the full shift
        (ONSET + fragility.SHIFT_PAST_ONSET_S, ONSET + fragility.SHIFT_PAST_ONSET_S),  # == t
        (ONSET + fragility.SHIFT_PAST_ONSET_S - 1, ONSET),  # past t: clamped to the onset
        (ONSET, ONSET),
    ],
)
def test_timestamp_edit_stays_inside_the_snapshot(t, vf):
    root = SimpleNamespace(entity="Change", id="c-bad")
    e = fragility._timestamp_edit(_screening(ONSET, t), root)
    assert e is not None and e.tamper["new_valid_from"] == vf
    assert e.statements[0][1] == {"id": "c-bad", "vf": vf}
    expiry = SimpleNamespace(entity="Expiry", id="x-cert")
    assert fragility._timestamp_edit(_screening(ONSET, t), expiry) is None


async def test_incident_snapshots_and_ledger_are_unchanged(snaps):
    sid, world, h = snaps
    graphs = [h.true_graph, h.known_graph, world, naming.ledger(sid)]
    before = [await fingerprint(g) for g in graphs]
    await run(snaps)
    await run(snaps, view="true")
    assert [await fingerprint(g) for g in graphs] == before


async def test_no_verified_root_means_no_copies(snaps):
    tracked: list[str] = []
    res = (await run(snaps, incident_id="inc-two", tracked=tracked)).result
    assert res["applicable"] is False and res["baseline"]["verdict"] == "RIVAL"
    assert res["tried"] == [] and res["copies"] == 0 and tracked == []


async def test_needs_a_session_and_stays_off_mcp(snaps):
    from ic import mcp_server

    _, world, h = snaps
    ctx = ToolContext(snapshot_known=h.known_graph, world_graph=world)
    with pytest.raises(ScopeError):
        await call_tool("fragility_map", {"incident_id": "inc-main", "root_id": "c-bad"}, ctx)
    assert "fragility_map" not in {t.name for t in mcp_server.exposed_tools()}
