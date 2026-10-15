"""Open postmortem recommendations on the causal path (U13, R23)."""

from __future__ import annotations

import json

import pytest
from memory_world import drop_session

from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.api.events import EventType
from ic.bench.learning_curve import _settings
from ic.graph import client, naming
from ic.memory import recommendations as R
from ic.runs.orchestrator import default_deps, execute_run
from ic.runs.registry import RunRegistry
from ic.sessions.manager import SessionManager
from ic.store import Store
from ic.world.faults import inject_fault

DAY = 86400
T = 2_000_000_000


@pytest.fixture
async def world():
    client.reset_client()
    sid = naming.new_session_id()
    yield naming.session_world(sid)
    await drop_session(sid)
    client.reset_client()


SETUP = """
CREATE (a:Service {id: 'a'}), (b:Service {id: 'b'}), (z:Service {id: 'z'})
CREATE (i:Incident {id: 'inc-007', opened_at: $opened, resolved_at: $resolved})
CREATE (pm:Postmortem {id: 'pm-007'})-[:ABOUT]->(i)
CREATE (pm)-[:RECOMMENDS]->(r1:Recommendation {id: 'rec-001', text: 'Add a breaker',
                                               status: 'open'})
CREATE (pm)-[:RECOMMENDS]->(r2:Recommendation {id: 'rec-002', text: 'Closed one', status: 'closed'})
CREATE (pm)-[:RECOMMENDS]->(r3:Recommendation {id: 'rec-003', text: 'Off path', status: 'open'})
CREATE (pm)-[:RECOMMENDS]->(r4:Recommendation {id: 'rec-004', text: 'Later', status: 'open',
                                               opened_at: $later})
CREATE (r1)-[:TARGETS]->(b), (r2)-[:TARGETS]->(b), (r3)-[:TARGETS]->(z), (r4)-[:TARGETS]->(a)
"""


async def test_open_recommendation_on_path_with_days_open(world):
    resolved = T - 12 * DAY - 3600
    await client.query(
        world, SETUP, {"opened": resolved - 7200, "resolved": resolved, "later": T + 10}
    )
    recs = await R.open_recommendations_on_path(world, ["a", "b"], T)
    assert [r["id"] for r in recs] == ["rec-001"]  # closed, off-path and future ones excluded
    r = recs[0]
    assert r["days_open"] == 12 and r["opened_at"] == resolved
    assert r["incident_id"] == "inc-007" and r["postmortem_id"] == "pm-007"
    assert r["services"] == ["b"] and r["display_id"] == "REC-001"
    assert (
        r["banner"] == "Predicted by REC-001 (from INC-007 postmortem), open 12 days: Add a breaker"
    )
    assert [r["id"] for r in await R.open_recommendations_on_path(world, ["z"], T)] == ["rec-003"]
    assert await R.open_recommendations_on_path(world, [], T) == []
    # rec-004 opens later than t; at a later t it is open on 'a'
    later = await R.open_recommendations_on_path(world, ["a"], T + 2 * DAY)
    assert [x["id"] for x in later] == ["rec-004"] and later[0]["days_open"] == 1
    data = R.narration_payload(recs)
    assert data["topic"] == "recommendation" and data["recommendations"] == recs
    assert "REC-001" in R.pr_sections(recs)["Predicted by open recommendations"]
    assert "REC-001" in R.page_fields(recs)["Predicted by"]
    assert R.pr_sections([]) == {} and R.page_fields([]) == {}


async def test_recommendation_surfaces_in_run_pr_body_and_page(tmp_path):
    """A full run: the open recommendation on the alert service (always on the causal
    path) is announced after the verdict, cited in the PR body and the page; the closed
    one is not."""
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    sessions = SessionManager(store, max_sessions=10)
    gh, pg = GitHubClient(None, None, mock=True), DiscordPager(None, mock=True)
    deps = default_deps(
        store,
        _settings(tmp_path / "ic.sqlite3"),
        None,
        runs=RunRegistry(store),
        sessions=sessions,
        github=gh,
        pager=pg,
    )
    sess = await sessions.create("127.0.0.1")
    world = naming.session_world(sess.sid)
    try:
        f = await inject_fault(
            world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=3, store=store
        )
        resolved = f.alert_time - 5 * DAY - 60
        await client.query(
            world,
            "MATCH (s:Service {id: $svc}) "
            "CREATE (i:Incident {id: 'inc-900', opened_at: $o, resolved_at: $r}) "
            "CREATE (pm:Postmortem {id: 'pm-900'})-[:ABOUT]->(i) "
            "CREATE (pm)-[:RECOMMENDS]->(r:Recommendation {id: 'rec-900', status: 'open', "
            "        text: 'Shed load at the edge before the pool saturates'})-[:TARGETS]->(s) "
            "CREATE (pm)-[:RECOMMENDS]->(c:Recommendation {id: 'rec-901', status: 'closed', "
            "        text: 'Closed advice'})-[:TARGETS]->(s)",
            {"svc": f.alert_service_id, "o": resolved - 600, "r": resolved},
        )
        run = deps.runs.create(sess.sid, f.incident_id)
        await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)
        assert run.of(EventType.VERDICT)[0]["outcome"] == "DIAGNOSED"
        [narr] = [n for n in run.of(EventType.NARRATION) if n.get("topic") == "recommendation"]
        ids = [r["id"] for r in narr["recommendations"]]
        assert "rec-900" in ids and "rec-901" not in ids
        mine = next(r for r in narr["recommendations"] if r["id"] == "rec-900")
        assert mine["days_open"] == 5 and mine["incident_id"] == "inc-900"
        types = run.types
        assert types.index("verdict") < types.index("narration", types.index("verdict"))
        [pr] = [c for c in gh.calls if c["op"] == "open_rollback_pr"]
        assert "REC-900" in pr["body"] and "open 5 days" in pr["body"]
        assert "REC-901" not in pr["body"] and "Closed advice" not in pr["body"]
        page = json.dumps(pg.calls[0]["payload"])
        assert "REC-900" in page and "REC-901" not in page
    finally:
        await deps.runs.wait_all(limit_s=60)
        await drop_session(sess.sid)
        client.reset_client()
