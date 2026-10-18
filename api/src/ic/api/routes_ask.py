"""Ask the graph (R1-R6, KTD5): plain-English questions about the company in the graph.

  POST /api/ask   {question, incident_id?}

Session-scoped like inject (a first question starts a session) and answered on the
session's own world graph, which is rebuilt from the seed if FalkorDB lost it. When the
visitor is looking at an incident whose snapshots exist (and no run owns them right now),
the incident's read-only snapshot tools join the company tools. Single turn: nothing is
remembered between questions, and nothing is written to any graph or the ledger.

Bounded: ``session_asks_per_hour`` questions per session (429 with Retry-After past it),
500 characters per question, token usage counted toward the daily spend breaker; with no
model, or once the breaker trips, the deterministic router answers in limited mode.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ic import usage
from ic.agent.ask import QUESTION_MAX, ask
from ic.api import auth
from ic.api.common import _IID, JSON_ONLY, _error, _new_session, _too_many, _with_cookie
from ic.api.state import AppState, get_state
from ic.graph import naming
from ic.graph.client import graph_exists
from ic.graph.snapshots import create_session_world
from ic.tools.registry import ToolContext


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=QUESTION_MAX)
    incident_id: str | None = Field(None, pattern=_IID)


router = APIRouter(prefix="/api")


@router.post("/ask", dependencies=JSON_ONLY)
async def ask_graph(body: AskBody, request: Request):
    s = get_state(request)
    question = body.question.strip()
    if not question:
        return _error(422, "ask a question")
    sess = auth.get_session_or_none(request, s.sessions)
    created = False
    if sess is None:
        sess, refusal = await _new_session(request, s)
        if refusal is not None:
            return refusal
        created = True
    assert sess is not None
    sid = sess.sid
    limit = s.settings.session_asks_per_hour
    wait = s.limiter.session_ask_retry(sid, limit)
    if wait is not None:
        msg = f"limit of {limit} questions per hour per session reached"
        return _with_cookie(_too_many(msg, wait), sess, s, created)
    usage.bump(s.store, "asks", ip=auth.client_ip(request, s.settings))

    world = naming.session_world(sid)
    async with s.lock_for(sid):
        if not await graph_exists(world):  # FalkorDB lost it: rebuild from the seed
            await create_session_world(sid)
            s.sessions.track_graph(sid, world)
    ctx, incident_tools = await _context(s, sid, world, body.incident_id)
    res = await ask(
        question,
        ctx,
        s.llm,
        incident_id=body.incident_id,
        incident_tools=incident_tools,
        on_usage=s.breaker.record_usage,
        should_stop=s.breaker.tripped,
    )
    return _with_cookie(JSONResponse(res.to_dict()), sess, s, created)


async def _context(s: AppState, sid: str, world: str, iid: str | None) -> tuple[ToolContext, bool]:
    """The world-only context, plus the incident's snapshots when they exist and no run is
    using them (a run rebuilds them)."""
    ctx = ToolContext(sid=sid, world_graph=world)
    if not iid or s.runs.active_for(sid, iid):
        return ctx, False
    true_g, known_g = naming.asof(sid, iid, "true"), naming.asof(sid, iid, "known")
    if not (await graph_exists(true_g) and await graph_exists(known_g)):
        return ctx, False
    ctx.iid, ctx.snapshot_true, ctx.snapshot_known = iid, true_g, known_g
    return ctx, True
