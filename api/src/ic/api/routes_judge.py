"""Judge mode (U14, KTD11, R25-R27): blind commit-reveal investigations and live tampering.

  POST /api/judge/targets                     services of the caller's world for the picker
  GET  /api/judge/services/{service_id}/changes   recent changes on one service
  POST /api/judge/inject                      plant a fault on a chosen service; returns only
                                              the commitment, never the class/root/nonce
  POST /api/judge/{incident_id}/reveal        after the verdict or 120 s: the opened
                                              commitment and how the verdict compares
  POST /api/judge/{incident_id}/tamper        one parameterized edit of ``s:{sid}:world``
  POST /api/judge/{incident_id}/rederive      a new run on the same incident and alert time
                                              (409 while a rollback PR's copies exist)
  POST /api/judge/{incident_id}/fragility     which single tamper edits would flip the latest
                                              DIAGNOSED verdict, tried on throwaway copies of
                                              the as-known snapshot (U9); never writes the
                                              incident's snapshots, world or ledger

Every route is scoped to the caller's session cookie; ids are validated by pattern and then
looked up in that session's world, and every write goes to ``naming.session_world(sid)``
through parameterized Cypher. The answer key is read only by ``reveal`` (SQLite, R26): the
run is started with no scenario label, so neither the agent context nor the event stream
carries the fault class, root id or nonce.
"""

from __future__ import annotations

import math
import re
import secrets
import time
from typing import Annotated, Any, Literal

import networkx as nx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ic.actions import pr_store
from ic.api import auth, routes_challenge
from ic.api.common import (
    _IID,
    JSON_ONLY,
    _check_iid,
    _new_session,
    _require_session,
    _reserve_run,
    _too_many,
    _with_cookie,
)
from ic.api.events import EventType
from ic.api.graph_view import SnapshotsNotReady, _ensure_snapshots, _incident_time
from ic.api.state import AppState, get_state
from ic.graph import naming
from ic.graph.client import delete_graph, graph_exists, query
from ic.graph.schema import OPEN_END
from ic.graph.snapshots import create_session_world
from ic.memory.writeback import write_back_after_reveal
from ic.runs.orchestrator import start_run
from ic.sessions.manager import Session
from ic.store import Store
from ic.tools.registry import ToolContext, call_tool
from ic.tools.result import ToolError
from ic.world import faults
from ic.world.faults import FAULT_CLASSES, InjectionError, inject_fault

REVEAL_TIMEOUT_S = 120
INJECT_ATTEMPTS = 6  # seeded variants tried on the chosen target before giving up
RECENT_CHANGES = 20
LATE_RECORD_S = 300  # a late-arriving planted deploy is recorded this long after the alert
KNOWN_RECORD_S = 60  # ... or this long after it took effect (capped at the alert time)
_SVC = r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_-]{1,32}$"
_SVC_RE = re.compile(_SVC)
_CHG_NUM = re.compile(r"^chg-(\d+)$")
_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

FaultClassName = Literal[
    "bad_deploy", "config_change", "flag_flip", "expiry", "undeclared_coupling"
]
assert set(FaultClassName.__args__) == set(FAULT_CLASSES)  # type: ignore[attr-defined]

SCHEMA = """
CREATE TABLE IF NOT EXISTS judge_rounds (
    sid              TEXT NOT NULL,
    incident_id      TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    latest_run_id    TEXT NOT NULL,
    alert_service_id TEXT NOT NULL,
    alert_time       INTEGER NOT NULL,
    commitment       TEXT NOT NULL,
    injected_at      REAL NOT NULL,
    PRIMARY KEY (sid, incident_id)
);
"""
_ready: set[str] = set()


# --- judge rounds (SQLite, API-owned) ------------------------------------------------------


def _db(store: Store):
    key = str(store.path.resolve())
    if key not in _ready:
        with store.conn() as c:
            c.executescript(SCHEMA)
        _ready.add(key)
    return store.conn()


def put_round(
    store: Store,
    *,
    sid: str,
    incident_id: str,
    run_id: str,
    alert_service_id: str,
    alert_time: int,
    commitment: str,
    injected_at: float | None = None,
) -> None:
    with _db(store) as c:
        c.execute(
            "INSERT OR REPLACE INTO judge_rounds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                incident_id,
                run_id,
                run_id,
                alert_service_id,
                int(alert_time),
                commitment,
                time.time() if injected_at is None else injected_at,
            ),
        )


def get_round(store: Store, sid: str, incident_id: str) -> dict | None:
    with _db(store) as c:
        row = c.execute(
            "SELECT * FROM judge_rounds WHERE sid = ? AND incident_id = ?", (sid, incident_id)
        ).fetchone()
    return dict(row) if row else None


def _set_latest_run(store: Store, sid: str, incident_id: str, run_id: str) -> None:
    with _db(store) as c:
        c.execute(
            "UPDATE judge_rounds SET latest_run_id = ? WHERE sid = ? AND incident_id = ?",
            (run_id, sid, incident_id),
        )


# --- helpers ------------------------------------------------------------------------------


def _reject(status: int, reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": reason, "reason": reason, **extra}, status_code=status)


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


async def _session_or_create(request: Request, s: AppState):
    """(session, created, refusal). Judge pages may open with the picker, before any run."""
    sess = auth.get_session_or_none(request, s.sessions)
    if sess is not None:
        return sess, False, None
    sess, refusal = await _new_session(request, s)
    return sess, True, refusal


async def _ensure_world(s: AppState, sid: str) -> str:
    world = naming.session_world(sid)
    if not await graph_exists(world):  # FalkorDB lost it: rebuild from the seed
        await create_session_world(sid)
        s.sessions.track_graph(sid, world)
    return world


def _session_busy(s: AppState, sid: str) -> bool:
    return any(r.sid == sid for r in s.runs.active())


async def _service_exists(world: str, service_id: str) -> bool:
    if not _SVC_RE.fullmatch(service_id):
        return False
    return bool(await _rows(world, "MATCH (s:Service {id: $id}) RETURN s.id", {"id": service_id}))


async def _incident_row(world: str, iid: str) -> tuple[int, str, int | None] | None:
    """(opened_at, alert_service_id, earliest symptom onset) of an incident in this world."""
    rows = await _rows(
        world,
        "MATCH (i:Incident {id: $iid}) OPTIONAL MATCH (i)-[:HAS]->(y:Symptom) "
        "RETURN i.opened_at, i.alert_service_id, min(y.onset)",
        {"iid": iid},
    )
    if not rows or rows[0][0] is None or not rows[0][1]:
        return None
    opened, alert, onset = rows[0]
    return int(opened), alert, int(onset) if onset is not None else None


def _verdict(s: AppState, run_id: str | None) -> tuple[dict | None, bool]:
    """(latest verdict payload or None, run finished) for a run id."""
    run = s.runs.get(run_id) if run_id else None
    if run is None:  # unknown run: fail closed, the reveal waits for the timeout
        return None, False
    verdicts = run.of(EventType.VERDICT)
    return (verdicts[-1] if verdicts else None), run.finished


# --- request bodies -----------------------------------------------------------------------


class TargetsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = Field(None, max_length=64)
    limit: int = Field(1000, ge=1, le=2000)


class JudgeInjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_service_id: str = Field(pattern=_SVC)
    fault_class: FaultClassName
    seed: int | None = Field(None, ge=0, le=2**31 - 1)


class AddDeployUpstream(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add_deploy_upstream"]
    service_id: str = Field(pattern=_SVC)
    # Negative values place the deploy after the earliest onset (a non-qualifying edit).
    minutes_before_onset: int = Field(ge=-720, le=1440)
    # False: recorded after the alert (late-arriving, absent from the as-known view).
    as_if_known_at_alert: bool = False
    summary: str | None = Field(None, min_length=1, max_length=200)


class RemoveEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["remove_edge"]
    from_service_id: str = Field(pattern=_SVC)
    to_service_id: str = Field(pattern=_SVC)


class ChangeTimestamp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["change_timestamp"]
    change_id: str = Field(pattern=_IID)
    new_valid_from: int = Field(ge=0, le=2**40)


TamperBody = Annotated[
    AddDeployUpstream | RemoveEdge | ChangeTimestamp, Field(discriminator="action")
]
_TAMPER = TypeAdapter(TamperBody)


# --- routes -------------------------------------------------------------------------------

router = APIRouter(prefix="/api/judge")


@router.post("/targets", dependencies=JSON_ONLY)
async def targets(body: TargetsBody, request: Request):
    s = get_state(request)
    sess, created, refusal = await _session_or_create(request, s)
    if refusal is not None:
        return refusal
    assert sess is not None
    world = await _ensure_world(s, sess.sid)
    cat = await faults.target_catalog(world)
    catalog = cat["services"]
    if body.q:
        q = body.q.strip().lower()
        catalog = [
            x
            for x in catalog
            if any(q in str(x[k] or "").lower() for k in ("id", "name", "region", "domain"))
        ]
    shown = catalog[: body.limit]
    ids = {x["id"] for x in shown}
    out = {
        "t_ref": cat["t_ref"],
        "total": len(catalog),
        "fault_classes": list(FAULT_CLASSES),
        "services": shown,
        "edges": [e for e in cat["edges"] if e["source"] in ids and e["target"] in ids],
    }
    return _with_cookie(JSONResponse(out), sess, s, created)


@router.get("/services/{service_id}/changes")
async def service_changes(service_id: str, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    world = naming.session_world(sess.sid)
    if not await _service_exists(world, service_id):
        raise HTTPException(404, "unknown service")
    rows = await _rows(
        world,
        "MATCH (c:Change)-[:CHANGES]->(:Service {id: $id}) "
        "RETURN c.id, c.kind, c.summary, c.valid_from, c.recorded_at "
        "ORDER BY c.valid_from DESC, c.id DESC LIMIT $n",
        {"id": service_id, "n": RECENT_CHANGES},
    )
    expiries = await _rows(
        world,
        "MATCH (e:Expiry)-[:EXPIRES_ON]->(:Service {id: $id}) "
        "RETURN e.id, e.kind, e.summary, e.valid_to ORDER BY e.valid_to DESC LIMIT 5",
        {"id": service_id},
    )
    return {
        "service_id": service_id,
        "changes": [
            {"id": i, "kind": k, "summary": sm, "valid_from": vf, "recorded_at": rec}
            for i, k, sm, vf, rec in rows
        ],
        "expiries": [
            {"id": i, "kind": k, "summary": sm, "valid_to": vt} for i, k, sm, vt in expiries
        ],
    }


@router.post("/inject", dependencies=JSON_ONLY)
async def judge_inject(body: JudgeInjectBody, request: Request):
    s = get_state(request)
    sess, created, refusal = await _session_or_create(request, s)
    if refusal is not None:
        return refusal
    assert sess is not None
    ip = auth.client_ip(request, s.settings)
    async with s.lock_for(sess.sid):
        reserved, refusal = _reserve_run(s, sess.sid, ip)
        if refusal is not None:
            return _with_cookie(refusal, sess, s, created)
        try:
            res = await _judge_inject_reserved(s, sess, created, body)
        except BaseException:
            s.limiter.release_run(sess.sid, ip, reserved)
            raise
        if isinstance(res, JSONResponse):  # refused before any run started
            s.limiter.release_run(sess.sid, ip, reserved)
            return res
    return _with_cookie(JSONResponse(res), sess, s, created)


async def _judge_inject_reserved(
    s: AppState, sess: Session, created: bool, body: JudgeInjectBody
) -> dict | JSONResponse:
    """judge_inject's body, run with the run reservation held: the response payload, or a
    refusal (the caller releases the reservation then)."""
    world = await _ensure_world(s, sess.sid)
    target = body.target_service_id
    why = await faults.alert_path_check(world, target, body.fault_class)
    if why is not None:
        return _with_cookie(
            _reject(422, f"{target} cannot produce an alert path: {why}", target=target),
            sess,
            s,
            created,
        )
    base = body.seed if body.seed is not None else secrets.randbelow(2**31 - INJECT_ATTEMPTS)
    fault, last_error = None, ""
    for k in range(INJECT_ATTEMPTS):
        try:
            fault = await inject_fault(
                world,
                session_id=sess.sid,
                fault_class=body.fault_class,
                rng_seed=base + k,
                target_service=target,
                store=s.store,
            )
            break
        except InjectionError as e:
            last_error = str(e)
    if fault is None:
        reason = (
            f"no {body.fault_class} fault on {target} produces a valid alert path "
            f"({INJECT_ATTEMPTS} variants tried; last: {last_error})"
        )
        return _with_cookie(_reject(422, reason, target=target), sess, s, created)
    run_id = await start_run(
        sess,
        fault.incident_id,
        fault.alert_time,
        fault.alert_service_id,
        llm=s.llm,
        store=s.store,
        settings=s.settings,
        deps=s.deps,
        scenario=None,  # blind: the class never reaches the event stream
    )
    put_round(
        s.store,
        sid=sess.sid,
        incident_id=fault.incident_id,
        run_id=run_id,
        alert_service_id=fault.alert_service_id,
        alert_time=fault.alert_time,
        commitment=fault.commitment,
    )
    return {
        "run_id": run_id,
        "incident_id": fault.incident_id,
        "alert_service_id": fault.alert_service_id,
        "alert_time": fault.alert_time,
        "commitment": fault.commitment,
        "reveal_after_s": REVEAL_TIMEOUT_S,
        "events_url": f"/api/runs/{run_id}/events",
        "degraded": s.llm is None,
    }


async def _grade(s: AppState, world: str, answer: dict, run_id: str | None) -> dict:
    verdict, finished = _verdict(s, run_id)
    outcome = verdict.get("outcome") if verdict else None
    suspect = verdict.get("suspect_id") if verdict else None
    claimed = await faults.claimed_class(world, suspect) if outcome == "DIAGNOSED" else None
    result, why = faults.compare(answer, outcome, suspect, claimed)
    return {
        "run_id": run_id,
        "finished": finished,
        "outcome": outcome,
        "suspect_id": suspect,
        "rival_ids": (verdict or {}).get("rival_ids") or [],
        "claimed_class": claimed,
        "headline": (verdict or {}).get("headline"),
        "result": result,
        "explanation": why,
    }


@router.post("/{incident_id}/reveal", dependencies=JSON_ONLY)
async def judge_reveal(incident_id: str, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    _check_iid(sess.sid, incident_id)
    rnd = get_round(s.store, sess.sid, incident_id)
    if rnd is None:
        raise HTTPException(404, "no judge round for this incident in this session")
    verdict, finished = _verdict(s, rnd["run_id"])
    elapsed = time.time() - rnd["injected_at"]
    if verdict is None and not finished and elapsed < REVEAL_TIMEOUT_S:
        wait = max(1, math.ceil(REVEAL_TIMEOUT_S - elapsed))
        return JSONResponse(
            {
                "error": "the reveal opens at the verdict or 120 s after injection",
                "retry_after": wait,
            },
            status_code=425,
            headers={"Retry-After": str(wait)},
        )
    answer = faults.reveal(s.store, sess.sid, incident_id)
    if answer is None:
        raise HTTPException(404, "no committed fault for this incident")
    world = naming.session_world(sess.sid)
    blind = await _grade(s, world, answer, rnd["run_id"])
    out: dict[str, Any] = {
        "incident_id": incident_id,
        "published_commitment": rnd["commitment"],
        "revealed": {k: answer[k] for k in ("fault_id", "fault_class", "root_id", "nonce")},
        "preimage": answer["preimage"],
        "commitment": answer["commitment"],
        "recomputed": answer["recomputed"],
        "commitment_ok": answer["commitment_ok"] and answer["recomputed"] == rnd["commitment"],
        "timed_out": verdict is None,
        "elapsed_s": round(elapsed, 1),
        "comparison": blind,
        "rederived": None,
    }
    if rnd["latest_run_id"] != rnd["run_id"]:
        out["rederived"] = await _grade(s, world, answer, rnd["latest_run_id"])
    finished_now = not s.runs.active_for(sess.sid, incident_id)  # any run, re-derives too
    out["memory"] = await _learn_on_match(
        sess.sid, incident_id, world, blind, elapsed, run_finished=finished_now
    )
    return out


async def _learn_on_match(
    sid: str, iid: str, world: str, blind: dict, elapsed: float, *, run_finished: bool
):
    """KTD8: a reveal ``match`` may teach memory when the run itself did not reach RESOLVED
    (a run that did was already written back; this is then a no-op). While a run on the
    incident is still in flight the write-back waits for a CLEARED rehearsal (see
    ``write_back_after_reveal``): the reveal opens at the verdict, before remediation."""
    if blind.get("result") != "match":
        return None
    row = await _incident_row(world, iid)
    if row is None:
        return None
    try:
        res = await write_back_after_reveal(
            sid=sid,
            iid=iid,
            match=True,
            resolved_at=row[0] + max(1, math.ceil(elapsed)),
            run_finished=run_finished,
        )
    except Exception:  # memory is a bonus: never fail the reveal over it
        return None
    return res.payload() if res is not None else None


# --- tampering ----------------------------------------------------------------------------


async def _next_change_id(world: str) -> str:
    """The next ``chg-NNNNN`` id: planted changes use the generator's scheme so later
    injections (which number from the max) keep working."""
    ids = [r[0] for r in await _rows(world, "MATCH (c:Change) RETURN c.id")]
    nums = [int(m.group(1)) for i in ids if isinstance(i, str) and (m := _CHG_NUM.match(i))]
    return f"chg-{max(nums, default=0) + 1:05d}"


async def _tamper_add_deploy(
    world: str, iid: str, t: int, alert: str, onset: int | None, b: AddDeployUpstream
) -> dict | JSONResponse:
    if not await _service_exists(world, b.service_id):
        return _reject(422, f"unknown service {b.service_id!r} in this session's world")
    if b.service_id != alert:
        g = await faults._impact_services(world, t)
        if b.service_id not in g or alert not in nx.descendants(g, b.service_id):
            return _reject(
                422,
                f"{b.service_id} is not upstream of the alerting service {alert}: no "
                "dependency path carries its failure there",
            )
    base = onset if onset is not None else t
    vf = base - b.minutes_before_onset * 60
    if vf >= t:
        return _reject(422, "the deploy would take effect after the alert time")
    rec = min(vf + KNOWN_RECORD_S, t) if b.as_if_known_at_alert else t + LATE_RECORD_S
    prev = await _rows(
        world,
        "MATCH (c:Change {kind: 'deploy'})-[:CHANGES]->(:Service {id: $sid}) "
        "WHERE c.valid_from <= $vf RETURN c.value ORDER BY c.valid_from DESC LIMIT 1",
        {"sid": b.service_id, "vf": vf},
    )
    before = prev[0][0] if prev and prev[0][0] else "v1.0.0"
    m = _VERSION.match(str(before))
    value = f"v{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}" if m else "v9.9.9"
    summary = b.summary or f"Deploy {value}: manual hotfix rollout"
    authors = await _rows(
        world,
        "MATCH (p:Person)-[:MEMBER_OF]->(:Team)-[:OWNS]->(:Service {id: $sid}) "
        "RETURN DISTINCT p.id ORDER BY p.id LIMIT 1",
        {"sid": b.service_id},
    )
    new_id = await _next_change_id(world)
    cypher = (
        "MATCH (s:Service {id: $sid}) "
        "CREATE (c:Change {id: $id, kind: 'deploy', service_id: $sid, summary: $summary, "
        "valid_from: $vf, valid_to: $vt, recorded_at: $rec, value: $value, prev_value: $prev}) "
        "CREATE (c)-[:CHANGES]->(s)"
    )
    params = {
        "sid": b.service_id,
        "id": new_id,
        "summary": summary,
        "vf": vf,
        "vt": OPEN_END,
        "rec": rec,
        "value": value,
        "prev": before,
    }
    await query(world, cypher, params)
    statements = [{"cypher": cypher, "params": params}]
    if authors:
        a_cypher = "MATCH (p:Person {id: $pid}), (c:Change {id: $id}) CREATE (p)-[:AUTHORED]->(c)"
        a_params = {"pid": authors[0][0], "id": new_id}
        await query(world, a_cypher, a_params)
        statements.append({"cypher": a_cypher, "params": a_params})
    late = rec > t
    return {
        "summary": (
            f"Created deploy {new_id} on {b.service_id}, effective {b.minutes_before_onset} min "
            f"before the earliest onset and recorded "
            + ("after the alert (late-arriving: only the as-true view sees it)." if late else "")
            + ("before the alert (the as-known view sees it too)." if not late else "")
        ),
        "created": {
            "label": "Change",
            "id": new_id,
            "kind": "deploy",
            "service_id": b.service_id,
            "summary": summary,
            "valid_from": vf,
            "recorded_at": rec,
            "late_arriving": late,
        },
        "node": new_id,
        "statements": statements,
    }


async def _tamper_remove_edge(world: str, b: RemoveEdge) -> dict | JSONResponse:
    for sid_ in (b.from_service_id, b.to_service_id):
        if not await _service_exists(world, sid_):
            return _reject(422, f"unknown service {sid_!r} in this session's world")
    p = {"a": b.from_service_id, "b": b.to_service_id}
    found = await _rows(
        world,
        "MATCH (:Service {id: $a})-[e]->(:Service {id: $b}) "
        "WHERE type(e) IN ['DEPENDS_ON', 'OBSERVED_CALL'] RETURN type(e), e",
        p,
    )
    if not found:
        edge = f"{b.from_service_id} to {b.to_service_id}"
        return _reject(422, f"no DEPENDS_ON or OBSERVED_CALL edge from {edge}")
    statements = []
    for rel in ("DEPENDS_ON", "OBSERVED_CALL"):
        cypher = f"MATCH (:Service {{id: $a}})-[e:{rel}]->(:Service {{id: $b}}) DELETE e"
        await query(world, cypher, p)
        statements.append({"cypher": cypher, "params": p})
    removed = [{"type": typ, "properties": dict(e.properties)} for typ, e in found]
    kinds = ", ".join(sorted({r["type"] for r in removed}))
    return {
        "summary": (
            f"Removed {len(removed)} edge(s) ({kinds}) from {b.from_service_id} to "
            f"{b.to_service_id}."
        ),
        "removed": removed,
        "node": b.to_service_id,
        "statements": statements,
    }


async def _tamper_timestamp(
    world: str, t: int, onset: int | None, b: ChangeTimestamp
) -> dict | JSONResponse:
    rows = await _rows(
        world,
        "MATCH (c:Change {id: $id}) RETURN c.valid_from, c.recorded_at, c.service_id",
        {"id": b.change_id},
    )
    if not rows:
        return _reject(422, f"unknown change {b.change_id!r} in this session's world")
    before, rec, svc = rows[0]
    if before == b.new_valid_from:
        return _reject(422, f"{b.change_id} already takes effect at {before}")
    cypher = "MATCH (c:Change {id: $id}) SET c.valid_from = $vf"
    params = {"id": b.change_id, "vf": b.new_valid_from}
    await query(world, cypher, params)
    where = []
    if onset is not None:
        where.append("before" if b.new_valid_from < onset else "at or after")
    note = f" ({where[0]} the earliest onset)" if where else ""
    if b.new_valid_from > t:
        note += "; after the alert time, so the as-of snapshots no longer contain it"
    return {
        "summary": f"Moved {b.change_id} on {svc} from {before} to {b.new_valid_from}{note}.",
        "changed": {
            "id": b.change_id,
            "service_id": svc,
            "valid_from_before": before,
            "valid_from_after": b.new_valid_from,
            "recorded_at": rec,
        },
        "node": b.change_id,
        "statements": [{"cypher": cypher, "params": params}],
    }


async def _judge_context(request: Request, incident_id: str) -> tuple[AppState, Session, str]:
    s = get_state(request)
    sess = _require_session(request, s)
    _check_iid(sess.sid, incident_id)
    world = naming.session_world(sess.sid)
    if not await graph_exists(world):
        raise HTTPException(404, "unknown incident")
    return s, sess, world


@router.post("/{incident_id}/tamper", dependencies=JSON_ONLY)
async def judge_tamper(incident_id: str, request: Request):
    s, sess, world = await _judge_context(request, incident_id)
    try:
        body = _TAMPER.validate_json(await request.body())
    except ValidationError as e:
        return _reject(422, f"invalid tamper action: {e.errors(include_url=False)[:3]}")
    row = await _incident_row(world, incident_id)
    if row is None:
        raise HTTPException(404, "unknown incident")
    _, alert, onset = row
    async with s.lock_for(sess.sid):
        if _session_busy(s, sess.sid):
            return JSONResponse(
                {"error": "a run is in progress; tamper after it finishes", "retry": True},
                status_code=409,
            )
        t = await _incident_time(s, sess.sid, incident_id)
        if isinstance(body, AddDeployUpstream):
            res = await _tamper_add_deploy(world, incident_id, t, alert, onset, body)
        elif isinstance(body, RemoveEdge):
            res = await _tamper_remove_edge(world, body)
        else:
            res = await _tamper_timestamp(world, t, onset, body)
    if isinstance(res, JSONResponse):
        return res
    return {
        "action": body.action,
        "incident_id": incident_id,
        "graph": world,
        "t": t,
        "earliest_onset": onset,
        **res,
    }


async def _reset_incident(s: AppState, sid: str, iid: str) -> None:
    """Drop the incident's as-of snapshots, ledger chain and anchor so a new run proves it
    from scratch (the ledger's state machine never re-enters TRIAGING), and give the judge
    a fresh pair of challenges for it."""
    for v in naming.VIEWS:
        await delete_graph(naming.asof(sid, iid, v))
    lg = naming.ledger(sid)
    if await graph_exists(lg):
        p = {"iid": iid}
        await query(lg, "MATCH (n:Proof {incident_id: $iid}) DETACH DELETE n", p)
        await query(lg, "MATCH (n:Hypothesis {incident_id: $iid}) DETACH DELETE n", p)
        await query(lg, "MATCH (n:LedgerIncident {id: $iid}) DETACH DELETE n", p)
    with s.store.conn() as c:
        c.execute("DELETE FROM chain_anchor WHERE session_id = ? AND incident_id = ?", (sid, iid))
    pr_store.drop_rehearsal_seal(s.store, sid, iid)
    routes_challenge.reset(s.store, sid, iid)
    s.forget_xray(sid, iid)  # its snapshots are rebuilt: cached plans no longer describe them


@router.post("/{incident_id}/rederive", dependencies=JSON_ONLY)
async def judge_rederive(incident_id: str, request: Request):
    s, sess, world = await _judge_context(request, incident_id)
    row = await _incident_row(world, incident_id)
    if row is None:
        raise HTTPException(404, "unknown incident")
    _, alert, _ = row
    ip = auth.client_ip(request, s.settings)
    async with s.lock_for(sess.sid):
        if _session_busy(s, sess.sid):
            return JSONResponse(
                {"error": "a run is in progress for this session", "retry": True},
                status_code=409,
            )
        if pr_store.has_pr_copies(s.store, sess.sid, incident_id):
            # The open PR's public proof page is bound to this chain (its copies and frozen
            # anchor); a re-derive would reset it. A fresh judge round has no such tie.
            msg = (
                "a rollback PR is open for this incident and its public proof is bound to "
                "this chain; inject a fresh round instead"
            )
            return JSONResponse({"error": msg, "reason": msg, "retry": False}, status_code=409)
        reserved, refusal = _reserve_run(s, sess.sid, ip)
        if refusal is not None:
            return refusal
        started = False
        try:
            t = await _incident_time(s, sess.sid, incident_id)  # before the anchor goes
            await _reset_incident(s, sess.sid, incident_id)
            run_id = await start_run(
                sess,
                incident_id,
                t,
                alert,
                llm=s.llm,
                store=s.store,
                settings=s.settings,
                deps=s.deps,
                scenario=None,
            )
            started = True
        finally:
            if not started:
                s.limiter.release_run(sess.sid, ip, reserved)
        rnd = get_round(s.store, sess.sid, incident_id)
        if rnd is not None:
            _set_latest_run(s.store, sess.sid, incident_id, run_id)
    return {
        "run_id": run_id,
        "incident_id": incident_id,
        "alert_service_id": alert,
        "alert_time": t,
        "previous_run_id": rnd["latest_run_id"] if rnd else None,
        "events_url": f"/api/runs/{run_id}/events",
        "degraded": s.llm is None,
    }


# --- fragility map (U9) -------------------------------------------------------------------


def _latest_verdict(s: AppState, sid: str, iid: str) -> dict | None:
    """The verdict of this session's latest run on the incident (a re-derive replaces it)."""
    with s.store.conn() as c:
        row = c.execute(
            "SELECT run_id FROM runs WHERE sid = ? AND incident_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (sid, iid),
        ).fetchone()
    return _verdict(s, row["run_id"])[0] if row else None


# What the web's FragilityResponse / FragilityEdit declare; the tool's internals (clone and
# source graph names, which carry the session id, the copy's write Cypher, query text) stay
# server-side.
FRAGILITY_KEYS = frozenset(
    {
        "incident_id",
        "root_id",
        "applicable",
        "baseline",
        "earliest_onset",
        "t",
        "path",
        "tried",
        "flips",
        "copies",
        "skipped",
        "out_of_time",
        "max_edits",
        "copy_ms",
        "detail",
    }
)
FRAGILITY_EDIT_KEYS = frozenset(
    {
        "kind",
        "label",
        "tamper",
        "prediction",
        "verdict",
        "clause",
        "detail",
        "rivals",
        "flips",
        "copy_ms",
        "hop",
        "near",
    }
)


def _fragility_body(res: dict) -> dict:
    out = {k: v for k, v in res.items() if k in FRAGILITY_KEYS}
    for key in ("tried", "flips"):
        out[key] = [{k: v for k, v in e.items() if k in FRAGILITY_EDIT_KEYS} for e in res[key]]
    return out


def _fragility_allowance(s: AppState, sid: str, iid: str) -> JSONResponse | None:
    """The per-(session, incident) debounce and the per-session hourly cap, as for Verify."""
    interval = s.settings.session_fragility_interval_s
    if interval > 0 and not s.budget.allow_status_rerun(f"fragility:{sid}:{iid}", interval):
        return _too_many("mapped moments ago; try again shortly", interval)
    limit = s.settings.session_fragility_per_hour
    wait = s.limiter.session_fragility_retry(sid, limit)
    if wait is not None:
        return _too_many(f"limit of {limit} fragility maps per hour per session reached", wait)
    return None


@router.post("/{incident_id}/fragility", dependencies=JSON_ONLY)
async def judge_fragility(incident_id: str, request: Request):
    """R13/R14: try at most ten single edits on throwaway copies of the incident's as-known
    snapshot and return the ones that would flip the latest DIAGNOSED verdict, each with the
    exact tamper body that applies it to the world. Reads the incident; writes only the
    copies (``s:{sid}:storm:fragility-*``, tracked and deleted). Each map makes up to ten
    copies, so like Verify it is bounded: a per-(session, incident) debounce and a
    per-session hourly cap (429 + Retry-After)."""
    s, sess, world = await _judge_context(request, incident_id)
    if await _incident_row(world, incident_id) is None:
        raise HTTPException(404, "unknown incident")
    async with s.lock_for(sess.sid):
        if _session_busy(s, sess.sid):
            return JSONResponse(
                {"error": "a run is in progress; try again after it finishes", "retry": True},
                status_code=409,
            )
        verdict = _latest_verdict(s, sess.sid, incident_id)
        if not verdict or verdict.get("outcome") != "DIAGNOSED" or not verdict.get("suspect_id"):
            outcome = (verdict or {}).get("outcome") or "no verdict"
            return _reject(409, f"the fragility map needs a DIAGNOSED verdict ({outcome})")
        # Counted only once a map will run, so a 409 the console retries spends nothing.
        refusal = _fragility_allowance(s, sess.sid, incident_id)
        if refusal is not None:
            return refusal
        try:
            true_g, known_g = await _ensure_snapshots(s, sess.sid, incident_id)
        except SnapshotsNotReady:
            return JSONResponse(
                {"error": "snapshots are being built; retry shortly", "retry": True},
                status_code=409,
            )
        ctx = ToolContext(
            sid=sess.sid,
            iid=incident_id,
            world_graph=world,
            snapshot_true=true_g,
            snapshot_known=known_g,
            view="known",  # the verdict's view: what was known at alert time
            track_graph=lambda g: s.sessions.track_graph(sess.sid, g),
        )
        try:
            r = await call_tool(
                "fragility_map",
                {"incident_id": incident_id, "root_id": verdict["suspect_id"]},
                ctx,
            )
        except ToolError as e:  # ScopeError included
            return _reject(422, str(e))
    return {**_fragility_body(r.result), "ms": r.ms}
