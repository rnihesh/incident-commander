"""Helpers the route modules share: the JSON-only dependency, refusal and error
responses, session creation with its cookie, run-limit reservation and id checks.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ic.api import auth
from ic.api.state import AppState
from ic.graph import naming
from ic.graph.client import graph_exists
from ic.proof import ledger
from ic.sessions.manager import Session
from ic.tools.result import ToolError

RECORDED_URL = "/api/recorded/hero"
_IID = r"^[A-Za-z0-9_-]{1,64}$"


async def require_json(request: Request) -> None:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")


def _refuse(status: int, reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"fallback": "recorded_run", "reason": reason, "recorded_url": RECORDED_URL, **extra},
        status_code=status,
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _too_many(msg: str, retry_after: float) -> JSONResponse:
    wait = max(1, math.ceil(retry_after))
    return JSONResponse(
        {"error": msg, "reason": msg, "retry_after": wait},
        status_code=429,
        headers={"Retry-After": str(wait)},
    )


def _not_ready() -> JSONResponse:
    return JSONResponse(
        {"error": "the investigation is still building its snapshots", "retry": True},
        status_code=409,
    )


async def _new_session(request: Request, s: AppState) -> tuple[Session | None, JSONResponse | None]:
    ip = auth.client_ip(request, s.settings)
    async with s.create_lock:
        if s.sessions.count_active() >= s.settings.max_sessions:
            return None, _refuse(
                503, f"server is at capacity ({s.settings.max_sessions} concurrent sessions)"
            )
        d = s.limiter.check_session_create(ip)
        if not d.allowed:
            return None, _refuse(429, d.reason)
        sess = await s.sessions.create(ip)
        s.limiter.record_session_create(ip)
    return sess, None


def _with_cookie(resp: JSONResponse, sess: Session, s: AppState, created: bool) -> JSONResponse:
    if created:
        auth.set_session_cookie(resp, sess.sid, s.settings)
    return resp


def _run_refusal(s: AppState, sid: str, ip: str) -> JSONResponse | None:
    if s.breaker.tripped():
        return _refuse(503, "daily LLM spend cap reached; serving recorded runs")
    d = s.limiter.check_run(sid, ip)
    return None if d.allowed else _refuse(429, d.reason)


def _reserve_run(s: AppState, sid: str, ip: str) -> tuple[float | None, JSONResponse | None]:
    """(reservation, refusal): check the run limits and, when they pass, record the run in
    the same synchronous stretch (no await in between), so concurrent requests from other
    sessions cannot all pass the check before any records. ``Limiter.release_run`` undoes a
    reservation whose run never started."""
    refusal = _run_refusal(s, sid, ip)
    if refusal is not None:
        return None, refusal
    return s.limiter.reserve_run(sid, ip), None


def _require_session(request: Request, s: AppState) -> Session:
    sess = auth.get_session_or_none(request, s.sessions)
    if sess is None:
        raise HTTPException(401, "no session: inject a fault first")
    return sess


def _check_iid(sid: str, iid: str) -> None:
    try:
        naming.asof(sid, iid, "known")
    except ValueError:
        raise HTTPException(404, "unknown incident") from None


async def _ledger_info(sid: str, iid: str) -> dict:
    graph = naming.ledger(sid)
    if not await graph_exists(graph):
        raise HTTPException(404, f"incident {iid} has not been investigated in this session")
    try:
        return await ledger.incident_info(graph, iid)
    except ToolError:
        raise HTTPException(
            404, f"incident {iid} has not been investigated in this session"
        ) from None


JSON_ONLY = [Depends(require_json)]
