"""Judge challenge (U2, R5, KTD2, KTD3): two live ``verify_claim`` checks per incident on a
fresh as-of build of the session world; nothing is written to the incident. Real FalkorDB,
deterministic fallback (no LLM key)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from api_helpers import cleanup_store_graphs, http, inject, sid_of, stream, test_settings

from ic.actions import pr_store
from ic.api import routes, routes_challenge
from ic.api.app import create_app
from ic.graph import client, naming
from ic.incidents import state as st
from ic.proof import verify as V
from ic.store import Store
from ic.tools.result import ToolError


@pytest.fixture(scope="module")
async def capp(tmp_path_factory):
    client.reset_client()
    tmp = tmp_path_factory.mktemp("challenge")
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
def cstate(capp) -> routes.AppState:
    return routes.app_state(capp)


@pytest.fixture(scope="module")
async def done(capp, cstate):
    """One bad_deploy incident investigated to a DIAGNOSED verdict."""
    c = http(capp, "10.70.0.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    verdict = next(e["data"] for e in events if e["type"] == "verdict")
    assert verdict["outcome"] == "DIAGNOSED"
    sid = sid_of(c)
    yield {
        "c": c,
        "sid": sid,
        "iid": body["incident_id"],
        "alert_time": body["alert_time"],
        "root": cstate.store.get_answer(body["incident_id"], sid)["root_id"],
    }
    await c.aclose()


def _url(iid: str) -> str:
    return f"/api/incidents/{iid}/challenge"


async def _remaining(c, iid: str) -> int:
    r = await c.get(_url(iid))
    assert r.status_code == 200, r.text
    assert r.json()["limit"] == routes_challenge.CHALLENGES_PER_INCIDENT == 2
    return r.json()["remaining"]


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await client.query(graph, cypher, params, read_only=True)).result_set


async def _session_graphs(sid: str) -> set[str]:
    return {g for g in await client.get_db().list_graphs() if g.startswith(f"s:{sid}:")}


async def _proof_count(sid: str, iid: str) -> int:
    rows = await _rows(
        naming.ledger(sid), "MATCH (p:Proof {incident_id: $iid}) RETURN count(p)", {"iid": iid}
    )
    return rows[0][0]


async def _rederive(c, cstate, sid: str, iid: str) -> None:
    """Re-derive after closing the run's rollback PR (its copies block a re-derive)."""
    rec = pr_store.pr_record(cstate.store, sid, iid)
    if rec is not None:
        await pr_store.cleanup_pr_graphs(cstate.store, closed_pr_urls={rec["pr_url"]})
    r = await c.post(f"/api/judge/{iid}/rederive", json={})
    assert r.status_code == 200, r.text
    _, events = await stream(c, r.json()["run_id"])
    assert events[-1]["type"] == "done"


async def test_challenge_requires_a_session_and_json(capp, done):
    async with http(capp, "10.70.9.1") as b:
        r = await b.post(_url(done["iid"]), json={"change_id": done["root"]})
        assert r.status_code == 401
    c = done["c"]
    r = await c.post(_url(done["iid"]), content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = await c.post(_url(done["iid"]), json={"change_id": done["root"], "graph": "x"})
    assert r.status_code == 422


async def test_unknown_and_non_candidate_ids_are_422_and_spend_nothing(done):
    c, sid, iid = done["c"], done["sid"], done["iid"]
    before = await _remaining(c, iid)
    # Outside the alert window: a real change of this world, but never a candidate.
    old = await _rows(
        naming.session_world(sid),
        "MATCH (c:Change) WHERE c.valid_from < $lo RETURN c.id ORDER BY c.valid_from LIMIT 1",
        {"lo": done["alert_time"] - 24 * 3600},
    )
    assert old
    for bad in ("chg-99999", old[0][0], "x' OR 1=1", ""):
        r = await c.post(_url(iid), json={"change_id": bad})
        assert r.status_code == 422, (bad, r.text)
    assert await _remaining(c, iid) == before


async def test_challenging_the_diagnosed_root_spends_nothing(done):
    c, iid, root = done["c"], done["iid"], done["root"]
    before = await _remaining(c, iid)
    r = await c.post(_url(iid), json={"change_id": root})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_root"] is True and body["diagnosed_root"] == root
    assert body["verdict"] is None and "diagnosed root" in body["message"]
    assert body["remaining"] == before == await _remaining(c, iid)


async def test_challenge_while_the_run_owns_its_snapshots_is_409(done, cstate):
    c, sid, iid = done["c"], done["sid"], done["iid"]
    before = await _remaining(c, iid)
    for v in naming.VIEWS:
        await client.delete_graph(naming.asof(sid, iid, v))
    run = cstate.runs.create(sid, iid)  # an investigation still in flight
    try:
        r = await c.post(_url(iid), json={"change_id": done["root"]})
        assert r.status_code == 409, r.text
        assert r.json().get("retry") is True
        assert not await client.graph_exists(naming.asof(sid, iid, "known"))
    finally:
        run.status = "done"
    assert await _remaining(c, iid) == before


async def test_challenge_refutes_qualifies_after_tamper_and_never_writes(done, cstate):
    """AE2 then AE3 on one late deploy, the budget of two, and a re-derive resetting it."""
    c, sid, iid, root = done["c"], done["sid"], done["iid"], done["root"]
    world, ledger = naming.session_world(sid), naming.ledger(sid)
    root_svc, root_vf = (
        await _rows(
            world,
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) RETURN s.id, c.valid_from",
            {"id": root},
        )
    )[0]

    # A deploy on the root's service that lands exactly at the earliest onset (known at t).
    r = await c.post(
        f"/api/judge/{iid}/tamper",
        json={
            "action": "add_deploy_upstream",
            "service_id": root_svc,
            "minutes_before_onset": 0,
            "as_if_known_at_alert": True,
        },
    )
    assert r.status_code == 200, r.text
    late, onset = r.json()["created"]["id"], r.json()["earliest_onset"]
    # Not a candidate of the incident as investigated: the stored snapshots predate it.
    assert (await c.post(_url(iid), json={"change_id": late})).status_code == 422

    await _rederive(c, cstate, sid, iid)  # now the incident's run saw it and eliminated it
    assert await _remaining(c, iid) == 2
    verify_before = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
    state_before = await st.get_state(ledger, iid)
    proofs_before = await _proof_count(sid, iid)
    graphs_before = await _session_graphs(sid)

    # AE2: REFUTED on after_onset, its change time not before the first symptom.
    r = await c.post(_url(iid), json={"change_id": late})
    assert r.status_code == 200, r.text
    ae2 = r.json()
    assert ae2["verdict"] == "REFUTED" and ae2["clause"] == "after_onset", ae2
    assert ae2["claim"]["event_time"] >= onset and "onset" in ae2["detail"]
    assert "MATCH" in ae2["query_text"] and ae2["checked"]
    assert ae2["is_root"] is False and ae2["diagnosed_root"] == root
    assert ae2["would_overturn"] is None and ae2["remaining"] == 1
    assert ae2["t"] == done["alert_time"]
    verify_after = (await c.post(f"/api/incidents/{iid}/verify", json={})).json()
    assert verify_after["status"] == verify_before["status"]
    assert [p["status"] for p in verify_after["proofs"]] == [
        p["status"] for p in verify_before["proofs"]
    ]

    # AE3: the judge moves the late deploy before onset; the fresh build sees the tamper.
    r = await c.post(
        f"/api/judge/{iid}/tamper",
        json={"action": "change_timestamp", "change_id": late, "new_valid_from": root_vf + 1},
    )
    assert r.status_code == 200, r.text
    r = await c.post(_url(iid), json={"change_id": late})
    assert r.status_code == 200, r.text
    ae3 = r.json()
    assert ae3["verdict"] in ("RIVAL", "VERIFIED") and ae3["clause"] is None, ae3
    assert ae3["would_overturn"] == "INCONCLUSIVE" and ae3["remaining"] == 0
    # Nothing written: same state, same chain, no graphs left behind.
    assert await st.get_state(ledger, iid) == state_before
    assert await _proof_count(sid, iid) == proofs_before
    assert await _session_graphs(sid) == graphs_before
    assert not any(":verify:" in g for g in await _session_graphs(sid))

    # The third challenge is refused, and the refusal spends nothing either.
    r = await c.post(_url(iid), json={"change_id": late})
    assert r.status_code == 429, r.text
    assert "two challenges" in r.json()["error"] and r.json()["remaining"] == 0
    # The diagnosed root is still answered without budget.
    r = await c.post(_url(iid), json={"change_id": root})
    assert r.status_code == 200 and r.json()["is_root"] is True

    # A re-derive starts the incident over: two challenges again.
    await _rederive(c, cstate, sid, iid)
    assert await _remaining(c, iid) == 2


async def test_budget_is_per_incident_and_per_session(cstate):
    store = cstate.store
    for _ in range(2):
        assert routes_challenge.spend(store, "s1", "inc-001")
    assert not routes_challenge.spend(store, "s1", "inc-001")
    assert routes_challenge.remaining(store, "s1", "inc-001") == 0
    assert routes_challenge.remaining(store, "s1", "inc-002") == 2
    assert routes_challenge.remaining(store, "s2", "inc-001") == 2
    routes_challenge.refund(store, "s1", "inc-001")
    assert routes_challenge.remaining(store, "s1", "inc-001") == 1
    routes_challenge.reset(store, "s1", "inc-001")
    assert routes_challenge.remaining(store, "s1", "inc-001") == 2


async def test_challenge_serializes_with_verify_and_leaves_no_graphs(capp, cstate):
    async with http(capp, "10.70.3.1") as c:
        r = await inject(c, "config_change", 11)
        assert r.status_code == 200, r.text
        iid = r.json()["incident_id"]
        await stream(c, r.json()["run_id"])
        sid = sid_of(c)
        root = cstate.store.get_answer(iid, sid)["root_id"]
        known = naming.asof(sid, iid, "known")
        await c.get(f"/api/incidents/{iid}/graph")  # snapshots present
        rows = await _rows(
            known,
            "MATCH (m:SnapshotMeta) WITH max(m.t) AS t "
            "MATCH (c:Change) WHERE c.valid_from <= t AND c.id <> $root "
            "RETURN c.id ORDER BY c.valid_from DESC LIMIT 1",
            {"root": root},
        )
        pick = rows[0][0]
        graphs_before = await _session_graphs(sid)

        # Held Verify lock: the challenge waits for it rather than racing on :verify: graphs.
        async with V._lock(sid, iid, False):
            task = asyncio.create_task(c.post(_url(iid), json={"change_id": pick}))
            await asyncio.sleep(0.5)
            assert not task.done()
        r = await task
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "REFUTED"

        rv, rc = await asyncio.gather(
            c.post(f"/api/incidents/{iid}/verify", json={}),
            c.post(_url(iid), json={"change_id": pick}),
        )
        assert rv.status_code == 200 and rc.status_code == 200, (rv.text, rc.text)
        assert rv.json()["status"] == "VERIFIED"
        assert await _session_graphs(sid) == graphs_before
        tracked = cstate.sessions.tracked_graphs(sid)
        assert {naming.verify(sid, iid, v) for v in naming.VIEWS} <= set(tracked)


# --- failure paths: the challenge comes back, nothing is left behind ----------------------


async def _pick(done) -> str:
    root, candidates = await routes_challenge._incident_record(done["sid"], done["iid"])
    return sorted(x for x in candidates if x != root)[0]


async def _no_verify_graphs(sid: str) -> bool:
    return not any(":verify:" in g for g in await _session_graphs(sid))


async def test_tool_error_is_422_and_refunds(done, monkeypatch):
    c, sid, iid = done["c"], done["sid"], done["iid"]
    pick, before = await _pick(done), await _remaining(c, iid)
    assert before > 0

    async def boom(*_a, **_k):
        raise ToolError(f"{pick} is not in the fresh snapshot")

    monkeypatch.setattr(routes_challenge, "call_tool", boom)
    r = await c.post(_url(iid), json={"change_id": pick})
    assert r.status_code == 422 and "fresh snapshot" in r.json()["error"], r.text
    assert await _remaining(c, iid) == before
    assert await _no_verify_graphs(sid)


async def test_crash_mid_build_propagates_refunds_and_leaves_no_graphs(done, monkeypatch):
    """The fresh build fails after (some of) its graphs exist: they are deleted by name."""
    c, sid, iid = done["c"], done["sid"], done["iid"]
    pick, before = await _pick(done), await _remaining(c, iid)
    real = routes_challenge.build_snapshots

    async def half(world, sid_, iid_, t, **kw):
        await real(world, sid_, iid_, t, **kw)
        raise RuntimeError("build blew up")

    monkeypatch.setattr(routes_challenge, "build_snapshots", half)
    with pytest.raises(RuntimeError, match="build blew up"):
        await c.post(_url(iid), json={"change_id": pick})
    assert await _remaining(c, iid) == before
    assert await _no_verify_graphs(sid)


async def test_a_failing_refund_does_not_mask_the_error(done, monkeypatch):
    c, sid, iid = done["c"], done["sid"], done["iid"]
    pick = await _pick(done)

    async def boom(*_a, **_k):
        raise RuntimeError("check blew up")

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(routes_challenge, "call_tool", boom)
    monkeypatch.setattr(routes_challenge, "refund", locked)
    with pytest.raises(RuntimeError, match="check blew up"):
        await c.post(_url(iid), json={"change_id": pick})
    assert await _no_verify_graphs(sid)
