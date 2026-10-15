"""Reflex (KTD16): valid-time Cypher on the session world, before any snapshot exists."""

import statistics
import time

import pytest
from fixture_world import T0, write_world

from ic.graph import client, naming
from ic.graph.snapshots import create_session_world
from ic.tools.reflex import reflex
from ic.tools.registry import ToolContext, call_tool


@pytest.fixture(scope="module")
async def world(module_sid):
    g = naming.session_world(module_sid)
    await write_world(g)
    return g


async def test_reflex_answer(world):
    r = await reflex(world, "db", T0 + 150)
    res = r.result
    assert res["pre_snapshot"] is True
    assert res["search_space"] == 1  # db depends on disk only
    assert res["blast_radius"] == 0  # no incident given: no symptoms counted
    # changes on db or what it depends on (disk), newest first, top 3
    assert [s["id"] for s in res["top_suspects"]] == ["c-late", "c-cfg", "c-disk"]
    assert [p["person_id"] for p in res["on_call"]["on_call"]] == ["per-bob"]
    assert len(r.cypher) == 3 and r.graph == world


async def test_reflex_filters_by_valid_time(world):
    # billing depended on db until T0-1000; ann was on call before T0
    then = await reflex(world, "db", T0 - 2000, window_minutes=1)
    assert then.result["search_space"] == 1
    assert then.result["top_suspects"] == []
    assert then.result["on_call"]["on_call"][0]["person_id"] == "per-ann"


async def test_reflex_runs_on_world_through_registry(world, module_sid):
    ctx = ToolContext(sid=module_sid, world_graph=world)  # no snapshots needed
    r = await call_tool("reflex", {"alert_service_id": "web", "t": T0}, ctx)
    # web depends on api, api-b, db, disk (transitively); nothing depends on web
    assert r.result["search_space"] == 4 and r.result["blast_radius"] == 0
    assert r.graph == world


async def test_reflex_blast_radius_counts_symptomatic_services_at_t(world):
    # inc-two: db at T0+100, api at T0+200
    assert (await reflex(world, "api", T0 + 150, incident_id="inc-two")).result["blast_radius"] == 1
    assert (await reflex(world, "api", T0 + 250, incident_id="inc-two")).result["blast_radius"] == 2
    assert (await reflex(world, "api", T0 + 250, incident_id="nope")).result["blast_radius"] == 0


@pytest.mark.slow
async def test_reflex_under_100ms_on_400_service_world(sid):
    if not await client.graph_exists(naming.seed_world()):
        pytest.skip("seed:world not loaded")
    world = await create_session_world(sid)
    rows = (
        await client.query(
            world,
            "MATCH (i:Incident)-[:HAS]->(s:Symptom)-[:ON]->(v:Service) "
            "RETURN v.id, i.opened_at ORDER BY i.opened_at, s.onset",
            read_only=True,
        )
    ).result_set
    ctx = ToolContext(sid=sid, world_graph=world)
    await reflex(world, rows[0][0], rows[0][1])  # warm the connection, like a running API
    timings = []
    for service_id, t in rows:
        t0 = time.perf_counter()
        await call_tool("reflex", {"alert_service_id": service_id, "t": t}, ctx)
        timings.append((time.perf_counter() - t0) * 1000)
    print(
        f"\nreflex on 400-service world over {len(timings)} alerts: "
        f"median {statistics.median(timings):.1f} ms, max {max(timings):.1f} ms"
    )
    assert max(timings) < 100
