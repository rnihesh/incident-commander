"""Query X-ray (U4, R6, R7): FalkorDB's plan and live profile for a funnel cut or a proof,
re-derived server-side from the incident's stored run events. Never accepts Cypher."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from api_helpers import http, inject, sid_of, stream
from redis.exceptions import ConnectionError as RedisConnectionError

from ic.api import routes, routes_judge
from ic.graph import client, naming
from ic.world.causality import CLAUSES

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def finished(ic_app, ic_state):
    c = http(ic_app, "10.9.0.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    yield c, body, sid_of(c), events
    await c.aclose()


def _walk(node):
    yield node
    for ch in node["children"]:
        yield from _walk(ch)


def _above(tree, upper: str, lower: str) -> bool:
    return any(
        any(d["name"].startswith(lower) for d in _walk(n) if d is not n)
        for n in _walk(tree)
        if n["name"] == upper
    )


async def test_funnel_xray_explains_and_profiles_the_exact_query(finished):
    c, body, sid, events = finished
    iid = body["incident_id"]
    funnel = {e["data"]["clause"]: e["data"] for e in events if e["type"] == "funnel"}
    assert set(funnel) == set(CLAUSES)
    for clause in CLAUSES:
        r = await c.post(f"/api/incidents/{iid}/xray", json={"funnel": clause})
        assert r.status_code == 200, r.text
        x = r.json()
        assert x["step"] == {"funnel": clause}
        assert x["graph"] == "as-of snapshot (as known)" and sid not in r.text
        assert x["cypher"] == funnel[clause]["cypher"]  # the query the cut showed
        ex, pr = x["explain"], x["profile"]
        assert ex["name"] and all(n["records"] is None for n in _walk(ex))
        assert all(isinstance(n["records"], int) for n in _walk(pr))
        assert all(isinstance(n["ms"], float) for n in _walk(pr))
        assert [n["name"] for n in _walk(ex)] == [n["name"] for n in _walk(pr)]


async def test_proof_xray_uses_the_proofs_query(finished):
    c, body, sid, events = finished
    iid = body["incident_id"]
    proofs = [e["data"] for e in events if e["type"] == "proof"]
    assert proofs
    for p in proofs:
        seq = int(p["proof_id"].rsplit("-", 1)[1])
        r = await c.post(f"/api/incidents/{iid}/xray", json={"proof": seq})
        assert r.status_code == 200, r.text
        x = r.json()
        assert x["step"] == {"proof": seq}
        assert x["cypher"] == p["query"] and x["params"] == p["params"]
        assert any(n["records"] is not None for n in _walk(x["profile"]))


@pytest.mark.parametrize(
    "payload",
    [
        {"funnel": "origin", "cypher": "MATCH (n) DETACH DELETE n"},
        {"cypher": "MATCH (n) RETURN n"},
        {"proof": 1, "params": {"x": 1}},
        {"funnel": "origin", "proof": 1},
        {},
        {"proof": 0},
        {"funnel": "origin) RETURN 1 //"},
    ],
)
async def test_xray_rejects_raw_cypher_and_malformed_bodies(finished, payload):
    c, body, _, _ = finished
    r = await c.post(f"/api/incidents/{body['incident_id']}/xray", json=payload)
    assert r.status_code == 422, r.text


async def test_xray_404s_unknown_steps_and_other_sessions(finished, ic_app):
    c, body, _, _ = finished
    iid = body["incident_id"]
    for payload in ({"funnel": "outside_window"}, {"funnel": "bogus"}, {"proof": 999}):
        r = await c.post(f"/api/incidents/{iid}/xray", json=payload)
        assert r.status_code == 404, (payload, r.text)
    r = await c.post("/api/incidents/inc-nope/xray", json={"funnel": "origin"})
    assert r.status_code == 404
    async with http(ic_app, "10.9.0.2") as other:
        r = await other.post(f"/api/incidents/{iid}/xray", json={"funnel": "origin"})
        assert r.status_code == 401  # no session at all
        assert (await other.post("/api/session", json={})).status_code == 200
        r = await other.post(f"/api/incidents/{iid}/xray", json={"funnel": "origin"})
        assert r.status_code == 404, r.text


async def test_xray_timeout_is_a_504_without_hanging(finished, ic_state, monkeypatch):
    c, body, _, _ = finished

    async def slow(*a, **kw):
        raise client.QueryTimeout("GRAPH.PROFILE timed out after 5000 ms")

    ic_state.xray_cache.clear()
    monkeypatch.setattr(routes, "profile", slow)
    t = time.monotonic()
    r = await c.post(f"/api/incidents/{body['incident_id']}/xray", json={"funnel": "origin"})
    assert r.status_code == 504 and "timed out" in r.json()["error"]
    assert time.monotonic() - t < 5


async def test_snapshot_plan_filters_variable_length_paths_after_traversal(finished):
    """The R7 claim on a real as-of snapshot: a valid-time filter on a variable-length IMPACTS
    match is planned as a Filter above the traversal, not applied during it."""
    c, body, sid, _ = finished
    iid = body["incident_id"]
    assert (
        await c.post(f"/api/incidents/{iid}/xray", json={"funnel": "origin"})
    ).status_code == 200
    q = (
        "MATCH p=(u:Service)-[:IMPACTS*1..4]->(s:Service {id: $sid}) "
        "WHERE all(r IN relationships(p) WHERE r.valid_from <= $t) RETURN DISTINCT u.id"
    )
    tree = await client.explain(
        naming.asof(sid, iid, "known"),
        q,
        {"sid": body["alert_service_id"], "t": body["alert_time"]},
    )
    assert _above(tree, "Filter", "Conditional Variable Length Traverse")


async def test_naive_plan_shows_the_filter_above_the_variable_length_traverse(finished):
    """R7 on the product's own surface: the step's query never walks a variable-length path
    (it runs on a materialized snapshot), so the X-ray also plans the naive alternative, a
    valid-time filter on a variable-length walk of the live world, and shows where the
    filter lands."""
    c, body, sid, _ = finished
    r = await c.post(f"/api/incidents/{body['incident_id']}/xray", json={"funnel": "origin"})
    assert r.status_code == 200, r.text
    naive = r.json()["naive"]
    assert set(naive) == {"cypher", "graph_label", "explain"}
    assert naive["cypher"] == routes.NAIVE_XRAY_CYPHER and "*1.." in naive["cypher"]
    assert sid not in naive["graph_label"] and "world" in naive["graph_label"]
    assert all(n["records"] is None for n in _walk(naive["explain"]))  # planned, never run
    assert _above(naive["explain"], "Filter", "Conditional Variable Length Traverse")
    step_ops = [n["name"] for n in _walk(r.json()["explain"])]
    assert "Conditional Variable Length Traverse" not in step_ops


async def test_xray_is_cached_per_step_and_serialized_per_session(finished, ic_state, monkeypatch):
    c, body, sid, _ = finished
    iid = body["incident_id"]
    calls = {"explain": 0, "profile": 0}
    real_explain, real_profile = routes.explain, routes.profile

    async def explain(*a, **kw):
        calls["explain"] += 1
        return await real_explain(*a, **kw)

    async def profile(*a, **kw):
        assert kw.get("planned") is not None  # the plan is not re-EXPLAINed
        calls["profile"] += 1
        await asyncio.sleep(0.2)  # concurrent requests interleave here
        return await real_profile(*a, **kw)

    monkeypatch.setattr(routes, "explain", explain)
    monkeypatch.setattr(routes, "profile", profile)
    ic_state.xray_cache.clear()
    url = f"/api/incidents/{iid}/xray"
    a, b = await asyncio.gather(
        c.post(url, json={"funnel": "origin"}), c.post(url, json={"funnel": "origin"})
    )
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert calls == {"explain": 2, "profile": 1}  # step + naive EXPLAIN, one PROFILE
    assert (await c.post(url, json={"funnel": "origin"})).json() == a.json()
    assert calls["profile"] == 1
    await c.post(url, json={"funnel": "after_onset"})  # another step: its own entry
    assert calls["profile"] == 2

    ic_state.forget_xray(sid, iid)  # what a re-derive does
    assert not any(k[:2] == (sid, iid) for k in ic_state.xray_cache)
    await c.post(url, json={"funnel": "origin"})
    assert calls["profile"] == 3


async def test_xray_unavailable_falkordb_is_a_503_and_bad_plans_a_422(
    finished, ic_state, monkeypatch
):
    c, body, _, _ = finished
    url = f"/api/incidents/{body['incident_id']}/xray"

    async def down(*a, **kw):
        raise RedisConnectionError("Connection refused")

    async def refused(*a, **kw):
        raise ValueError("PROFILE is read-only here; the plan has Drop Index")

    for target, fake, status, text in (
        ("explain", down, 503, "FalkorDB is unavailable"),
        ("_ensure_snapshots", down, 503, "FalkorDB is unavailable"),
        ("profile", refused, 422, "could not plan"),
    ):
        ic_state.xray_cache.clear()
        with monkeypatch.context() as m:
            m.setattr(routes, target, fake)
            r = await c.post(url, json={"funnel": "origin"})
        assert r.status_code == status and text in r.json()["error"], (target, r.text)
    assert not ic_state.xray_cache  # failures are not cached


def _screen_call(cypher: str, graph: str) -> dict:
    return {
        "tool": "screen_candidates",
        "graph": graph,
        "cypher": [cypher],
        "params": [{"q": cypher}],
        "result": {"steps": [{"step": "origin", "queries": [0]}], "verdicts": []},
    }


async def test_funnel_xray_uses_the_newest_runs_last_screening():
    sid, iid = "a" * 32, "inc-001"
    known = naming.asof(sid, iid, "known")
    by_run = [
        ("run-new", _screen_call("NEW FIRST", known)),
        ("run-new", {"tool": "list_symptoms", "result": {}}),
        ("run-new", _screen_call("NEW LAST", known)),
        ("run-old", _screen_call("OLD LAST", known)),
    ]
    runs = SimpleNamespace(incident_events_by_run=lambda *_a: by_run)
    s = SimpleNamespace(runs=runs)
    body = routes.XrayBody(funnel="origin")
    cypher, params, view = routes._xray_step(s, sid, iid, body)
    assert (cypher, params, view) == ("NEW LAST", {"q": "NEW LAST"}, "known")


async def test_rederive_reset_forgets_the_incidents_xrays(finished, ic_state):
    """Before the events-gone test: a reset drops the incident's snapshots and its cached
    X-rays; the next X-ray rebuilds the snapshot and profiles afresh."""
    c, body, sid, _ = finished
    iid = body["incident_id"]
    url = f"/api/incidents/{iid}/xray"
    assert (await c.post(url, json={"funnel": "origin"})).status_code == 200
    assert any(k[:2] == (sid, iid) for k in ic_state.xray_cache)
    await routes_judge._reset_incident(ic_state, sid, iid)
    assert not any(k[:2] == (sid, iid) for k in ic_state.xray_cache)
    r = await c.post(url, json={"funnel": "origin"})
    assert r.status_code == 200, r.text


async def test_xray_404s_when_the_run_events_are_gone(finished, ic_state):
    # Last in the module: deletes this incident's stored events.
    c, body, sid, _ = finished
    iid = body["incident_id"]
    with ic_state.store.conn() as db:
        db.execute(
            "DELETE FROM run_events WHERE run_id IN "
            "(SELECT run_id FROM runs WHERE sid = ? AND incident_id = ?)",
            (sid, iid),
        )
    r = await c.post(f"/api/incidents/{iid}/xray", json={"funnel": "origin"})
    assert r.status_code == 404
    assert "run events" in r.json()["detail"]
