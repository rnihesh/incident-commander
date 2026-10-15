"""Judge mode (U14): commit-reveal, alert-path validation, tampering, re-derive, isolation
and answer-key leakage. Real FalkorDB, deterministic fallback (no LLM key)."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from api_helpers import cleanup_store_graphs, http, sid_of, stream, test_settings
from mcp import Client

from ic.actions import pr_store
from ic.api import routes, routes_judge
from ic.api.app import create_app
from ic.graph import client, naming
from ic.mcp_server import McpScope, build_server
from ic.store import Store
from ic.world import faults

SEED = 3


@pytest.fixture(scope="module")
async def japp(tmp_path_factory):
    client.reset_client()
    tmp = tmp_path_factory.mktemp("judge")
    store = Store(tmp / "ic.sqlite3")
    settings = test_settings(
        tmp,
        session_runs_per_hour=100,
        ip_runs_per_hour=100,
        ip_concurrent_sessions=10,
        ip_sessions_per_hour=100,
    )
    app = create_app(settings, llm=None, store=store)
    state = routes.app_state(app)
    yield app
    await state.runs.wait_all(limit_s=60)
    await cleanup_store_graphs(store)
    client.reset_client()


@pytest.fixture(scope="module")
def jstate(japp) -> routes.AppState:
    return routes.app_state(japp)


async def _targets(c, **body) -> dict:
    r = await c.post("/api/judge/targets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _judge_inject(c, fault_class: str, seed: int = SEED, target: str | None = None):
    """Inject on the first eligible target (sorted) that yields a valid fault."""
    cands = [target] if target else []
    if not cands:
        svcs = (await _targets(c))["services"]
        cands = [x["id"] for x in svcs if fault_class not in x["unavailable"]]
    for svc in cands[:25]:
        body = {"target_service_id": svc, "fault_class": fault_class, "seed": seed}
        r = await c.post("/api/judge/inject", json=body)
        if r.status_code == 200:
            return svc, r.json()
        assert r.status_code == 422, r.text
    raise AssertionError(f"no target produced a {fault_class} fault")


def _keys(obj, out: set[str]) -> set[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


@pytest.fixture(scope="module")
async def judged(japp, jstate):
    """One blind bad_deploy judge round, run to completion."""
    c = http(japp, "10.40.0.1")
    target, body = await _judge_inject(c, "bad_deploy")
    published = body["commitment"]
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    sid = sid_of(c)
    answer = jstate.store.get_answer(body["incident_id"], sid)
    yield {
        "c": c,
        "target": target,
        "body": body,
        "published": published,
        "events": events,
        "sid": sid,
        "answer": answer,
    }
    await c.aclose()


# --- commitment and reveal ----------------------------------------------------------------


def test_commitment_is_sha256_of_canonical_json_plus_nonce():
    pre = faults.commitment_preimage("flt-1", "inc-001", "bad_deploy", "chg-00042")
    assert pre == (
        '{"fault_class":"bad_deploy","fault_id":"flt-1","incident_id":"inc-001",'
        '"root_id":"chg-00042"}'
    )
    want = hashlib.sha256((pre + "n0nce").encode()).hexdigest()
    assert faults.commitment("flt-1", "inc-001", "bad_deploy", "chg-00042", "n0nce") == want
    assert faults.commitment("flt-1", "inc-001", "bad_deploy", "chg-00042", "other") != want


def test_compare_grades_match_partial_miss_and_no_claim():
    a = {"root_id": "chg-00010", "fault_class": "bad_deploy"}
    assert faults.compare(a, "DIAGNOSED", "chg-00010", "bad_deploy")[0] == "match"
    assert faults.compare(a, "DIAGNOSED", "chg-00011", "bad_deploy")[0] == "partial"
    assert faults.compare(a, "DIAGNOSED", "chg-00011", "flag_flip")[0] == "miss"
    for outcome in ("INCONCLUSIVE", "FAILED", None):
        assert faults.compare(a, outcome, "chg-00010", None)[0] == "no_claim"
    assert faults.compare(a, "DIAGNOSED", None, None)[0] == "no_claim"


async def test_inject_returns_only_the_commitment(judged):
    body = judged["body"]
    assert set(body) == {
        "run_id", "incident_id", "alert_service_id", "alert_time", "commitment",
        "reveal_after_s", "events_url", "degraded",
    }  # fmt: skip
    a = judged["answer"]
    blob = json.dumps(body)
    assert a["nonce"] not in blob and a["root_id"] not in blob
    assert a["fault_class"] not in blob and a["fault_id"] not in blob
    assert len(body["commitment"]) == 64 and a["commitment"] == body["commitment"]


async def test_reveal_after_verdict_matches_and_recomputes_the_published_hash(judged):
    c, body, a = judged["c"], judged["body"], judged["answer"]
    r = await c.post(f"/api/judge/{body['incident_id']}/reveal", json={})
    assert r.status_code == 200, r.text
    rv = r.json()
    assert rv["revealed"] == {k: a[k] for k in ("fault_id", "fault_class", "root_id", "nonce")}
    # The hash published before the run equals the one recomputed from the revealed values.
    recomputed = hashlib.sha256((rv["preimage"] + rv["revealed"]["nonce"]).encode()).hexdigest()
    assert recomputed == judged["published"] == rv["recomputed"] == rv["published_commitment"]
    assert rv["commitment_ok"] is True and rv["timed_out"] is False
    verdict = next(e["data"] for e in judged["events"] if e["type"] == "verdict")
    assert verdict["outcome"] == "DIAGNOSED"
    cmp_ = rv["comparison"]
    assert cmp_["result"] == "match" and cmp_["suspect_id"] == a["root_id"]
    assert cmp_["claimed_class"] == "bad_deploy" and rv["rederived"] is None


async def test_reveal_is_blocked_until_verdict_or_timeout(japp, jstate):
    async with http(japp, "10.40.1.1") as c:
        await c.post("/api/session", json={})
        sid = sid_of(c)
        world = naming.session_world(sid)
        f = await faults.inject_fault(
            world, session_id=sid, fault_class="flag_flip", rng_seed=7, store=jstate.store
        )
        run = jstate.runs.create(sid, f.incident_id)  # in flight, no verdict yet
        routes_judge.put_round(
            jstate.store,
            sid=sid,
            incident_id=f.incident_id,
            run_id=run.id,
            alert_service_id=f.alert_service_id,
            alert_time=f.alert_time,
            commitment=f.commitment,
        )
        try:
            r = await c.post(f"/api/judge/{f.incident_id}/reveal", json={})
            assert r.status_code == 425, r.text
            assert 100 < r.json()["retry_after"] <= 120 and r.headers["retry-after"]
            assert f.answer.nonce not in r.text and f.answer.root_id not in r.text

            # Another session cannot reveal it.
            async with http(japp, "10.40.1.2") as other:
                await other.post("/api/session", json={})
                r2 = await other.post(f"/api/judge/{f.incident_id}/reveal", json={})
                assert r2.status_code == 404

            # 120 s after injection the reveal opens with "no claim".
            with jstate.store.conn() as conn:
                conn.execute(
                    "UPDATE judge_rounds SET injected_at = ? WHERE sid = ? AND incident_id = ?",
                    (time.time() - 121, sid, f.incident_id),
                )
            r = await c.post(f"/api/judge/{f.incident_id}/reveal", json={})
            assert r.status_code == 200, r.text
            rv = r.json()
            assert rv["timed_out"] is True and rv["comparison"]["result"] == "no_claim"
            assert rv["revealed"]["root_id"] == f.answer.root_id
            assert rv["recomputed"] == f.commitment and rv["commitment_ok"]
        finally:
            run.status = "done"
        async with http(japp, "10.40.1.3") as anon:
            r = await anon.post(f"/api/judge/{f.incident_id}/reveal", json={})
            assert r.status_code == 401


async def test_reveal_fails_closed_when_the_run_is_unknown(japp, jstate):
    """A round whose run cannot be found (lost registry, bad id) must not open early."""
    async with http(japp, "10.40.1.9") as c:
        await c.post("/api/session", json={})
        sid = sid_of(c)
        world = naming.session_world(sid)
        f = await faults.inject_fault(
            world, session_id=sid, fault_class="bad_deploy", rng_seed=9, store=jstate.store
        )
        routes_judge.put_round(
            jstate.store,
            sid=sid,
            incident_id=f.incident_id,
            run_id="run-does-not-exist",
            alert_service_id=f.alert_service_id,
            alert_time=f.alert_time,
            commitment=f.commitment,
        )
        r = await c.post(f"/api/judge/{f.incident_id}/reveal", json={})
        assert r.status_code == 425, r.text
        assert f.answer.nonce not in r.text and f.answer.root_id not in r.text


# --- targets and alert-path validation ---------------------------------------------------


async def test_targets_list_services_with_alert_path_flags(japp, jstate):
    async with http(japp, "10.40.2.1") as c:
        t = await _targets(c)
        assert sid_of(c)  # a session was created for the picker
        svcs = t["services"]
        assert t["total"] == len(svcs) > 100
        assert set(t["fault_classes"]) == set(faults.FAULT_CLASSES)
        s0 = svcs[0]
        assert {"id", "name", "region", "tier", "has_upstream_path", "reason"} <= set(s0)
        ok = [x for x in svcs if x["has_upstream_path"]]
        bad = [x for x in svcs if not x["has_upstream_path"]]
        assert ok and bad and all(x["reason"] for x in bad)
        assert all(x["reason"] is None for x in ok)
        q = await _targets(c, q=ok[0]["name"])
        assert 0 < q["total"] < t["total"]
        assert all(ok[0]["name"] in x["id"] or ok[0]["name"] in x["name"] for x in q["services"])

        r = await c.get(f"/api/judge/services/{ok[0]['id']}/changes")
        assert r.status_code == 200, r.text
        ch = r.json()["changes"]
        assert all(set(x) == {"id", "kind", "summary", "valid_from", "recorded_at"} for x in ch)
        vfs = [x["valid_from"] for x in ch]
        assert vfs == sorted(vfs, reverse=True)
        assert (await c.get("/api/judge/services/svc-nope@nowhere/changes")).status_code == 404
        assert (await c.get("/api/judge/services/x'%20OR%201=1/changes")).status_code == 404


async def test_target_without_alert_path_is_rejected_with_a_reason(japp, jstate):
    async with http(japp, "10.40.3.1") as c:
        svcs = (await _targets(c))["services"]
        dead = next(x for x in svcs if not x["has_upstream_path"])
        body = {"target_service_id": dead["id"], "fault_class": "bad_deploy", "seed": 1}
        r = await c.post("/api/judge/inject", json=body)
        assert r.status_code == 422
        assert "alert path" in r.json()["reason"] and dead["reason"] in r.json()["reason"]
        assert jstate.store.get_answer("inc-001", sid_of(c)) is None  # nothing injected

        body["target_service_id"] = "svc-nope@nowhere"
        r = await c.post("/api/judge/inject", json=body)
        assert r.status_code == 422 and "unknown service" in r.json()["reason"]
        for bad_id in ["svc-a'}) DETACH DELETE (n", "s:abc:world", "*"]:
            body["target_service_id"] = bad_id
            assert (await c.post("/api/judge/inject", json=body)).status_code == 422
        body["target_service_id"] = dead["id"]
        body["fault_class"] = "rm_rf"
        assert (await c.post("/api/judge/inject", json=body)).status_code == 422


# --- leakage ---------------------------------------------------------------------------------


async def test_answer_key_never_reaches_events_tools_graphs_or_mcp(judged):
    """R26: the nonce, fault id, commitment and fault class never appear in the run's events
    (which carry every tool result and the agent's narration), the session's graphs, or MCP
    tool results; no event carries answer-shaped keys."""
    a, sid, iid = judged["answer"], judged["sid"], judged["body"]["incident_id"]
    events = judged["events"]
    needles = [a["nonce"], a["fault_id"], a["commitment"], a["fault_class"], "bad_deploy"]
    blob = json.dumps(events)
    for n in needles:
        assert n not in blob, n
    tool_results = [e["data"] for e in events if e["type"] == "tool_call"]
    assert tool_results  # the funnel ran
    for n in needles:
        assert n not in json.dumps(tool_results)
    keys = _keys([e["data"] for e in events], set())
    assert not keys & {"root_id", "fault_class", "nonce", "fault_id", "answer", "commitment"}
    started = next(e["data"] for e in events if e["type"] == "run_started")
    assert "scenario" not in started

    for graph in (naming.session_world(sid), naming.ledger(sid)):
        for n in needles:
            rows = (
                await client.query(
                    graph,
                    "MATCH (x) WHERE any(k IN keys(x) WHERE k <> 'embedding' "
                    "AND toString(x[k]) CONTAINS $n) "
                    "RETURN count(x)",
                    {"n": n},
                    read_only=True,
                )
            ).result_set
            assert rows[0][0] == 0, (graph, n)

    from ic.graph.snapshots import build_snapshots

    await build_snapshots(naming.session_world(sid), sid, iid, judged["body"]["alert_time"])
    scope = McpScope(sid=sid, incident_id=iid)
    seen = []
    async with Client(build_server(lambda: scope)) as mc:
        for name, args in [
            ("list_symptoms", {"incident_id": iid}),
            ("screen_candidates", {"incident_id": iid}),
            ("absence_certificate", {"incident_id": iid, "suspect_id": a["root_id"]}),
            ("find_precedents", {"incident_id": iid}),
        ]:
            res = await mc.call_tool(name, args)
            assert not res.is_error, res.content[0].text
            seen.append(res.content[0].text)
    mblob = "\n".join(seen)
    for n in needles:
        assert n not in mblob


# --- tampering, verify, re-derive ---------------------------------------------------------


async def _root_service(sid: str, root_id: str) -> str:
    rows = (
        await client.query(
            naming.session_world(sid),
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) RETURN s.id",
            {"id": root_id},
            read_only=True,
        )
    ).result_set
    return rows[0][0]


async def _count(graph: str, cypher: str) -> int:
    return (await client.query(graph, cypher, read_only=True)).result_set[0][0]


async def test_tamper_validation_and_cross_session_isolation(judged, japp):
    """AE7: a tamper request names only the caller's world; ids are validated against it."""
    c, sid, iid = judged["c"], judged["sid"], judged["body"]["incident_id"]
    world_a = naming.session_world(sid)
    before = await _count(world_a, "MATCH (c:Change) RETURN count(c)")
    root_svc = await _root_service(sid, judged["answer"]["root_id"])
    url = f"/api/judge/{iid}/tamper"
    add = {"action": "add_deploy_upstream", "service_id": root_svc, "minutes_before_onset": 5}

    for bad in [
        {**add, "service_id": "svc-nope@nowhere"},
        {**add, "service_id": "svc-a'}) MATCH (n) DETACH DELETE n //@x"},
        {**add, "minutes_before_onset": 99999},
        {**add, "graph": "s:" + "0" * 32 + ":world"},
        {"action": "remove_edge", "from_service_id": root_svc, "to_service_id": root_svc},
        {"action": "change_timestamp", "change_id": "chg-99999", "new_valid_from": 1},
        {"action": "change_timestamp", "change_id": "chg-1' OR 1=1", "new_valid_from": 1},
        {"action": "drop_graph"},
    ]:
        r = await c.post(url, json=bad)
        assert r.status_code == 422, (bad, r.text)
    assert await _count(world_a, "MATCH (c:Change) RETURN count(c)") == before

    # A deploy on a service that is not upstream of the alerting service is refused.
    svcs = (await _targets(c))["services"]
    alert = judged["body"]["alert_service_id"]
    g = await faults._impact_services(world_a, judged["body"]["alert_time"])
    import networkx as nx

    off_path = next(
        x["id"]
        for x in svcs
        if x["id"] != alert and (x["id"] not in g or alert not in nx.descendants(g, x["id"]))
    )
    r = await c.post(url, json={**add, "service_id": off_path})
    assert r.status_code == 422 and "not upstream" in r.json()["reason"]

    async with http(japp, "10.40.4.2") as b:
        # B has no session world incident with that id yet: refused, A untouched.
        assert (await b.post(url, json=add)).status_code == 401
        await b.post("/api/session", json={})
        r = await b.post(url, json=add)
        assert r.status_code == 404
        # B plants its own fault (incident ids repeat across sessions) and tampers its own world.
        _, bb = await _judge_inject(b, "bad_deploy", seed=SEED, target=judged["target"])
        await stream(b, bb["run_id"])
        assert bb["incident_id"] == iid  # same id, different world
        sid_b = sid_of(b)
        root_b = await _root_service(sid_b, judged["answer"]["root_id"])
        r = await b.post(url, json={**add, "service_id": root_b})
        assert r.status_code == 200, r.text
        assert r.json()["graph"] == naming.session_world(sid_b) != world_a
        # B's tamper and B's run never show up in A's world or A's verify.
        assert await _count(world_a, "MATCH (c:Change) RETURN count(c)") == before
        rep = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
        assert rep["status"] == "VERIFIED", rep
        # B cannot read A's run events.
        r = await b.get(f"/api/runs/{judged['body']['run_id']}/events")
        assert r.status_code == 403


async def test_tamper_add_deploy_then_verify_falsifies_and_rederive_reflects_it(judged, jstate):
    """AE5: a qualifying deploy upstream (recorded before t) flips Verify to FALSIFIED and
    names the new change; a re-derived run on the tampered world concludes differently."""
    c, sid, iid = judged["c"], judged["sid"], judged["body"]["incident_id"]
    root = judged["answer"]["root_id"]
    rep = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
    assert rep["status"] == "VERIFIED"

    root_svc = await _root_service(sid, root)
    r = await c.post(
        f"/api/judge/{iid}/tamper",
        json={
            "action": "add_deploy_upstream",
            "service_id": root_svc,
            "minutes_before_onset": 3,
            "as_if_known_at_alert": True,
        },
    )
    assert r.status_code == 200, r.text
    t = r.json()
    new_id = t["created"]["id"]
    assert t["graph"] == naming.session_world(sid) and t["node"] == new_id
    assert t["created"]["late_arriving"] is False
    assert (
        t["created"]["recorded_at"] <= t["t"] and t["created"]["valid_from"] < t["earliest_onset"]
    )
    assert new_id in t["summary"] and t["statements"]

    rep = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
    assert rep["status"] == "FALSIFIED" and rep["chain_ok"] and rep["anchor_ok"]
    flipped = [p for p in rep["proofs"] if p["status"] == "FALSIFIED"]
    assert flipped and any(new_id in p["responsible"] for p in flipped)

    # Re-derive: refused while a run is active, then a new run on the same incident and t.
    busy = jstate.runs.create(sid, iid)
    try:
        assert (await c.post(f"/api/judge/{iid}/rederive", json={})).status_code == 409
    finally:
        busy.status = "done"
    # ... and refused while the blind run's rollback PR is open (its proof page is bound to
    # this chain); once the auto-close pass closed it and dropped its copies, it goes ahead.
    r = await c.post(f"/api/judge/{iid}/rederive", json={})
    assert r.status_code == 409 and "PR" in r.json()["error"], r.text
    pr_url = pr_store.pr_record(jstate.store, sid, iid)["pr_url"]
    await pr_store.cleanup_pr_graphs(jstate.store, closed_pr_urls={pr_url})
    assert not pr_store.has_pr_copies(jstate.store, sid, iid)
    r = await c.post(f"/api/judge/{iid}/rederive", json={})
    assert r.status_code == 200, r.text
    rd = r.json()
    assert rd["run_id"] != judged["body"]["run_id"]
    assert rd["alert_time"] == judged["body"]["alert_time"]
    assert rd["previous_run_id"] == judged["body"]["run_id"]
    _, events = await stream(c, rd["run_id"])
    assert events[-1]["type"] == "done"
    verdict = next(e["data"] for e in events if e["type"] == "verdict")
    named = {verdict.get("suspect_id"), *verdict.get("rival_ids", [])}
    # Two changes now explain the incident: the old single-root verdict cannot stand.
    assert not (verdict["outcome"] == "DIAGNOSED" and verdict["suspect_id"] == root)
    assert new_id in named

    rep = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
    assert rep["status"] in ("VERIFIED", "DRIFTED"), rep  # the new chain holds

    rv = (await c.post(f"/api/judge/{iid}/reveal", json={})).json()
    assert rv["comparison"]["result"] == "match"  # the blind run is still graded
    assert rv["rederived"]["run_id"] == rd["run_id"]
    assert rv["rederived"]["result"] != "match"


async def test_tamper_remove_edge_and_change_timestamp_describe_the_edit(japp, jstate):
    async with http(japp, "10.40.5.1") as c:
        target, body = await _judge_inject(c, "config_change", seed=11)
        await stream(c, body["run_id"])
        sid, iid = sid_of(c), body["incident_id"]
        world = naming.session_world(sid)
        root = jstate.store.get_answer(iid, sid)["root_id"]

        a, b_ = (
            await client.query(
                world,
                "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service {id: $s}) RETURN a.id, b.id LIMIT 1",
                {"s": target},
                read_only=True,
            )
        ).result_set[0]
        r = await c.post(
            f"/api/judge/{iid}/tamper",
            json={"action": "remove_edge", "from_service_id": a, "to_service_id": b_},
        )
        assert r.status_code == 200, r.text
        assert r.json()["removed"] and "DEPENDS_ON" in r.json()["summary"]
        left = await _count(
            world,
            f"MATCH (:Service {{id: '{a}'}})-[e:DEPENDS_ON]->(:Service {{id: '{b_}'}}) "
            "RETURN count(e)",
        )
        assert left == 0

        onset = r.json()["earliest_onset"]
        r = await c.post(
            f"/api/judge/{iid}/tamper",
            json={"action": "change_timestamp", "change_id": root, "new_valid_from": onset + 30},
        )
        assert r.status_code == 200, r.text
        ch = r.json()["changed"]
        assert ch["id"] == root and ch["valid_from_after"] == onset + 30
        assert "after the earliest onset" in r.json()["summary"]
        rep = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
        assert rep["status"] == "FALSIFIED"
        responsible = {n for p in rep["proofs"] for n in p["responsible"]}
        assert root in responsible


# --- fragility map (U9) -------------------------------------------------------------------


async def _graph_state(graph: str) -> tuple:
    async def rows(cypher: str) -> list:
        return (await client.query(graph, cypher, read_only=True)).result_set

    return (
        await rows("MATCH (n) RETURN count(n)"),
        await rows("MATCH ()-[r]->() RETURN count(r)"),
        sorted(map(tuple, await rows("MATCH (c:Change) RETURN c.id, c.valid_from"))),
    )


async def test_fragility_predicts_a_flip_that_tamper_and_rederive_reproduce(japp, jstate):
    """R13/R14: suggestions come from throwaway copies (the incident is untouched); applying
    a suggested edit through the real tamper flow and re-deriving flips the verdict."""
    async with http(japp, "10.40.6.1") as c:
        _, body = await _judge_inject(c, "bad_deploy", seed=5)
        _, events = await stream(c, body["run_id"])
        verdict = next(e["data"] for e in events if e["type"] == "verdict")
        assert verdict["outcome"] == "DIAGNOSED", verdict
        sid, iid = sid_of(c), body["incident_id"]
        url = f"/api/judge/{iid}/fragility"

        assert (await c.post("/api/judge/inc-999/fragility", json={})).status_code == 404

        guarded = [
            naming.asof(sid, iid, "true"),
            naming.asof(sid, iid, "known"),
            naming.session_world(sid),
            naming.ledger(sid),
        ]
        before = [await _graph_state(g) for g in guarded]
        r = await c.post(url, json={})
        assert r.status_code == 200, r.text
        fm = r.json()
        assert fm["applicable"] and fm["root_id"] == verdict["suspect_id"]
        assert 0 < fm["copies"] == len(fm["tried"]) <= 10 and fm["flips"]
        assert [await _graph_state(g) for g in guarded] == before
        clones = {g for g in jstate.sessions.tracked_graphs(sid) if ":storm:fragility-" in g}
        assert len(clones) == fm["copies"]
        assert not set(await client.get_db().list_graphs()) & clones
        # The web's FragilityEdit keys only: no clone names, write Cypher or session id.
        assert sid not in r.text and "source" not in fm
        for e in fm["tried"] + fm["flips"]:
            assert set(e) <= routes_judge.FRAGILITY_EDIT_KEYS, set(e)

        busy = jstate.runs.create(sid, iid)  # a run in flight: refused (it has no verdict yet)
        try:
            assert (await c.post(url, json={})).status_code == 409
        finally:
            busy.status = "done"
        r = await c.post(url, json={})  # the latest run has no verdict: nothing to map
        assert r.status_code == 409 and "DIAGNOSED" in r.json()["error"]

        pick = next((e for e in fm["flips"] if e["kind"] == "remove_edge"), fm["flips"][0])
        r = await c.post(f"/api/judge/{iid}/tamper", json=pick["tamper"])
        assert r.status_code == 200, r.text
        pr = pr_store.pr_record(jstate.store, sid, iid)
        if pr:  # the run's rollback PR binds the chain; close it like the auto-close pass
            await pr_store.cleanup_pr_graphs(jstate.store, closed_pr_urls={pr["pr_url"]})
        r = await c.post(f"/api/judge/{iid}/rederive", json={})
        assert r.status_code == 200, r.text
        _, events = await stream(c, r.json()["run_id"])
        after = next(e["data"] for e in events if e["type"] == "verdict")
        assert not (after["outcome"] == "DIAGNOSED" and after["suspect_id"] == fm["root_id"]), (
            pick,
            after,
        )


async def test_fragility_is_debounced_capped_and_maps_tool_errors(japp, jstate, monkeypatch):
    """Each map copies the snapshot up to ten times: a per-(session, incident) debounce and a
    per-session hourly cap (429 + Retry-After), like Verify. A tool error is a 422."""
    from ic.tools.registry import ScopeError
    from ic.tools.result import ToolError

    async with http(japp, "10.40.7.1") as c:
        _, body = await _judge_inject(c, "bad_deploy", seed=5)
        _, events = await stream(c, body["run_id"])
        assert next(e["data"] for e in events if e["type"] == "verdict")["outcome"] == "DIAGNOSED"
        url = f"/api/judge/{body['incident_id']}/fragility"

        monkeypatch.setattr(jstate.settings, "session_fragility_interval_s", 3600.0)
        monkeypatch.setattr(jstate.settings, "session_fragility_per_hour", 1000)
        assert (await c.post(url, json={})).status_code == 200
        r = await c.post(url, json={})
        assert r.status_code == 429, r.text
        assert r.json()["retry_after"] > 3500 and int(r.headers["retry-after"]) > 3500

        monkeypatch.setattr(jstate.settings, "session_fragility_interval_s", 0.0)
        monkeypatch.setattr(jstate.settings, "session_fragility_per_hour", 1)
        r = await c.post(url, json={})
        assert r.status_code == 429 and "per hour" in r.json()["error"], r.text
        assert 0 < int(r.headers["retry-after"]) <= 3600

        monkeypatch.setattr(jstate.settings, "session_fragility_per_hour", 1000)
        for err in (ToolError("root chg-x is not in the snapshot"), ScopeError("out of scope")):

            async def boom(*_a, _err=err, **_k):
                raise _err

            monkeypatch.setattr(routes_judge, "call_tool", boom)
            r = await c.post(url, json={})
            assert r.status_code == 422 and str(err) in r.json()["error"], r.text
