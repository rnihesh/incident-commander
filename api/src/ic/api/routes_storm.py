"""Storm mode (U17, R24, KTD13): POST /api/storm fires an alert storm into the caller's
session world and collapses it in a background run streamed over the usual SSE route.

The run's events, in order::

    run_started, snapshot, storm(intake) x N, tool_call(collapse_storm), storm(grouped),
    storm(incident) per incident, storm(chokepoints), storm(collapsed), done

Intake events carry counts only (pages received so far, services paging), never one event
per page. Same session handling and limits as ``/api/inject``: the session cookie scopes
every graph, the run counts against the session and global run caps, and the answer key
(each fault's roots) stays in SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ic.api import auth
from ic.api.common import JSON_ONLY, _error, _new_session, _reserve_run, _with_cookie
from ic.api.events import Agent, EventType
from ic.api.state import AppState, get_state
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.graph.snapshots import build_snapshots, create_session_world
from ic.runs.registry import Run
from ic.tools.registry import ToolContext, call_tool
from ic.world.faults import InjectedStorm, InjectionError, inject_storm

log = logging.getLogger(__name__)

PACE_S = 0.2  # pause between storm events so the console can animate the collapse
INTAKE_BATCHES = 8

StormFault = Literal["bad_deploy", "config_change", "flag_flip", "joint"]


class StormBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int | None = Field(None, ge=0, le=2**31 - 1)
    faults: list[StormFault] | None = Field(None, min_length=1, max_length=3)


router = APIRouter(prefix="/api")


@router.post("/storm", dependencies=JSON_ONLY)
async def storm(body: StormBody, request: Request):
    s = get_state(request)
    sess = auth.get_session_or_none(request, s.sessions)
    created = False
    if sess is None:
        sess, refusal = await _new_session(request, s)
        if refusal is not None:
            return refusal
        created = True
    assert sess is not None
    ip = auth.client_ip(request, s.settings)
    async with s.lock_for(sess.sid):
        # Reserve before any await so concurrent storms cannot all pass the ceiling check.
        reserved, refusal = _reserve_run(s, sess.sid, ip)
        if refusal is not None:
            return _with_cookie(refusal, sess, s, created)
        started = False
        try:
            world = naming.session_world(sess.sid)
            if not await graph_exists(world):  # FalkorDB lost it: rebuild from the seed
                await create_session_world(sess.sid)
                s.sessions.track_graph(sess.sid, world)
            seed = body.seed if body.seed is not None else secrets.randbelow(2**31)
            try:
                st = await inject_storm(
                    world,
                    session_id=sess.sid,
                    rng_seed=seed,
                    store=s.store,
                    faults=list(body.faults) if body.faults else None,
                )
            except InjectionError as e:
                return _with_cookie(_error(422, str(e)), sess, s, created)
            run = s.runs.create(sess.sid, st.storm_id)
            s.sessions.run_started(sess.sid)
            task = asyncio.create_task(execute_storm(s, run, st), name=run.id)
            s.runs.track(run, task)
            started = True
        finally:
            if not started:
                s.limiter.release_run(sess.sid, ip, reserved)
    out = {
        "run_id": run.id,
        "storm_id": st.storm_id,
        "pages": st.pages,
        "started_at": st.started_at,
        "ended_at": st.ended_at,
        "events_url": f"/api/runs/{run.id}/events",
    }
    return _with_cookie(JSONResponse(out), sess, s, created)


async def _pace() -> None:
    if PACE_S > 0:
        await asyncio.sleep(PACE_S)


def _intake(pages: list[list], batches: int = INTAKE_BATCHES) -> list[dict]:
    """Cumulative page counts in ``batches`` slices of the storm's timeline."""
    out, seen = [], set()
    n = len(pages)
    for i in range(1, batches + 1):
        upto = round(n * i / batches)
        chunk = pages[:upto]
        seen |= {p[1] for p in chunk}
        out.append(
            {
                "phase": "intake",
                "received": upto,
                "total": n,
                "services_paging": len(seen),
                "at": chunk[-1][0] if chunk else None,
            }
        )
    return out


def _incident_payload(inc: dict) -> dict:
    cands = {c["id"]: c for c in inc["candidates"]}
    explaining = [
        {
            "id": i,
            "kind": cands[i]["kind"],
            "service_id": cands[i]["service_id"],
            "summary": cands[i]["summary"],
            "event_time": cands[i]["event_time"],
        }
        for i in inc["explaining"]
    ]
    return {
        "phase": "incident",
        "component": inc["component"],
        "symptomatic": inc["symptomatic"],
        "alerts": inc["alerts"],
        "first_page": inc["first_page"],
        "explaining": explaining,
        "jointly_caused": inc["jointly_caused"],
        "joint": inc["joint"],
        "alternatives": inc["alternatives"],
        "unexplained": inc["unexplained"],
        "paths": [
            {"label": f"{p['from']} -> {p['to']}", "nodes": p["path"]}
            for p in inc["paths"]
            if p["path"]
        ],
        "checked": len(inc["candidates"]),
    }


async def execute_storm(s: AppState, run: Run, st: InjectedStorm) -> None:
    """Drive one storm run to ``done``. Never raises (errors become ``error`` events)."""
    t0 = time.perf_counter()
    sid, runs = run.sid, s.runs
    world = naming.session_world(sid)
    try:
        await runs.emit(
            run,
            EventType.RUN_STARTED,
            {
                "storm_id": st.storm_id,
                "storm": True,
                "title": f"Alert storm {st.storm_id}",
                "pages": st.pages,
                "started_at": st.started_at,
                "ended_at": st.ended_at,
            },
        )
        for v in naming.VIEWS:
            s.sessions.track_graph(sid, naming.asof(sid, st.storm_id, v))
        handles = await build_snapshots(world, sid, st.storm_id, st.ended_at)
        await runs.emit(
            run,
            EventType.SNAPSHOT,
            {
                "t": st.ended_at,
                "graphs": [
                    {"name": "true", "graph": handles.true_graph},
                    {"name": "known", "graph": handles.known_graph},
                ],
                "build_ms": handles.build_ms,
            },
        )
        pages = (
            await query(
                handles.known_graph,
                "MATCH (:Storm {id: $id})<-[:PART_OF]-(a:Alert) "
                "RETURN a.fired_at, a.service_id ORDER BY a.fired_at, a.id",
                {"id": st.storm_id},
                read_only=True,
            )
        ).result_set
        for ev in _intake(pages):
            await runs.emit(run, EventType.STORM, ev)
            await _pace()

        ctx = ToolContext(
            sid=sid,
            iid=st.storm_id,
            world_graph=world,
            snapshot_true=handles.true_graph,
            snapshot_known=handles.known_graph,
            track_graph=lambda name: s.sessions.track_graph(sid, name),
        )
        r = await call_tool("collapse_storm", {"storm_id": st.storm_id}, ctx)
        await runs.emit(run, EventType.TOOL_CALL, r.model_dump(mode="json"))
        res = r.result
        await runs.emit(
            run,
            EventType.STORM,
            {
                "phase": "grouped",
                "pages_in": res["pages_in"],
                "symptomatic_services": res["symptomatic_services"],
                "candidates_checked": res["candidates_checked"],
                "components": [
                    {
                        "component": i["component"],
                        "alerts": i["alerts"],
                        "services": i["symptomatic"],
                    }
                    for i in res["incidents"]
                ],
                "query": res["wcc_query"],
            },
        )
        await _pace()
        for inc in res["incidents"]:
            await runs.emit(run, EventType.STORM, _incident_payload(inc))
            await _pace()
        await runs.emit(
            run,
            EventType.STORM,
            {
                "phase": "chokepoints",
                "chokepoints": res["chokepoints"],
                "betweenness": res["betweenness"],
                "area": res["area"],
            },
        )
        await _pace()
        await runs.emit(
            run,
            EventType.STORM,
            {
                "phase": "collapsed",
                "pages_in": res["pages_in"],
                "incidents": res["collapsed_to"],
                "jointly_caused": sum(1 for i in res["incidents"] if i["jointly_caused"]),
                "tool_ms": r.ms,
            },
        )
    except asyncio.CancelledError:
        await _close(run, s, t0, error="the run was cancelled")
        raise
    except Exception as e:
        log.exception("storm run %s failed", run.id)
        await _close(run, s, t0, error=f"{type(e).__name__}: {e}")
    finally:
        if not run.finished:
            await _close(run, s, t0)
        s.sessions.run_finished(sid)


async def _close(run: Run, s: AppState, t0: float, *, error: str | None = None) -> None:
    if run.finished:
        return
    try:
        if error is not None:
            await s.runs.emit(
                run, EventType.ERROR, {"code": "failed", "message": error[:500]}, agent=Agent.SYSTEM
            )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        await s.runs.emit(run, EventType.DONE, {"ms": ms}, agent=Agent.SYSTEM)
    except Exception:  # the store itself is failing: nothing more to record
        log.exception("could not close storm run %s", run.id)
