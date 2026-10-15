"""Investigation tools on snapshots built from the hand-built world (see fixture_world)."""

import re

import pytest
from fixture_world import FUTURE_INCIDENT, SNAP_T, T0, write_world

from ic.graph import naming
from ic.graph.snapshots import build_snapshots
from ic.tools.registry import ToolContext, all_tools, call_tool
from ic.tools.result import ToolError, ToolResult


@pytest.fixture(scope="module")
async def snaps(module_sid):
    world = naming.session_world(module_sid)
    await write_world(world)
    h = await build_snapshots(world, module_sid, "fx", SNAP_T)
    return module_sid, world, h


def ctx_for(snaps, view: str = "known") -> ToolContext:
    sid, world, h = snaps
    return ToolContext(
        sid=sid,
        iid="fx",
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        view=view,
    )


async def call(snaps, name: str, view: str = "known", **args) -> ToolResult:
    return await call_tool(name, args, ctx_for(snaps, view))


async def test_list_symptoms_and_window(snaps):
    r = await call(snaps, "list_symptoms", incident_id="inc-main")
    assert [s["service_id"] for s in r.result["symptoms"]] == ["db", "api", "web"]
    assert r.result["earliest_onset"] == T0 + 100
    r = await call(snaps, "changes_in_window", incident_id="inc-main")
    ids = [c["id"] for c in r.result["candidates"]]
    assert set(ids) == {"c-bad", "x-cert", "c-disk", "c-cfg", "c-api", "c-late"}
    assert "c-unk" not in ids  # recorded after the snapshot time: unknown in the known view
    r = await call(snaps, "changes_in_window", view="true", incident_id="inc-main")
    assert "c-unk" in {c["id"] for c in r.result["candidates"]}


async def test_funnel_cuts_in_clause_order(snaps):
    r = await call(snaps, "screen_candidates", incident_id="inc-main")
    steps = {s["step"]: s for s in r.result["steps"]}
    assert [s["step"] for s in r.result["steps"]] == [
        "outside_window",
        "after_onset",
        "origin",
        "coverage",
        "order",
        "signature",
    ]
    assert steps["outside_window"]["eliminated"] == ["c-old"]
    assert steps["after_onset"]["eliminated"] == ["c-late"]
    assert sorted(steps["origin"]["eliminated"]) == ["c-api", "c-disk"]
    assert sorted(steps["signature"]["eliminated"]) == ["c-cfg", "x-cert"]
    assert [s["remaining"] for s in r.result["steps"]] == [6, 5, 3, 3, 3, 1]
    assert r.result["start"] == 7 and r.result["survivors"] == ["c-bad"]
    for s in r.result["steps"]:  # every cut points at the queries that made it
        assert all(0 <= q < len(r.cypher) for q in s["queries"])
        assert bool(s["queries"]) == bool(s["eliminated"])


async def test_bad_deploy_causal_path_is_canonical(snaps):
    r = await call(snaps, "causal_path", entity_id="c-bad", service_id="web")
    # web is reachable through api and api-b at equal length: the smaller id sequence wins
    assert r.result["path"] == ["c-bad", "db", "api", "web"]
    assert r.result["hops"] == 3
    assert len(r.result["display_path"]) == 4  # algo.SPpaths, any one shortest path
    assert any("allShortestPaths" in c for c in r.cypher)
    assert any("algo.SPpaths" in c for c in r.cypher)
    r = await call(snaps, "causal_path", entity_id="c-bad", service_id="billing")
    assert r.result["found"] is False and r.result["path"] is None


async def test_impact_closure(snaps):
    r = await call(snaps, "impact_closure", entity_id="c-bad")
    # cache only via the undeclared observed call
    assert r.result["reachable_services"] == ["api", "api-b", "cache", "db", "web"]
    r = await call(snaps, "impact_closure", entity_id="disk", max_depth=1)
    assert r.result["reachable_services"] == ["db"]
    assert any("algo.BFS" in c for c in r.cypher)


async def test_expiry_certificate_shows_zero_qualifying_changes(snaps):
    """AE2: the expiry explains it, every checked change is disqualified with a reason,
    and the causal path ends at the expired item."""
    r = await call(snaps, "absence_certificate", incident_id="inc-exp", suspect_id="x-cert")
    res = r.result
    assert res["qualifying"] == [] and res["clean"] is True
    assert res["suspect"]["ok"] is True
    assert res["checked_services"] == ["api", "db", "disk"]  # disk can impact db
    clauses = {v["id"]: v["clause"] for v in res["checked"] if v["entity"] == "Change"}
    assert clauses == {
        "c-bad": "signature",
        "c-cfg": "signature",
        "c-late": "after_onset",
        "c-disk": "origin",
        "c-api": "origin",
    }
    assert all(v["detail"] for v in res["checked"])
    path = await call(snaps, "causal_path", entity_id="x-cert", service_id="api")
    assert path.result["path"] == ["x-cert", "db", "api"]


async def test_two_independent_causes_survive_each_others_removal(snaps):
    """AE1: both qualify; removing either leaves the other as a rival with its path."""
    r = await call(snaps, "screen_candidates", incident_id="inc-two")
    assert sorted(r.result["survivors"]) == ["c-bad", "c-cfg"]
    for suspect, rival in (("c-bad", "c-cfg"), ("c-cfg", "c-bad")):
        n = await call(snaps, "necessity", incident_id="inc-two", suspect_id=suspect)
        assert n.result["necessary"] is False
        assert [x["id"] for x in n.result["rivals"]] == [rival]
        assert n.result["rivals"][0]["path"]["path"] == [rival, "db", "api"]
        assert suspect not in n.result["checked"]  # excluded from the snapshot query
        assert n.params[2]["exclude"] == [suspect]


async def test_single_cause_is_necessary(snaps):
    n = await call(snaps, "necessity", incident_id="inc-main", suspect_id="c-bad")
    assert n.result["necessary"] is True and n.result["rivals"] == []
    assert n.result["suspect"]["ok"] is True


async def test_exonerate(snaps):
    r = await call(snaps, "exonerate", incident_id="inc-main", change_id="c-disk")
    assert r.result["qualifies"] is False
    receipt = r.result["receipt"]
    assert receipt["failing_clause"] == "origin" and "disk" in receipt["detail"]
    assert receipt["checked_services"] == ["api", "db", "web"]
    r = await call(snaps, "exonerate", incident_id="inc-main", change_id="c-bad")
    assert r.result["qualifies"] is True and "receipt" not in r.result
    assert r.result["path"]["path"] == ["c-bad", "db", "api", "web"]
    for missing in ("inc-nope", FUTURE_INCIDENT):  # the future one exists only in the world
        with pytest.raises(ToolError, match="requires a completed incident"):
            await call(snaps, "exonerate", incident_id=missing, change_id="c-bad")


async def test_who_is_on_call_uses_valid_time(snaps):
    before = await call(snaps, "who_is_on_call", service_id="db", at=T0 - 10)
    after = await call(snaps, "who_is_on_call", service_id="db", at=T0 + 10)
    assert [p["person_id"] for p in before.result["on_call"]] == ["per-ann"]
    assert [p["person_id"] for p in after.result["on_call"]] == ["per-bob"]
    assert after.result["owner"]["team_id"] == "team-core"
    assert after.graph == naming.session_world(snaps[0])  # runs on the world, not a snapshot


async def test_observed_calls_flag_undeclared(snaps):
    r = await call(snaps, "observed_calls", service_id="db")
    callers = {c["peer"]: c for c in r.result["calls"] if c["direction"] == "caller"}
    assert callers["cache"]["undeclared"] is True and callers["cache"]["impacts"] is True
    assert callers["api"]["declared"] is True
    assert r.result["undeclared"] == ["cache"]


async def test_seeded_betweenness_is_deterministic(snaps):
    a = await call(snaps, "rank_chokepoints", top_k=3)
    b = await call(snaps, "rank_chokepoints", top_k=3)
    assert a.result == b.result
    assert a.result["chokepoints"][0]["service_id"] == "db"  # everything flows through db
    assert any("algo.betweenness" in c and "samplingSeed" in c for c in a.cypher)


CALLS = {
    "list_symptoms": {"incident_id": "inc-main"},
    "changes_in_window": {"incident_id": "inc-main"},
    "impact_closure": {"entity_id": "c-bad"},
    "causal_path": {"entity_id": "c-bad", "service_id": "web"},
    "screen_candidates": {"incident_id": "inc-main"},
    "absence_certificate": {"incident_id": "inc-main", "suspect_id": "c-bad"},
    "necessity": {"incident_id": "inc-main", "suspect_id": "c-bad"},
    "exonerate": {"incident_id": "inc-main", "change_id": "c-disk"},
    "verify_claim": {"incident_id": "inc-main", "entity_id": "c-late"},
    "who_is_on_call": {"service_id": "db", "at": T0},
    "observed_calls": {"service_id": "db"},
    "rank_chokepoints": {"top_k": 3},
    "reflex": {"alert_service_id": "db", "t": T0 + 150},
    "find_precedents": {"incident_id": "inc-main", "suspect_id": "c-bad"},
    "rehearse_rollback": {"incident_id": "inc-main", "change_id": "c-bad"},
    "recall": {"incident_id": "inc-main"},
    "fragility_map": {"incident_id": "inc-main", "root_id": "c-bad"},
    "find_entities": {"search": "db"},
    "service_profile": {"service_id": "db"},
    "team_profile": {"team_id": "team-core"},
    "person_profile": {"person_id": "per-bob"},
    "decisions_for": {"entity_id": "db"},
    "incident_history": {"entity_id": "db"},
}


async def test_every_tool_returns_evidence_with_domain_ids_only(snaps):
    # Ledger tools (test_hypothesis) need an opened ledger incident: see tests/proof.
    # collapse_storm needs an injected storm: see tests/tools/test_storm.py.
    tools = {t.name for t in all_tools() if t.target != "ledger"} - {"collapse_storm"}
    assert set(CALLS) == tools
    for name, args in CALLS.items():
        r = await call(snaps, name, **args)
        assert r.tool == name
        assert r.cypher and all(c.strip() for c in r.cypher), name
        assert len(r.params) == len(r.cypher) and any(r.params), name
        assert r.nodes_touched, name
        assert not [n for n in r.nodes_touched if re.fullmatch(r"\d+", str(n))], name
        assert r.ms > 0 and r.graph, name
