"""Replay scrubber route (U18): frames, onset order, as-known vs as-true (AE3), scoping."""

from __future__ import annotations

import pytest
from api_helpers import http, inject, sid_of, stream

from ic.api.routes_replay import MAX_FRAMES, frame_times
from ic.graph import client, naming


@pytest.fixture(scope="module")
async def finished(ic_app, ic_state):
    c = http(ic_app, "10.18.0.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    yield c, body, sid_of(c)
    await c.aclose()


def test_frame_times_fit_the_cap_and_cover_the_range():
    ts = frame_times(1000, 1000 + 3600)
    assert len(ts) <= MAX_FRAMES and ts[0] == 1000 and ts[-1] == 4600
    assert ts == sorted(set(ts))
    short = frame_times(0, 10)
    assert short == list(range(11))
    assert frame_times(5, 5) == [5]


async def test_frames_start_empty_and_symptoms_appear_in_onset_order(finished):
    c, body, _ = finished
    iid = body["incident_id"]
    r = await c.get(f"/api/incidents/{iid}/replay?view=known")
    assert r.status_code == 200, r.text
    d = r.json()
    frames, nodes, m = d["frames"], {n["id"]: n for n in d["nodes"]}, d["markers"]
    assert 2 <= len(frames) <= MAX_FRAMES
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)
    assert m["root_event_t"] is None and m["alert_t"] == body["alert_time"]
    assert frames[-1]["t"] == m["alert_t"] + 30
    assert frames[0]["t"] <= m["earliest_onset_t"] - 1800
    assert frames[0]["symptomatic"] == []
    assert all(f["symptomatic"] == [] for f in frames if f["t"] < m["earliest_onset_t"])
    assert frames[-1]["symptomatic"]
    assert {n["id"] for n in d["nodes"] if n["symptomatic"]} == set(frames[-1]["symptomatic"])
    # Each frame's symptomatic list is a prefix of the onset ordering, and only grows.
    order = sorted(frames[-1]["symptomatic"], key=lambda i: (nodes[i]["onset"], i))
    prev = 0
    for f in frames:
        assert f["symptomatic"] == order[: len(f["symptomatic"])]
        assert len(f["symptomatic"]) >= prev
        prev = len(f["symptomatic"])
        assert all(nodes[i]["onset"] <= f["t"] for i in f["symptomatic"])
        assert f["late_hidden"] == []
    # Changes land in valid_from order; edges and nodes match the graph route.
    graph = (await c.get(f"/api/incidents/{iid}/graph")).json()
    assert d["nodes"] == graph["nodes"] and d["edges"] == graph["edges"]
    assert frames[0]["changes_active"] == [] or all(
        ch["valid_from"] <= frames[0]["t"]
        for ch in d["changes"]
        if ch["id"] in frames[0]["changes_active"]
    )
    assert set(frames[-1]["changes_active"]) == {ch["id"] for ch in d["changes"]}
    # The ledger's verdict, when held, is the only answer-shaped marker.
    assert m["verdict_suspect_t"] is None or isinstance(m["verdict_suspect_t"], int)


async def test_as_known_hides_a_late_arriving_record_the_true_view_shows(finished):
    c, body, sid = finished
    iid = body["incident_id"]
    t = body["alert_time"]
    svc = body["alert_service_id"]
    world = naming.session_world(sid)
    await client.query(
        world,
        "MATCH (s:Service {id: $sid}) "
        "CREATE (c:Change {id: 'chg-late-replay', kind: 'deploy', service_id: $sid, "
        "summary: 'late record', valid_from: $vf, valid_to: 9999999999, recorded_at: $rec}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"sid": svc, "vf": t - 120, "rec": t + 600},
    )
    for v in naming.VIEWS:  # force a rebuild that sees the new record
        await client.delete_graph(naming.asof(sid, iid, v))
    true_d = (await c.get(f"/api/incidents/{iid}/replay?view=true")).json()
    known_d = (await c.get(f"/api/incidents/{iid}/replay?view=known")).json()
    assert "chg-late-replay" in {ch["id"] for ch in true_d["changes"]}
    assert "chg-late-replay" not in {ch["id"] for ch in known_d["changes"]}
    late = next(ch for ch in true_d["changes"] if ch["id"] == "chg-late-replay")
    assert late["late"] is True
    assert all(f["late_hidden"] == ["chg-late-replay"] for f in true_d["frames"])
    assert all(f["late_hidden"] == [] for f in known_d["frames"])
    assert all("chg-late-replay" not in f["changes_active"] for f in known_d["frames"])
    last = true_d["frames"][-1]
    assert "chg-late-replay" in last["changes_active"]
    # Non-late behaviour is identical across views.
    assert [f["symptomatic"] for f in true_d["frames"]] == [
        f["symptomatic"] for f in known_d["frames"]
    ]
    assert true_d["nodes"] == known_d["nodes"]


async def test_replay_is_session_scoped_and_validates_view(finished, ic_app):
    c, body, _ = finished
    iid = body["incident_id"]
    async with http(ic_app, "10.18.0.2") as other:
        await other.post("/api/session", json={})
        assert (await other.get(f"/api/incidents/{iid}/replay")).status_code in (403, 404)
    async with http(ic_app, "10.18.0.3") as anon:
        assert (await anon.get(f"/api/incidents/{iid}/replay")).status_code == 401
    assert (await c.get(f"/api/incidents/{iid}/replay?view=bogus")).status_code == 422
    assert (await c.get("/api/incidents/bad.id/replay")).status_code in (404, 422)


async def test_replay_409_while_a_run_is_active(finished, ic_state, monkeypatch):
    c, body, sid = finished
    iid = body["incident_id"]
    monkeypatch.setattr(ic_state.runs, "active_for", lambda s, i: True)
    r = await c.get(f"/api/incidents/{iid}/replay")
    assert r.status_code == 409 and r.json()["retry"] is True
