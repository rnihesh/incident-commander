"""POST /api/storm (U17): storm injection plus a streamed collapse run."""

from __future__ import annotations

import pytest
from api_helpers import http, sid_of, stream

from ic.api import routes_storm
from ic.graph import client, naming


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(routes_storm, "PACE_S", 0)


async def test_storm_run_streams_intake_incidents_and_chokepoints(ic_app, ic_state):
    async with http(ic_app, "10.17.0.1") as c:
        r = await c.post("/api/storm", json={"seed": 7, "faults": ["bad_deploy", "joint"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["storm_id"].startswith("storm-") and 200 <= body["pages"] <= 400
        resp, events = await stream(c, body["run_id"])
        assert resp.status_code == 200
        sid = sid_of(c)
    types = [e["type"] for e in events]
    assert types[:2] == ["run_started", "snapshot"] and types[-1] == "done"
    assert "error" not in types and types.count("tool_call") == 1
    storm = [e["data"] for e in events if e["type"] == "storm"]
    phases = [d["phase"] for d in storm]
    intake = [d for d in storm if d["phase"] == "intake"]
    assert len(intake) == routes_storm.INTAKE_BATCHES  # counts, not one event per page
    assert [d["received"] for d in intake] == sorted(d["received"] for d in intake)
    assert intake[-1]["received"] == intake[-1]["total"] == body["pages"]
    assert phases[len(intake)] == "grouped" and phases[-2:] == ["chokepoints", "collapsed"]
    incidents = [d for d in storm if d["phase"] == "incident"]
    assert len(incidents) == 2
    assert [i["jointly_caused"] for i in incidents].count(True) == 1
    joint = next(i for i in incidents if i["jointly_caused"])
    assert len(joint["explaining"]) == 2
    assert {x["kind"] for x in joint["explaining"]} == {"flag", "config"}
    assert storm[-1] == {**storm[-1], "pages_in": body["pages"], "incidents": 2}
    tool = next(e["data"] for e in events if e["type"] == "tool_call")
    assert tool["tool"] == "collapse_storm" and tool["graph"].startswith(f"s:{sid}:asof:")
    # The answer key stays server-side: the event stream never names a fault class.
    blob = repr(events)
    assert "bad_deploy" not in blob and "flt-" not in blob
    # Snapshots and the scratch graph are tracked for cleanup; the scratch graph is gone.
    tracked = ic_state.sessions.tracked_graphs(sid)
    scratch = naming.storm(sid, body["storm_id"])
    assert scratch in tracked and naming.asof(sid, body["storm_id"], "known") in tracked
    assert scratch not in await client.get_db().list_graphs()


async def test_storm_rejects_bad_bodies_and_counts_as_a_run(ic_app, ic_state):
    async with http(ic_app, "10.17.0.2") as c:
        r = await c.post("/api/storm", json={"faults": ["expiry"]})
        assert r.status_code == 422
        r = await c.post("/api/storm", json={"faults": ["joint"] * 4})
        assert r.status_code == 422
        r = await c.post("/api/storm", content=b"{}", headers={"content-type": "text/plain"})
        assert r.status_code == 415
        await c.post("/api/session", json={})
        ic_state.sessions.run_started(sid_of(c))  # a run is in flight: same refusal as inject
        try:
            r = await c.post("/api/storm", json={})
            assert r.status_code == 429 and r.json()["fallback"] == "recorded_run"
        finally:
            ic_state.sessions.run_finished(sid_of(c))
