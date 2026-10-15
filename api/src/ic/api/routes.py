"""HTTP surface (U9): sessions, fault injection, signed alerts, run streams, verify,
exonerate, the query X-ray and the recorded-run fallback.

Every state-changing route requires ``Content-Type: application/json`` (415 otherwise):
together with SameSite=Lax cookies and the CORS allowlist that keeps cross-site forms from
driving the API. Over any limit or with the spend breaker tripped, the visitor is pointed
at a recorded run (``{"fallback": "recorded_run", "reason": ...}``).

Application state lives in ``ic.api.state``, the helpers the route modules share in
``ic.api.common``, incident snapshots and the console graph in ``ic.api.graph_view`` and the
cleanup loop in ``ic.api.maintenance``; the names callers used from here are re-exported.
"""

from __future__ import annotations

import json
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sse_starlette import EventSourceResponse

from ic.agent import payloads
from ic.api import auth
from ic.api.common import (
    _IID,
    JSON_ONLY,
    _check_iid,
    _error,
    _ledger_info,
    _new_session,
    _not_ready,
    _require_session,
    _reserve_run,
    _too_many,
    _with_cookie,
)
from ic.api.common import require_json as require_json
from ic.api.events import EventType
from ic.api.graph_view import (
    SnapshotsNotReady,
    _ensure_snapshots,
    _graph_payload,
    _incident_time,
)
from ic.api.maintenance import cleanup_loop as cleanup_loop
from ic.api.maintenance import close_stale_prs_once as close_stale_prs_once
from ic.api.maintenance import github_client as github_client
from ic.api.maintenance import maintenance_once as maintenance_once
from ic.api.state import AUTO as AUTO
from ic.api.state import AppState as AppState
from ic.api.state import app_state as app_state
from ic.api.state import build_state as build_state
from ic.api.state import get_state
from ic.graph import naming
from ic.graph.client import QueryTimeout, explain, graph_exists, profile, query
from ic.graph.snapshots import create_session_world
from ic.incidents import state as st
from ic.proof.verify import verify_incident
from ic.runs.orchestrator import start_run
from ic.runs.recorded import hero_graph_path, load_hero, load_hero_graph
from ic.tools.registry import ToolContext, call_tool
from ic.tools.result import ToolError
from ic.world.causality import CLAUSES
from ic.world.faults import InjectionError, inject_fault

# --- request bodies ---------------------------------------------------------------------


class InjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault_class: Literal["bad_deploy", "config_change", "flag_flip"]
    seed: int | None = Field(None, ge=0, le=2**31 - 1)


class AlertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    incident_id: str = Field(pattern=_IID)
    text: str | None = Field(None, max_length=2000)  # untrusted alert text, data only


class ExonerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str = Field(pattern=_IID)


class XrayBody(BaseModel):
    """Which step to X-ray, by id only: a funnel clause or a proof's ledger seq. There is
    deliberately no field for Cypher or parameters (KTD5); the query is re-derived."""

    model_config = ConfigDict(extra="forbid")
    funnel: str | None = Field(None, pattern=r"^[a-z_]{1,32}$")
    proof: int | None = Field(None, ge=1, le=100_000)

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.funnel is None) == (self.proof is None):
            raise ValueError("name exactly one of funnel (a clause) or proof (a seq)")
        return self


# --- routes -----------------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.post("/session", dependencies=JSON_ONLY)
async def create_session(request: Request):
    s = get_state(request)
    sess = auth.get_session_or_none(request, s.sessions)
    created = False
    if sess is None:
        sess, refusal = await _new_session(request, s)
        if refusal is not None:
            return refusal
        created = True
    assert sess is not None
    body = {"created": created, "idle_ttl_s": s.settings.session_idle_ttl_s}
    return _with_cookie(JSONResponse(body), sess, s, created)


@router.get("/session/incidents")
async def session_incidents(request: Request):
    """Incident ids this session has started; empty without a session. Read-only, creates nothing
    (the console uses it to avoid auto-playing the recorded run over a visitor's own incident)."""
    s = get_state(request)
    sess = auth.get_session_or_none(request, s.sessions)
    if sess is None:
        return {"incidents": []}
    with s.store.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT incident_id FROM runs WHERE sid = ? ORDER BY created_at", (sess.sid,)
        ).fetchall()
    return {"incidents": [r["incident_id"] for r in rows]}


@router.post("/inject", dependencies=JSON_ONLY)
async def inject(body: InjectBody, request: Request):
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
                fault = await inject_fault(
                    world,
                    session_id=sess.sid,
                    fault_class=body.fault_class,
                    rng_seed=seed,
                    store=s.store,
                )
            except InjectionError as e:
                return _with_cookie(_error(422, str(e)), sess, s, created)
            run_id = await start_run(
                sess,
                fault.incident_id,
                fault.alert_time,
                fault.alert_service_id,
                llm=s.llm,
                store=s.store,
                settings=s.settings,
                deps=s.deps,
                scenario=body.fault_class,
            )
            started = True
        finally:
            if not started:
                s.limiter.release_run(sess.sid, ip, reserved)
    out = {
        "run_id": run_id,
        "incident_id": fault.incident_id,
        "alert_service_id": fault.alert_service_id,
        "alert_time": fault.alert_time,
        "events_url": f"/api/runs/{run_id}/events",
        "degraded": s.llm is None,
    }
    return _with_cookie(JSONResponse(out), sess, s, created)


@router.post("/alerts", dependencies=JSON_ONLY)
async def alert_webhook(request: Request):
    s = get_state(request)
    raw = await request.body()
    sig = request.headers.get(auth.SIGNATURE_HEADER)
    if not auth.verify_signature(raw, sig, s.settings.alert_hmac_secret):
        return _error(401, "invalid or missing alert signature")
    try:
        body = AlertBody.model_validate_json(raw)
    except ValidationError as e:
        return _error(422, f"invalid alert: {e.errors(include_url=False)[:3]}")
    sess = s.sessions.get(body.session_id)
    if sess is None:
        return _error(404, "unknown session")
    s.sessions.touch(sess.sid)
    _check_iid(sess.sid, body.incident_id)
    async with s.lock_for(sess.sid):
        reserved, refusal = _reserve_run(s, sess.sid, sess.ip)
        if refusal is not None:
            return refusal
        started = False
        try:
            world = naming.session_world(sess.sid)
            rows = (
                await query(
                    world,
                    "MATCH (i:Incident {id: $iid}) RETURN i.opened_at, i.alert_service_id",
                    {"iid": body.incident_id},
                    read_only=True,
                )
            ).result_set
            if not rows or rows[0][0] is None or not rows[0][1]:
                return _error(404, "unknown incident")
            alert_time, alert_service = int(rows[0][0]), rows[0][1]
            lg = naming.ledger(sess.sid)
            if await graph_exists(lg):
                cur = await st.get_state(lg, body.incident_id)
                if cur is not None and cur != st.State.OPEN:
                    return _error(409, f"incident {body.incident_id} is already {cur.value}")
            run_id = await start_run(
                sess,
                body.incident_id,
                alert_time,
                alert_service,
                llm=s.llm,
                store=s.store,
                settings=s.settings,
                deps=s.deps,
                alert_text=body.text,
            )
            started = True
        finally:
            if not started:
                s.limiter.release_run(sess.sid, sess.ip, reserved)
    return JSONResponse(
        {
            "run_id": run_id,
            "incident_id": body.incident_id,
            "alert_service_id": alert_service,
            "alert_time": alert_time,
            "events_url": f"/api/runs/{run_id}/events",
        },
        status_code=202,
    )


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        return int(raw) if raw not in (None, "") else -1
    except ValueError:
        return -1


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    s = get_state(request)
    run = s.runs.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    sess = auth.get_session_or_none(request, s.sessions)
    if sess is None or sess.sid != run.sid:
        raise HTTPException(403, "this run belongs to another session")
    after = _last_event_id(request)

    async def events():
        async for ev in s.runs.stream(run, after):
            yield {"id": str(ev.id), "data": ev.model_dump_json()}

    return EventSourceResponse(
        events(),
        ping=s.settings.sse_ping_s,
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/incidents/{incident_id}/verify", dependencies=JSON_ONLY)
async def verify(incident_id: str, request: Request):
    """Re-verify the session's chain. Each Verify builds two world copies, so it is bounded:
    a per-(session, incident) debounce and a per-session hourly cap (429 + Retry-After).
    ``verify_incident`` serializes with every other Verify of this incident (remediation's
    Verify-before-acting included) and tracks its snapshots for the session cleanup."""
    s = get_state(request)
    sess = _require_session(request, s)
    sid = sess.sid
    _check_iid(sid, incident_id)
    await _ledger_info(sid, incident_id)
    interval = s.settings.session_verify_interval_s
    if interval > 0 and not s.budget.allow_status_rerun(f"verify:{sid}:{incident_id}", interval):
        return _too_many("verified moments ago; try again shortly", interval)
    wait = s.limiter.session_verify_retry(sid, s.settings.session_verifies_per_hour)
    if wait is not None:
        limit = s.settings.session_verifies_per_hour
        return _too_many(f"limit of {limit} verifies per hour per session reached", wait)
    try:
        report = await verify_incident(
            sid, incident_id, s.store, track_graph=lambda name: s.sessions.track_graph(sid, name)
        )
    except ToolError as e:
        return _error(422, str(e))
    return report.model_dump(mode="json")


@router.post("/incidents/{incident_id}/exonerate", dependencies=JSON_ONLY)
async def exonerate(incident_id: str, body: ExonerateBody, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    sid = sess.sid
    _check_iid(sid, incident_id)
    world = naming.session_world(sid)
    try:
        true_g, known_g = await _ensure_snapshots(s, sid, incident_id)
    except SnapshotsNotReady:
        return _not_ready()
    ctx = ToolContext(
        sid=sid,
        iid=incident_id,
        world_graph=world,
        snapshot_true=true_g,
        snapshot_known=known_g,
        ledger_graph=naming.ledger(sid),
    )
    try:
        r = await call_tool(
            "exonerate", {"incident_id": incident_id, "change_id": body.change_id}, ctx
        )
    except ToolError as e:
        return _error(422, str(e))
    return r.model_dump(mode="json")


def _xray_step(s: AppState, sid: str, iid: str, body: XrayBody) -> tuple[str, dict, str]:
    """(cypher, params, view) for the step, from the tool calls this session's runs of the
    incident recorded (API-owned SQLite, not the editable ledger graph), picked exactly as
    the console's funnel/proof payloads picked the query they show: the newest run's last
    screening for a funnel cut (the console shows the latest one), the newest run's proof
    for a proof seq."""
    by_run = s.runs.incident_events_by_run(sid, iid, EventType.TOOL_CALL)
    if not by_run:
        raise HTTPException(
            404, f"no stored run events for incident {iid} in this session (never run or pruned)"
        )
    newest = by_run[0][0]
    calls = [c for _, c in by_run]
    if body.funnel is not None:
        if body.funnel not in CLAUSES:
            raise HTTPException(404, f"unknown funnel clause {body.funnel!r}")
        screens = [c for r, c in by_run if r == newest and c.get("tool") == "screen_candidates"]
        if not screens:
            raise HTTPException(404, f"incident {iid} has no recorded elimination funnel")
        call = screens[-1]
        q = payloads.funnel_query_index(call["result"], body.funnel)
        used = call.get("graph")
    else:
        pid = f"prf-{iid}-{body.proof}"
        call = next(
            (
                c
                for c in calls
                if c.get("tool") == "test_hypothesis" and (c.get("result") or {}).get("id") == pid
            ),
            None,
        )
        if call is None:
            raise HTTPException(404, f"incident {iid} has no proof {body.proof}")
        q = payloads.proof_query_index(call["result"])
        used = call["result"].get("graph")
    cypher, params = call.get("cypher") or [], call.get("params") or []
    if q is None or not 0 <= q < len(cypher):
        raise HTTPException(404, "no query was recorded for this step")
    view = "true" if used == naming.asof(sid, iid, "true") else "known"
    return cypher[q], (params[q] if q < len(params) else {}), view


# R7 made visible: what the product avoids. A valid-time filter on a variable-length walk of
# the live world is planned as a Filter above the traversal (every path is expanded first,
# then discarded), which is why each incident gets a materialized as-of snapshot instead.
# Planned with EXPLAIN only, never run. OBSERVED_CALL has no valid_from: its first_seen
# stands in, and it never ends.
NAIVE_XRAY_CYPHER = (
    "MATCH p = (s:Service {id: $service})-[:DEPENDS_ON|OBSERVED_CALL*1..6]->(u:Service)\n"
    "WHERE all(r IN relationships(p) WHERE coalesce(r.valid_from, r.first_seen) <= $t\n"
    "  AND (r.valid_to IS NULL OR r.valid_to > $t))\n"
    "RETURN DISTINCT u.id"
)
NAIVE_XRAY_LABEL = "session world (live, every valid time)"
XRAY_LABELS = {"true": "as-of snapshot (true)", "known": "as-of snapshot (as known)"}
XRAY_CACHE_MAX = 512
_DB_DOWN = (RedisConnectionError, RedisTimeoutError, ConnectionError)


async def _naive_plan(s: AppState, sid: str, iid: str) -> dict:
    world = naming.session_world(sid)
    t = await _incident_time(s, sid, iid)
    rows = (
        await query(
            world,
            "MATCH (i:Incident {id: $iid}) RETURN i.alert_service_id",
            {"iid": iid},
            read_only=True,
        )
    ).result_set
    service = rows[0][0] if rows and rows[0][0] else ""
    params = {"service": service, "t": t}
    return {
        "cypher": NAIVE_XRAY_CYPHER,
        "graph_label": NAIVE_XRAY_LABEL,
        "explain": await explain(world, NAIVE_XRAY_CYPHER, params),
    }


async def _xray_body(s: AppState, sid: str, iid: str, cypher: str, params: dict, view: str):
    """The X-ray of one query: its plan, a live profile and the naive plan (R7)."""
    try:
        true_g, known_g = await _ensure_snapshots(s, sid, iid)
    except SnapshotsNotReady:
        return _not_ready()
    graph = true_g if view == "true" else known_g
    plan = await explain(graph, cypher, params)
    prof = await profile(graph, cypher, params, planned=plan)
    return {
        "graph": XRAY_LABELS[view],  # never the graph's name: it carries the session id
        "cypher": cypher,
        "params": params,
        "explain": plan,
        "profile": prof,
        "naive": await _naive_plan(s, sid, iid),
    }


@router.post("/incidents/{incident_id}/xray", dependencies=JSON_ONLY)
async def xray(incident_id: str, body: XrayBody, request: Request):
    """FalkorDB's execution plan (GRAPH.EXPLAIN) and a live per-operator profile
    (GRAPH.PROFILE) of the exact query a funnel cut or proof showed, on the incident's
    as-of snapshot, plus the plan of the naive valid-time traversal the snapshot replaces
    (``naive``). Console only; each call is bounded by ``PLAN_TIMEOUT_MS``. One X-ray per
    session at a time, and a step's answer is cached until the incident is re-derived."""
    s = get_state(request)
    sess = _require_session(request, s)
    sid = sess.sid
    _check_iid(sid, incident_id)
    cypher, params, view = _xray_step(s, sid, incident_id, body)
    step = {"funnel": body.funnel} if body.funnel is not None else {"proof": body.proof}
    key = (sid, incident_id, json.dumps(step, sort_keys=True))
    fingerprint = json.dumps([cypher, params, view], sort_keys=True, default=str)
    async with s.lock_for(f"xray:{sid}"):
        hit = s.xray_cache.get(key)
        if hit is not None and hit["query"] == fingerprint:
            return hit["body"]
        try:
            res = await _xray_body(s, sid, incident_id, cypher, params, view)
        except QueryTimeout as e:
            return _error(504, str(e))
        except _DB_DOWN:
            return _error(503, "FalkorDB is unavailable; try again shortly")
        except (ValueError, ResponseError) as e:
            return _error(422, f"FalkorDB could not plan this query: {e}")
        if isinstance(res, JSONResponse):
            return res
        out = {"incident_id": incident_id, "step": step, **res}
        while len(s.xray_cache) >= XRAY_CACHE_MAX:
            del s.xray_cache[next(iter(s.xray_cache))]  # oldest first
        s.xray_cache[key] = {"query": fingerprint, "body": out}
    return out


@router.get("/incidents/{incident_id}/graph")
async def incident_graph(incident_id: str, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    _check_iid(sess.sid, incident_id)
    try:
        _, known_g = await _ensure_snapshots(s, sess.sid, incident_id)
    except SnapshotsNotReady:
        return _not_ready()
    return await _graph_payload(known_g, incident_id)


@router.get("/recorded/hero")
async def recorded_hero(request: Request):
    s = get_state(request)
    events = load_hero(s.recorded_path)
    if not events:
        return _error(404, "no recorded run available")
    return {"recorded": True, "events": events}


@router.get("/recorded/hero/graph")
async def recorded_hero_graph(request: Request):
    """The incident graph saved with the recorded run (same shape as
    ``/incidents/{id}/graph``), so recorded playback draws the services it names."""
    s = get_state(request)
    graph = load_hero_graph(hero_graph_path(s.recorded_path))
    if not graph:
        return _error(404, "no recorded graph available")
    return graph


@router.get("/learning-curve")
async def learning_curve(request: Request):
    """The published learning-curve run (``ic learn``): public, read-only, no session."""
    s = get_state(request)
    try:
        data = json.loads(s.learning_curve_path.read_text())
    except (OSError, ValueError):
        return _error(404, "no learning curve has been measured yet")
    return data


BENCH_DROP = ("results",)  # per-case rows: large and not shown on the page


@router.get("/benchmark")
async def benchmark(request: Request):
    """The published benchmark report (``ic bench``): public, read-only, no session, and
    no way to trigger a run from here. Per-case rows are dropped (see report.json)."""
    s = get_state(request)
    try:
        data = json.loads(s.benchmark_path.read_text())
    except (OSError, ValueError):
        return _error(404, "no benchmark report has been generated yet")
    if not isinstance(data, dict):
        return _error(404, "no benchmark report has been generated yet")
    return {k: v for k, v in data.items() if k not in BENCH_DROP}
