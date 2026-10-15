"""Judge challenge (U2, R5, KTD2, KTD3): "was it my deploy?" answered live.

  GET  /api/incidents/{incident_id}/challenge   the challenges left for this incident
  POST /api/incidents/{incident_id}/challenge   ``verify_claim`` on one of its changes

The judge names a change the incident's investigation eliminated; ``verify_claim`` (the
agent's own predicate) answers VERIFIED, RIVAL or REFUTED with the first failing clause and
the query that decided it. It runs on a *fresh* as-of build of the session world at the
incident's alert time, not on the stored snapshots: those predate any judge tamper, which
would hide it (AE3). The build reuses Verify's ``:verify:`` names and Verify's per-incident
lock (the two would otherwise build and delete the same graphs), is tracked for the session
cleanup before it exists, and is deleted afterwards.

A challenge never writes to the incident (state, ledger, proofs, anchor): a qualifying
answer on a change other than the diagnosed root only reports ``would_overturn:
INCONCLUSIVE``; the judge's existing re-derive is what changes the verdict.

Each (session, incident) gets two challenges, counted in SQLite (API-owned); the third is
429. Naming the diagnosed root answers "this is the diagnosed root" and spends nothing; an
id that is not one of the incident's candidates (the changes and expiries its proofs
touched) is 422 and spends nothing. While a run on the incident is in flight (it owns the
ledger and snapshots) the answer is 409 ``retry``. A re-derive resets the count.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ic.agent.remediation import read_handoff
from ic.api.common import _IID, JSON_ONLY, _check_iid, _error, _ledger_info, _require_session
from ic.api.graph_view import _incident_time
from ic.api.state import AppState, get_state
from ic.graph import naming
from ic.graph.client import delete_graph
from ic.graph.snapshots import build_snapshots
from ic.proof import ledger as L
from ic.proof.verify import _lock as verify_lock
from ic.store import Store
from ic.tools.registry import ToolContext, call_tool
from ic.tools.result import ToolError

CHALLENGES_PER_INCIDENT = 2
QUALIFYING = ("VERIFIED", "RIVAL")

log = logging.getLogger(__name__)


# --- challenge budget (SQLite, API-owned) --------------------------------------------------


def _db(store: Store):
    return store.conn()  # judge_challenges is created by Store.init


def remaining(store: Store, sid: str, iid: str) -> int:
    with _db(store) as c:
        row = c.execute(
            "SELECT used FROM judge_challenges WHERE sid = ? AND incident_id = ?", (sid, iid)
        ).fetchone()
    return max(0, CHALLENGES_PER_INCIDENT - (row["used"] if row else 0))


def spend(store: Store, sid: str, iid: str) -> bool:
    """Take one challenge; False when none is left. One statement: concurrent requests
    cannot both take the last one."""
    with _db(store) as c:
        cur = c.execute(
            "INSERT INTO judge_challenges VALUES (?, ?, 1) "
            "ON CONFLICT (sid, incident_id) DO UPDATE SET used = used + 1 WHERE used < ?",
            (sid, iid, CHALLENGES_PER_INCIDENT),
        )
        return cur.rowcount == 1


def refund(store: Store, sid: str, iid: str) -> None:
    """Give back a challenge whose check could not run."""
    with _db(store) as c:
        c.execute(
            "UPDATE judge_challenges SET used = used - 1 "
            "WHERE sid = ? AND incident_id = ? AND used > 0",
            (sid, iid),
        )


def reset(store: Store, sid: str, iid: str) -> None:
    """A re-derive starts the incident over: two challenges again."""
    with _db(store) as c:
        c.execute("DELETE FROM judge_challenges WHERE sid = ? AND incident_id = ?", (sid, iid))


# --- helpers ------------------------------------------------------------------------------


async def _incident_record(sid: str, iid: str) -> tuple[str | None, set[str]]:
    """(diagnosed root, candidates) from the incident's proofs: the root is the one entity
    whose proofs pass the DIAGNOSED gate, as remediation reads it (None for an INCONCLUSIVE
    or unfinished incident); the candidates are the changes and expiries those proofs
    touched (the absence certificate touches every one it screened). Read from the chain,
    not a snapshot: on-demand snapshot rebuilds would include changes a tamper added later."""
    try:
        proofs = await L.chain(sid, iid)
        handoff = await read_handoff(sid, iid)
    except ToolError:  # LedgerCorrupt included: nothing trustworthy to challenge
        return None, set()
    touched = {n for p in proofs for n in (p.entity_id, *p.nodes_touched)}
    candidates = {n for n in touched if isinstance(n, str) and n != iid and "@" not in n}
    return (handoff.suspect_id if handoff else None), candidates


def _budget(store: Store, sid: str, iid: str) -> dict:
    return {"limit": CHALLENGES_PER_INCIDENT, "remaining": remaining(store, sid, iid)}


# --- request bodies -----------------------------------------------------------------------


class ChallengeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str = Field(pattern=_IID)


# --- routes -------------------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.get("/incidents/{incident_id}/challenge")
async def challenge_budget(incident_id: str, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    _check_iid(sess.sid, incident_id)
    return {"incident_id": incident_id, **_budget(s.store, sess.sid, incident_id)}


@router.post("/incidents/{incident_id}/challenge", dependencies=JSON_ONLY)
async def challenge(incident_id: str, body: ChallengeBody, request: Request):
    s = get_state(request)
    sess = _require_session(request, s)
    sid, iid, pick = sess.sid, incident_id, body.change_id
    _check_iid(sid, iid)
    if s.runs.active_for(sid, iid):
        msg = "the investigation is still running; challenge it once the verdict is in"
        return JSONResponse({"error": msg, "retry": True}, status_code=409)
    await _ledger_info(sid, iid)
    root, candidates = await _incident_record(sid, iid)
    if pick == root:
        return {
            "incident_id": iid,
            "change_id": pick,
            "is_root": True,
            "diagnosed_root": root,
            "verdict": None,
            "would_overturn": None,
            "message": f"{pick} is the diagnosed root; challenge one of the changes it beat",
            **_budget(s.store, sid, iid),
        }
    if pick not in candidates:
        return _error(422, f"{pick} is not one of the changes this incident's run checked")
    t = await _incident_time(s, sid, iid)
    if not spend(s.store, sid, iid):
        msg = f"both challenges for {iid} are used; re-derive the incident to start over"
        return JSONResponse(
            {"error": f"two challenges per incident: {msg}", "reason": msg, "remaining": 0},
            status_code=429,
        )
    try:
        out = await _run_challenge(s, sid, iid, pick, t)
    except ToolError as e:
        _refund_quietly(s.store, sid, iid)
        return _error(422, str(e))
    except BaseException:
        _refund_quietly(s.store, sid, iid)
        raise
    verdict = out["result"]["verdict"]
    return {
        **out["result"],
        "change_id": pick,
        "is_root": False,
        "diagnosed_root": root,
        "would_overturn": "INCONCLUSIVE" if root and verdict in QUALIFYING else None,
        "t": t,
        "snapshot_ms": out["snapshot_ms"],
        "ms": out["ms"],
        **_budget(s.store, sid, iid),
    }


def _refund_quietly(store: Store, sid: str, iid: str) -> None:
    """``refund`` on a failure path: a refund that fails is logged, never raised over the
    error that caused it."""
    try:
        refund(store, sid, iid)
    except Exception:
        log.exception("could not refund the challenge for %s", iid)


async def _run_challenge(s: AppState, sid: str, iid: str, pick: str, t: int) -> dict:
    """``verify_claim`` on fresh ``:verify:`` snapshots of the session world at ``t``, under
    Verify's lock; the snapshots are tracked before they exist and deleted by name
    afterwards, however far the build got."""
    t0 = time.perf_counter()
    world = naming.session_world(sid)
    names = [naming.verify(sid, iid, v) for v in naming.VIEWS]
    async with verify_lock(sid, iid, False):
        for name in names:
            s.sessions.track_graph(sid, name)
        try:
            handles = await build_snapshots(world, sid, iid, t, kind="verify")
            ctx = ToolContext(
                sid=sid,
                iid=iid,
                world_graph=world,
                snapshot_true=handles.true_graph,
                snapshot_known=handles.known_graph,
                view="known",
            )
            r = await call_tool("verify_claim", {"incident_id": iid, "entity_id": pick}, ctx)
        finally:
            for name in names:
                await delete_graph(name)
    return {
        "result": r.result,
        "snapshot_ms": handles.build_ms,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }
