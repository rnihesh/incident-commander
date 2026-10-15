"""Run event stream over SSE (U9, R32): order, resume, isolation (AE7)."""

from __future__ import annotations

import json

import pytest
from api_helpers import http, inject, is_subsequence, sid_of, stream, types

from ic.graph import client, naming

HERO_ORDER = [
    "run_started",
    "reflex",
    "snapshot",
    "state:TRIAGING",
    "funnel",
    "hypothesis",
    "proof",
    "certificate",
    "verdict",
    "state:DIAGNOSED",
    "state:REHEARSING",
    "handoff",
    "rehearsal",
    "state:ACTING",
    "action:pr",
    "action:commit_status",
    "action:page",
    "state:PR_OPEN",
    "state:RESOLVED",
    "memory",
    "state:LEARNED",
    "done",
]


@pytest.fixture(scope="module")
async def hero(ic_app, ic_state):
    c = http(ic_app, "10.1.0.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    resp, events = await stream(c, body["run_id"])
    assert resp.status_code == 200
    yield c, body, resp, events
    await c.aclose()


def of(events: list[dict], type_: str) -> list[dict]:
    return [e["data"] for e in events if e["type"] == type_]


async def test_inject_streams_a_full_investigation_in_order(hero, ic_state):
    c, body, _, events = hero
    assert [e["id"] for e in events] == list(range(len(events)))  # monotonic, no gaps
    assert all(e["run_id"] == body["run_id"] for e in events)
    assert is_subsequence(HERO_ORDER, types(events)), types(events)
    assert types(events)[0] == "run_started" and types(events)[-1] == "done"
    assert events == sorted(events, key=lambda e: e["ts"])

    sid = sid_of(c)
    answer = ic_state.store.get_answer(body["incident_id"], sid)
    [verdict] = of(events, "verdict")
    assert verdict["outcome"] == "DIAGNOSED" and verdict["degraded"] is True
    assert verdict["degraded_reason"] == "no_llm"
    prec = [n for n in of(events, "narration") if n.get("topic") == "precedent"]
    assert len(prec) == 1 and prec[0]["text"]
    ids = [e["type"] for e in events]
    assert ids.index("verdict") < ids.index("narration", ids.index("verdict"))
    for m in prec[0]["matches"]:
        assert set(m) == {"incident", "title", "method", "score", "cause"}
        assert m["method"] in ("semantic", "fuzzy", "path_shape")
    assert verdict["root_cause"]["change"] == answer["root_id"]
    assert body["degraded"] is True

    started = of(events, "run_started")[0]
    assert started["incident_id"] == body["incident_id"]
    assert started["alert"]["service"] == body["alert_service_id"]
    reflex = of(events, "reflex")[0]
    assert reflex["label"] == "pre-snapshot" and isinstance(reflex["blast_radius"], int)
    assert isinstance(reflex["search_space"], int) and reflex["search_space"] > 0
    assert {"name"} <= set(reflex["oncall"]) and isinstance(reflex["suspects"], list)
    snap = of(events, "snapshot")[0]
    assert [g["name"] for g in snap["graphs"]] == ["true", "known"]
    assert body["alert_service_id"] in snap["symptomatic"]

    [rehearsal] = of(events, "rehearsal")
    assert rehearsal["result"] == "CLEARED" and rehearsal["status"] == "CLEARED"
    assert rehearsal["still_explains"] == [] and rehearsal["checked"] > 0
    assert rehearsal["change"] == answer["root_id"]
    [handoff] = of(events, "handoff")
    assert handoff["to"] == "remediation" and handoff["suspect"] == answer["root_id"]
    assert handoff["verdict"] == "DIAGNOSED" and len(handoff["proofs"]) >= 3
    actions = {a["kind"]: a for a in of(events, "action")}
    assert set(actions) == {"pr", "commit_status", "page"}
    assert all(a["status"] == "ok" and a["simulated"] is True for a in actions.values())
    assert actions["pr"]["url"].startswith("https://github.com/") and actions["pr"]["number"]
    assert actions["commit_status"]["state"] == "success"
    assert actions["commit_status"]["proof_status"] == "VERIFIED"
    assert of(events, "done")[0]["ms"] > 0


async def test_stream_headers(hero):
    _, _, resp, _ = hero
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"


@pytest.mark.parametrize("via", ["header", "query"])
async def test_reconnect_with_last_event_id_resumes_without_duplicates(hero, via):
    c, body, _, events = hero
    k = len(events) // 2
    if via == "header":
        r, tail = await stream(c, body["run_id"], **{"Last-Event-ID": str(k)})
    else:
        r = await c.get(f"/api/runs/{body['run_id']}/events", params={"last_event_id": k})
        from api_helpers import parse_sse

        tail = parse_sse(r.text)
    assert r.status_code == 200
    assert [e["id"] for e in tail] == list(range(k + 1, len(events)))
    assert tail == events[k + 1 :]


async def test_replay_survives_a_process_restart(hero, ic_state):
    """A fresh registry on the same SQLite store replays the persisted events."""
    from ic.runs.registry import RunRegistry

    _, body, _, events = hero
    reborn = RunRegistry(ic_state.store).get(body["run_id"])
    assert reborn is not None and reborn.finished
    assert [e.model_dump(mode="json") for e in reborn.events] == events


async def test_another_sessions_run_is_forbidden(ic_app, hero):
    _, body, _, _ = hero
    async with http(ic_app, "10.1.0.2") as other:
        assert (await other.post("/api/session", json={})).status_code == 200
        r, _ = await stream(other, body["run_id"])
        assert r.status_code == 403
    async with http(ic_app, "10.1.0.3") as anon:
        r, _ = await stream(anon, body["run_id"])
        assert r.status_code == 403
        r, _ = await stream(anon, "run-doesnotexist")
        assert r.status_code == 404


async def test_two_sessions_see_only_their_own_fault(ic_app, ic_state):
    """AE7: two visitors plant different faults; neither stream nor world shows the other's."""
    async with http(ic_app, "10.1.1.1") as a, http(ic_app, "10.1.1.2") as b:
        ra, rb = await inject(a, "bad_deploy", 11), await inject(b, "flag_flip", 12)
        assert ra.status_code == rb.status_code == 200, (ra.text, rb.text)
        ja, jb = ra.json(), rb.json()
        _, ea = await stream(a, ja["run_id"])
        _, eb = await stream(b, jb["run_id"])
        sa, sb = sid_of(a), sid_of(b)
        assert sa != sb
        ka = ic_state.store.get_answer(ja["incident_id"], sa)
        kb = ic_state.store.get_answer(jb["incident_id"], sb)
        va, vb = of(ea, "verdict")[0], of(eb, "verdict")[0]
        assert va["root_cause"]["change"] == ka["root_id"] and va["root_cause"]["kind"] == "deploy"
        assert vb["root_cause"]["change"] == kb["root_id"] and vb["root_cause"]["kind"] == "flag"
        # Streams never mention the other session's graphs.
        assert sb not in json.dumps(ea) and sa not in json.dumps(eb)

        # Worlds: the other's root change (by content, ids are per-world counters) is absent.
        q = (
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s) "
            "RETURN c.kind, c.summary, c.valid_from, s.id"
        )
        root_a = (await client.query(naming.session_world(sa), q, {"id": ka["root_id"]})).result_set
        root_b = (await client.query(naming.session_world(sb), q, {"id": kb["root_id"]})).result_set
        match = (
            "MATCH (c:Change)-[:CHANGES]->(s:Service {id: $svc}) "
            "WHERE c.kind = $kind AND c.summary = $summary AND c.valid_from = $vf RETURN count(c)"
        )
        for world, (kind, summary, vf, svc) in (
            (naming.session_world(sb), root_a[0]),
            (naming.session_world(sa), root_b[0]),
        ):
            p = {"svc": svc, "kind": kind, "summary": summary, "vf": vf}
            assert (await client.query(world, match, p)).result_set[0][0] == 0

        # And neither can read the other's stream.
        r, _ = await stream(a, jb["run_id"])
        assert r.status_code == 403
        r, _ = await stream(b, ja["run_id"])
        assert r.status_code == 403
