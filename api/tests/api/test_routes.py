"""HTTP routes (U9): sessions and limits, content type, signed alerts, verify, exonerate,
recorded-run fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace

import pytest
from api_helpers import SECRET, cleanup_store_graphs, http, inject, sid_of, stream, test_settings
from starlette.requests import Request

from ic.actions import pr_store
from ic.api import auth, graph_view, maintenance, routes
from ic.api.app import create_app
from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.runs.recorded import (
    REDACTED_SID,
    hero_graph_path,
    load_hero,
    load_hero_graph,
    record_hero,
)
from ic.store import Store
from ic.world.faults import InjectionError, inject_fault

# --- sessions, cookies, content type ----------------------------------------------------


async def test_session_cookie_is_issued_and_reused(ic_app):
    async with http(ic_app, "10.2.0.1") as c:
        r = await c.post("/api/session", json={})
        assert r.status_code == 200 and r.json()["created"] is True
        cookie = r.headers["set-cookie"].lower()
        assert "ic_sid=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert "secure" not in cookie  # disabled for the plain-http test settings
        sid = sid_of(c)
        assert len(sid) == 32 and int(sid, 16) >= 0
        r = await c.post("/api/session", json={})
        assert r.json()["created"] is False and sid_of(c) == sid


def test_cookie_is_secure_and_scoped_by_default(tmp_path):
    from fastapi.responses import JSONResponse

    resp = JSONResponse({})
    s = test_settings(tmp_path, cookie_secure=True, cookie_domain=".example.com")
    auth.set_session_cookie(resp, "a" * 32, s)
    cookie = resp.headers["set-cookie"].lower()
    assert "secure" in cookie and "domain=.example.com" in cookie and "httponly" in cookie


@pytest.mark.parametrize("cookie", ["not-a-session", "../../etc", "a" * 32, "A" * 32])
async def test_malformed_or_unknown_cookie_starts_a_new_session(ic_app, ic_state, cookie):
    async with http(ic_app, f"10.2.1.{len(cookie) % 200 + 1}") as c:
        c.cookies.set(auth.COOKIE, cookie)
        r = await c.post("/api/session", json={})
        assert r.status_code == 200 and r.json()["created"] is True
        new = r.cookies[auth.COOKIE]
        assert new != cookie and ic_state.sessions.get(new) is not None


@pytest.mark.parametrize(
    "path",
    [
        "/api/session",
        "/api/inject",
        "/api/alerts",
        "/api/incidents/inc-001/verify",
        "/api/incidents/inc-001/exonerate",
    ],
)
@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_state_changing_routes_require_json(ic_app, path, ctype):
    async with http(ic_app, "10.2.2.1") as c:
        headers = {"content-type": ctype} if ctype else {}
        r = await c.post(path, content=b'{"fault_class": "bad_deploy"}', headers=headers)
        assert r.status_code == 415


async def test_per_ip_session_limit_serves_recorded_run(ic_app):
    """A cookie-less retry loop from one IP is refused after two concurrent sessions."""
    async with http(ic_app, "10.2.3.1") as c:
        codes = []
        for _ in range(3):
            c.cookies.clear()
            codes.append((await c.post("/api/session", json={})).status_code)
        assert codes[:2] == [200, 200]
        c.cookies.clear()
        r = await c.post("/api/session", json={})
        assert r.status_code == 429
        assert r.json()["fallback"] == "recorded_run" and "per client" in r.json()["reason"]


async def test_server_at_capacity_returns_503_with_fallback(tmp_path):
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    app = create_app(test_settings(tmp_path, max_sessions=1), llm=None, store=store)
    try:
        async with http(app, "10.3.0.1") as a, http(app, "10.3.0.2") as b:
            assert (await a.post("/api/session", json={})).status_code == 200
            r = await b.post("/api/session", json={})
            assert r.status_code == 503
            assert r.json()["fallback"] == "recorded_run" and "capacity" in r.json()["reason"]
            r = await inject(b)  # inject without a session hits the same wall
            assert r.status_code == 503
    finally:
        from api_helpers import cleanup_store_graphs

        await cleanup_store_graphs(store)


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "client": (peer, 1), "headers": headers})


def test_client_ip_trusts_forwarded_for_only_from_trusted_proxies(tmp_path):
    s = test_settings(tmp_path)  # trusted_proxies = ["127.0.0.1"]
    assert auth.client_ip(_request("203.0.113.9", "1.2.3.4"), s) == "203.0.113.9"
    assert auth.client_ip(_request("127.0.0.1", "1.2.3.4"), s) == "1.2.3.4"
    # The left-most entry is client-controlled: the right-most untrusted hop wins.
    assert auth.client_ip(_request("127.0.0.1", "6.6.6.6, 1.2.3.4"), s) == "1.2.3.4"
    assert auth.client_ip(_request("127.0.0.1", "1.2.3.4, 127.0.0.1"), s) == "1.2.3.4"
    assert auth.client_ip(_request("127.0.0.1", "not-an-ip"), s) == "127.0.0.1"
    assert auth.client_ip(_request("127.0.0.1"), s) == "127.0.0.1"


def test_signature_check_is_strict():
    body = b'{"a": 1}'
    good = auth.sign(body, SECRET)
    assert auth.verify_signature(body, good, SECRET)
    assert not auth.verify_signature(body + b" ", good, SECRET)
    assert not auth.verify_signature(body, good.removeprefix("sha256="), SECRET)
    assert not auth.verify_signature(body, auth.sign(body, "other"), SECRET)
    assert not auth.verify_signature(body, good, None)  # no secret configured: reject all
    assert not auth.verify_signature(body, None, SECRET)


# --- runs and limits --------------------------------------------------------------------


async def test_fourth_run_in_an_hour_is_refused_with_recorded_fallback(ic_app):
    async with http(ic_app, "10.4.0.1") as c:
        for seed in (21, 22, 23):
            r = await inject(c, "config_change", seed)
            assert r.status_code == 200, r.text
            _, events = await stream(c, r.json()["run_id"])  # wait for it to finish
            assert events[-1]["type"] == "done"
        r = await inject(c, "config_change", 24)
        assert r.status_code == 429
        body = r.json()
        assert body["fallback"] == "recorded_run" and "3 runs per hour" in body["reason"]
        assert body["recorded_url"] == "/api/recorded/hero"


async def test_concurrent_run_in_one_session_is_refused(ic_app, ic_state):
    async with http(ic_app, "10.4.1.1") as c:
        await c.post("/api/session", json={})
        ic_state.sessions.run_started(sid_of(c))  # a run is in flight
        try:
            r = await inject(c)
            assert r.status_code == 429 and "in progress" in r.json()["reason"]
        finally:
            ic_state.sessions.run_finished(sid_of(c))


async def test_unknown_fault_class_is_rejected(ic_app):
    async with http(ic_app, "10.4.2.1") as c:
        r = await c.post("/api/inject", json={"fault_class": "rm_rf"})
        assert r.status_code == 422


# --- alert webhook ----------------------------------------------------------------------


async def test_alert_webhook_requires_a_valid_signature_and_starts_a_run(ic_app, ic_state):
    async with http(ic_app, "10.5.0.1") as c:
        await c.post("/api/session", json={})
        sid = sid_of(c)
        f = await inject_fault(
            naming.session_world(sid),
            session_id=sid,
            fault_class="bad_deploy",
            rng_seed=5,
            store=ic_state.store,
        )
        body = json.dumps({"session_id": sid, "incident_id": f.incident_id}).encode()
        hdr = {"content-type": "application/json"}

        r = await c.post("/api/alerts", content=body, headers=hdr)
        assert r.status_code == 401
        bad = {**hdr, "X-IC-Signature": auth.sign(body, "wrong-secret")}
        assert (await c.post("/api/alerts", content=body, headers=bad)).status_code == 401
        tampered = body.replace(b"inc-", b"inc-9")
        sig = {**hdr, "X-IC-Signature": auth.sign(body, SECRET)}
        assert (await c.post("/api/alerts", content=tampered, headers=sig)).status_code == 401

        r = await c.post("/api/alerts", content=body, headers=sig)
        assert r.status_code == 202, r.text
        j = r.json()
        assert j["alert_time"] == f.alert_time and j["alert_service_id"] == f.alert_service_id
        _, events = await stream(c, j["run_id"])
        verdict = next(e["data"] for e in events if e["type"] == "verdict")
        assert verdict["outcome"] == "DIAGNOSED"
        assert verdict["root_cause"]["change"] == f.answer.root_id
        assert events[-1]["type"] == "done"

        # Counted against the session's limits; a finished incident is not re-run.
        r = await c.post("/api/alerts", content=body, headers=sig)
        assert r.status_code == 409

        unknown = json.dumps({"session_id": "f" * 32, "incident_id": f.incident_id}).encode()
        r = await c.post(
            "/api/alerts",
            content=unknown,
            headers={**hdr, "X-IC-Signature": auth.sign(unknown, SECRET)},
        )
        assert r.status_code == 404


# --- verify and exonerate ---------------------------------------------------------------


@pytest.fixture(scope="module")
async def finished(ic_app, ic_state):
    c = http(ic_app, "10.6.0.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    sid = sid_of(c)
    answer = ic_state.store.get_answer(body["incident_id"], sid)
    yield c, body, sid, answer, events
    await c.aclose()


async def test_exonerate_endpoint_runs_on_the_incident_snapshot(finished):
    c, body, sid, answer, events = finished
    iid = body["incident_id"]
    r = await c.post(f"/api/incidents/{iid}/exonerate", json={"change_id": answer["root_id"]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["tool"] == "exonerate" and res["result"]["qualifies"] is True
    assert res["graph"] == naming.asof(sid, iid, "known") and res["cypher"]
    decoy = next(
        e["change"]
        for f in (x["data"] for x in events if x["type"] == "funnel")
        for e in f["eliminated"]
    )
    # Snapshots gone (e.g. cleaned up): they are rebuilt on demand.
    for v in naming.VIEWS:
        await client.delete_graph(naming.asof(sid, iid, v))
    r = await c.post(f"/api/incidents/{iid}/exonerate", json={"change_id": decoy})
    assert r.status_code == 200 and r.json()["result"]["qualifies"] is False
    assert await client.graph_exists(naming.asof(sid, iid, "known"))
    r = await c.post(f"/api/incidents/{iid}/exonerate", json={"change_id": "x' OR 1=1"})
    assert r.status_code == 422


async def test_graph_route_shape_flags_and_scoping(finished, ic_app):
    c, body, sid, answer, _ = finished
    iid = body["incident_id"]
    r = await c.get(f"/api/incidents/{iid}/graph")
    assert r.status_code == 200, r.text
    g = r.json()
    assert set(g) == {"incident_id", "t", "alert_service_id", "nodes", "edges", "changes"}
    assert g["incident_id"] == iid and g["alert_service_id"] == body["alert_service_id"]
    assert g["t"] == body["alert_time"]
    nodes = {n["id"]: n for n in g["nodes"]}
    assert 0 < len(nodes) <= 400 and len(nodes) == len(g["nodes"])
    for n in g["nodes"]:
        assert set(n) == {
            "id", "name", "region", "tier", "x", "y",
            "symptomatic", "onset", "signature", "alerting",
        }  # fmt: skip
        assert n["x"] is not None and n["y"] is not None
        assert (n["onset"] is not None) == n["symptomatic"]
    assert [n["id"] for n in g["nodes"] if n["alerting"]] == [body["alert_service_id"]]
    assert nodes[body["alert_service_id"]]["symptomatic"] is True
    assert answer["root_service"] in nodes if "root_service" in answer else True
    assert g["edges"] and all(e["source"] in nodes and e["target"] in nodes for e in g["edges"])
    assert set(g["edges"][0]) == {"source", "target"}
    sym = (
        await client.query(
            naming.session_world(sid),
            "MATCH (:Incident {id: $i})-[:HAS]->(y:Symptom) RETURN DISTINCT y.service_id",
            {"i": iid},
            read_only=True,
        )
    ).result_set
    assert {n["id"] for n in g["nodes"] if n["symptomatic"]} == {r_[0] for r_ in sym}
    for ch in g["changes"]:
        assert set(ch) == {"id", "service_id", "kind", "valid_from", "recorded_at"}
        assert ch["service_id"] in nodes and g["t"] - 7200 <= ch["valid_from"] <= g["t"]
    assert answer["root_id"] in {ch["id"] for ch in g["changes"]}

    # Snapshots are rebuilt when missing.
    for v in naming.VIEWS:
        await client.delete_graph(naming.asof(sid, iid, v))
    again = await c.get(f"/api/incidents/{iid}/graph")
    assert again.status_code == 200 and again.json() == g

    async with http(ic_app, "10.6.0.4") as other:
        await other.post("/api/session", json={})
        assert (await other.get(f"/api/incidents/{iid}/graph")).status_code in (403, 404)
    async with http(ic_app, "10.6.0.5") as anon:
        assert (await anon.get(f"/api/incidents/{iid}/graph")).status_code == 401
    assert (await c.get("/api/incidents/bad.id/graph")).status_code in (404, 422)


async def test_graph_route_cap_keeps_symptomatic_then_nearest(finished, monkeypatch):
    c, body, *_ = finished
    iid = body["incident_id"]
    full = (await c.get(f"/api/incidents/{iid}/graph")).json()
    n_sym = sum(n["symptomatic"] for n in full["nodes"])
    cap = n_sym + 2
    assert len(full["nodes"]) > cap
    monkeypatch.setattr(graph_view, "GRAPH_MAX_NODES", cap)
    capped = (await c.get(f"/api/incidents/{iid}/graph")).json()
    assert len(capped["nodes"]) == cap
    assert sum(n["symptomatic"] for n in capped["nodes"]) == n_sym
    ids = {n["id"] for n in capped["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in capped["edges"])


async def test_verify_after_a_run_then_after_a_qualifying_edit(finished, ic_app):
    c, body, sid, answer, _ = finished
    iid = body["incident_id"]
    r = await c.post(f"/api/incidents/{iid}/verify", json={})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["status"] == "VERIFIED" and rep["anchor_ok"] is True and rep["chain_ok"]
    assert rep["proofs"] and all(p["status"] == "VERIFIED" for p in rep["proofs"])

    # Another session cannot verify this incident (it has no ledger for it).
    async with http(ic_app, "10.6.0.2") as other:
        await other.post("/api/session", json={})
        assert (await other.post(f"/api/incidents/{iid}/verify", json={})).status_code == 404
    async with http(ic_app, "10.6.0.3") as anon:
        assert (await anon.post(f"/api/incidents/{iid}/verify", json={})).status_code == 401

    # AE5: a qualifying deploy appears upstream (recorded before t) -> FALSIFIED.
    world = naming.session_world(sid)
    vf, svc = (
        await client.query(
            world,
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s) RETURN c.valid_from, s.id",
            {"id": answer["root_id"]},
        )
    ).result_set[0]
    await client.query(
        world,
        "MATCH (s:Service {id: $svc}) "
        "CREATE (c:Change {id: 'chg-judge-1', kind: 'deploy', service_id: $svc, "
        "summary: 'Deploy v9.9.9: judge', valid_from: $vf, valid_to: $vt, recorded_at: $rec}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"svc": svc, "vf": vf + 1, "vt": OPEN_END, "rec": vf + 10},
    )
    r = await c.post(f"/api/incidents/{iid}/verify", json={})
    rep = r.json()
    assert rep["status"] == "FALSIFIED" and rep["chain_ok"] and rep["anchor_ok"]
    assert any("chg-judge-1" in p["responsible"] for p in rep["proofs"])


async def test_verify_reports_tampered_ledger(ic_app, ic_state):
    async with http(ic_app, "10.6.1.1") as c:
        r = await inject(c, "flag_flip", 31)
        body = r.json()
        await stream(c, body["run_id"])
        await client.query(
            naming.ledger(sid_of(c)),
            "MATCH (p:Proof {incident_id: $iid, seq: 1}) SET p.result_hash = 'forged'",
            {"iid": body["incident_id"]},
        )
        rep = (await c.post(f"/api/incidents/{body['incident_id']}/verify", json={})).json()
        assert rep["status"] == "TAMPERED" and rep["broken_at"] == 1


# --- recorded fallback ------------------------------------------------------------------


async def test_recorded_hero_endpoint_returns_a_well_formed_event_list(ic_app, tmp_path):
    state = routes.app_state(ic_app)
    if load_hero(state.recorded_path) is None:  # not shipped yet: record one for this test
        state.recorded_path = tmp_path / "hero.json"
        await record_hero(state.recorded_path)
    async with http(ic_app, "10.7.0.1") as c:
        r = await c.get("/api/recorded/hero")
    assert r.status_code == 200
    events = r.json()["events"]
    assert events and [e["id"] for e in events] == list(range(len(events)))
    assert {"id", "run_id", "ts", "type", "agent", "data", "schema_version"} <= set(events[0])
    assert events[0]["type"] == "run_started" and events[0]["data"]["recorded"] is True
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "verdict" for e in events)
    text = json.dumps(events)
    for sid in await _session_ids(state.store):
        assert sid not in text  # the recording session id is redacted


async def _session_ids(store) -> list[str]:
    with store.conn() as c:
        return [r[0] for r in c.execute("SELECT sid FROM sessions")]


async def test_recorded_hero_missing_is_404(tmp_path):
    app = create_app(
        test_settings(tmp_path),
        llm=None,
        store=Store(tmp_path / "s.sqlite3"),
        recorded_path=tmp_path / "missing.json",
    )
    async with http(app) as c:
        assert (await c.get("/api/recorded/hero")).status_code == 404
        assert (await c.get("/api/recorded/hero/graph")).status_code == 404


async def test_record_hero_saves_the_incident_graph_and_serves_it(tmp_path):
    out = tmp_path / "hero.json"
    events = await record_hero(out, settings=test_settings(tmp_path))  # deterministic, no LLM
    started = next(e["data"] for e in events if e["type"] == "run_started")
    verdict = next(e["data"] for e in events if e["type"] == "verdict")
    alert = started["alert"]["service"]
    graph_file = hero_graph_path(out)
    assert graph_file == tmp_path / "hero_graph.json" and graph_file.is_file()
    g = load_hero_graph(graph_file)
    assert g is not None
    assert set(g) == {"incident_id", "t", "alert_service_id", "nodes", "edges", "changes"}
    assert g["incident_id"] == started["incident_id"] and g["alert_service_id"] == alert
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[alert]["alerting"] is True and nodes[alert]["symptomatic"] is True
    assert verdict["outcome"] == "DIAGNOSED"
    assert verdict["root_cause"]["service"] in nodes  # the suspect is drawn
    for p in verdict["paths"]:
        assert set(p["nodes"]) <= nodes.keys()  # so is every causal path
    assert verdict["root_cause"]["change"] in {ch["id"] for ch in g["changes"]}
    # No session id leaks: any 32-hex token left in the file is the redaction placeholder.
    assert set(re.findall(r"[0-9a-f]{32}", graph_file.read_text())) <= {REDACTED_SID}

    app = create_app(
        test_settings(tmp_path), llm=None, store=Store(tmp_path / "s.sqlite3"), recorded_path=out
    )
    async with http(app, "10.7.0.2") as c:
        r = await c.get("/api/recorded/hero/graph")
    assert r.status_code == 200 and r.json() == g


def test_lifespan_initializes_state_and_health_is_unchanged(tmp_path):
    from fastapi.testclient import TestClient

    client.reset_client()  # TestClient runs the app on its own event loop
    store = Store(tmp_path / "life.sqlite3")
    app = create_app(test_settings(tmp_path, cleanup_interval_s=3600), llm=None, store=store)
    try:
        with TestClient(app) as tc:
            assert getattr(app.state, "ic", None) is not None  # built on startup
            r = tc.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok", "falkordb": True}
    finally:
        client.reset_client()


async def test_graph_route_never_rebuilds_snapshots_under_an_active_run(finished, ic_state):
    """The UI fetches the graph as soon as it learns the incident id, before the run has
    built its snapshots. Rebuilding then replaced the graphs triage was querying."""
    c, body, sid, _, _ = finished
    iid = body["incident_id"]
    for view in ("true", "known"):
        await client.delete_graph(naming.asof(sid, iid, view))
    run = ic_state.runs.create(sid, iid)  # an investigation still in flight
    try:
        r = await c.get(f"/api/incidents/{iid}/graph")
        assert r.status_code == 409, r.text
        assert r.json().get("retry") is True
        assert not await client.graph_exists(naming.asof(sid, iid, "known"))
        r = await c.post(f"/api/incidents/{iid}/exonerate", json={"change_id": "chg-00001"})
        assert r.status_code == 409, r.text
    finally:
        run.status = "done"
    r = await c.get(f"/api/incidents/{iid}/graph")  # no active run: rebuilt on demand
    assert r.status_code == 200, r.text


async def test_learning_curve_route_serves_the_published_run(ic_app, ic_state, tmp_path):
    saved = ic_state.learning_curve_path
    try:
        ic_state.learning_curve_path = tmp_path / "missing.json"
        async with http(ic_app, "10.7.9.1") as c:
            assert (await c.get("/api/learning-curve")).status_code == 404
        data = {"meta": {"runs": 2}, "incidents": [{"tool_calls": 9}, {"tool_calls": 5}]}
        ic_state.learning_curve_path = tmp_path / "learning_curve.json"
        ic_state.learning_curve_path.write_text(json.dumps(data))
        async with http(ic_app, "10.7.9.1") as c:
            r = await c.get("/api/learning-curve")
        assert r.status_code == 200 and r.json() == data
    finally:
        ic_state.learning_curve_path = saved


async def test_benchmark_route_is_public_read_only_and_drops_case_rows(ic_app, ic_state, tmp_path):
    saved = ic_state.benchmark_path
    try:
        ic_state.benchmark_path = tmp_path / "missing.json"
        async with http(ic_app, "10.7.9.2") as c:
            assert (await c.get("/api/benchmark")).status_code == 404
        data = {
            "meta": {"cases": 2, "seed": 42},
            "summary": {"graph": {"accuracy_overall": 1.0}},
            "results": [{"case": "c000"}],
            "scale": {"services": 4000},
            "live_llm_runs": [{"model": "m", "cases": 5, "summary": {}}],
        }
        ic_state.benchmark_path = tmp_path / "report.json"
        ic_state.benchmark_path.write_text(json.dumps(data))
        async with http(ic_app, "10.7.9.2") as c:  # no session cookie
            r = await c.get("/api/benchmark")
            assert r.status_code == 200
            body = r.json()
            assert "results" not in body
            assert body["meta"] == data["meta"] and body["scale"] == data["scale"]
            assert body["live_llm_runs"] == data["live_llm_runs"]
            # read-only: no run can be triggered from this route
            assert (await c.post("/api/benchmark", json={})).status_code == 405
    finally:
        ic_state.benchmark_path = saved


# --- session verify limits ----------------------------------------------------------------


async def test_session_verify_is_debounced_and_capped_per_session(finished, ic_state, monkeypatch):
    """Each Verify builds two world copies: per (session, incident) debounce, per-session
    hourly cap, both 429 with retry information."""
    c, body, sid, _, _ = finished
    iid = body["incident_id"]
    monkeypatch.setattr(ic_state.settings, "session_verify_interval_s", 3600.0)
    monkeypatch.setattr(ic_state.settings, "session_verifies_per_hour", 1000)
    r = await c.post(f"/api/incidents/{iid}/verify", json={})
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/incidents/{iid}/verify", json={})
    assert r.status_code == 429, r.text
    assert r.json()["retry_after"] > 3500 and int(r.headers["retry-after"]) > 3500
    assert "moments ago" in r.json()["error"]

    monkeypatch.setattr(ic_state.settings, "session_verify_interval_s", 0.0)
    monkeypatch.setattr(ic_state.settings, "session_verifies_per_hour", 1)
    r = await c.post(f"/api/incidents/{iid}/verify", json={})
    assert r.status_code == 429, r.text
    assert "per hour" in r.json()["error"] and 0 < r.json()["retry_after"] <= 3600
    assert r.headers["retry-after"]


# --- run ceilings under concurrency -------------------------------------------------------


async def _fake_fault(*a, **kw):
    await asyncio.sleep(0.2)  # an injection takes a while: requests interleave here
    return SimpleNamespace(
        incident_id="inc-001", alert_time=1, alert_service_id="svc-a@us-east", commitment="c0"
    )


async def _fake_start_run(sess, iid, *a, **kw):
    return f"run-{sess.sid[:8]}"


def _run_rows(store: Store, kind: str = "run_global") -> int:
    with store.conn() as c:
        return c.execute("SELECT count(*) FROM limit_events WHERE kind = ?", (kind,)).fetchone()[0]


@pytest.fixture
async def ceiling_app(tmp_path, monkeypatch):
    from ic.api import routes_judge
    from ic.world import faults

    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    app = create_app(test_settings(tmp_path, global_runs_per_hour=2), llm=None, store=store)
    for mod in (routes, routes_judge):
        monkeypatch.setattr(mod, "start_run", _fake_start_run)
        monkeypatch.setattr(mod, "inject_fault", _fake_fault)

    async def no_path_problem(*a, **kw):
        return None

    monkeypatch.setattr(faults, "alert_path_check", no_path_problem)
    yield app, routes.app_state(app)
    await cleanup_store_graphs(store)
    client.reset_client()


@pytest.mark.parametrize("judge", [False, True])
async def test_concurrent_injects_never_overshoot_the_global_run_ceiling(ceiling_app, judge):
    app, state = ceiling_app
    clients = [http(app, f"10.70.{i}.1") for i in range(4)]
    try:
        for c in clients:
            assert (await c.post("/api/session", json={})).status_code == 200
        path, payload = "/api/inject", {"fault_class": "bad_deploy", "seed": 1}
        if judge:
            path = "/api/judge/inject"
            payload = {"target_service_id": "svc-a@us-east", "fault_class": "bad_deploy"}
        rs = await asyncio.gather(*(c.post(path, json=payload) for c in clients))
        codes = sorted(r.status_code for r in rs)
        assert codes == [200, 200, 429, 429], [r.text for r in rs]
        assert _run_rows(state.store) == 2
    finally:
        for c in clients:
            await c.aclose()


async def test_a_failed_injection_releases_its_run_reservation(ceiling_app, monkeypatch):
    app, state = ceiling_app

    async def refuse(*a, **kw):
        raise InjectionError("no valid alert path")

    monkeypatch.setattr(routes, "inject_fault", refuse)
    async with http(app, "10.71.0.1") as c:
        r = await inject(c)
        assert r.status_code == 422, r.text
    assert _run_rows(state.store) == 0
    assert _run_rows(state.store, "run_session") == _run_rows(state.store, "run_ip") == 0


# --- background maintenance ---------------------------------------------------------------


async def test_one_failing_maintenance_stage_does_not_skip_the_others(
    ic_state, monkeypatch, caplog
):
    calls: list[str] = []

    async def cleanup_fails(*a, **kw):
        calls.append("sessions")
        raise RuntimeError("FalkorDB timeout")

    async def close(s, now=None):
        calls.append("close")
        raise RuntimeError("GitHub is down")

    async def pr_cleanup_fails(store, **kw):
        calls.append("pr")
        raise RuntimeError("graph busy")

    def prune():
        calls.append("prune")
        return 3

    monkeypatch.setattr(ic_state.sessions, "cleanup", cleanup_fails)
    monkeypatch.setattr(maintenance, "close_stale_prs_once", close)
    monkeypatch.setattr(pr_store, "cleanup_pr_graphs", pr_cleanup_fails)
    monkeypatch.setattr(ic_state.runs, "prune", prune)
    with caplog.at_level(logging.ERROR):
        out = await maintenance.maintenance_once(ic_state)
    assert calls == ["sessions", "close", "pr", "prune"]
    assert out["runs_pruned"] == 3 and out["session_graphs"] == [] and out["pr_graphs"] == []
    for stage in ("sessions.cleanup", "close_stale_prs", "cleanup_pr_graphs"):
        assert f"maintenance stage {stage} failed" in caplog.text


async def test_concurrent_storms_never_overshoot_the_global_run_ceiling(ceiling_app, monkeypatch):
    from ic.api import routes_storm
    from ic.world.faults import InjectedStorm

    app, state = ceiling_app

    async def slow_storm(world, *, session_id, rng_seed, store, faults=None):
        await asyncio.sleep(0.05)  # the await between the limit check and recording
        return InjectedStorm(
            storm_id=f"storm-{session_id[:6]}", started_at=0, ended_at=1, pages=10, answers=[]
        )

    async def no_run(*a, **kw):
        return None

    monkeypatch.setattr(routes_storm, "inject_storm", slow_storm)
    monkeypatch.setattr(routes_storm, "execute_storm", no_run)
    clients = [http(app, f"10.72.{i}.1") for i in range(4)]
    try:
        for c in clients:
            assert (await c.post("/api/session", json={})).status_code == 200
        rs = await asyncio.gather(*(c.post("/api/storm", json={"seed": 1}) for c in clients))
        assert sorted(r.status_code for r in rs) == [200, 200, 429, 429], [r.text for r in rs]
        assert _run_rows(state.store) == 2
    finally:
        for c in clients:
            await c.aclose()


async def test_session_incidents_lists_only_own_runs_and_creates_nothing(ic_app, ic_state):
    async with http(ic_app, "10.2.9.1") as c:
        r = await c.get("/api/session/incidents")
        assert r.status_code == 200 and r.json() == {"incidents": []}
        assert "set-cookie" not in r.headers
        await c.post("/api/session", json={})
        assert (await c.get("/api/session/incidents")).json() == {"incidents": []}
        sid = sid_of(c)
        with ic_state.store.conn() as db:
            db.execute("INSERT INTO runs VALUES ('run-a', ?, 'inc-001', 'done', 1.0)", (sid,))
            db.execute("INSERT INTO runs VALUES ('run-b', 'other', 'inc-002', 'done', 2.0)")
        assert (await c.get("/api/session/incidents")).json() == {"incidents": ["inc-001"]}
