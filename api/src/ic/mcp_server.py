"""MCP server over the read-only tool registry (KTD3, R31).

The same typed tools the agent uses are exposed to external MCP clients (Claude Desktop,
Cursor...). Tools and input schemas come from the registry; every call runs through
``registry.call_tool`` with a *server-built* ``ToolContext`` so the client never names a
graph. ``test_hypothesis`` (it writes the proof ledger) and anything with a ledger target
or write access are not exposed. Two read-only tools sit beside the registry's: ``list_incidents``
(public fields only, never a root cause) and ``get_proof`` (the public ``/api/proof/{ref}``).

Scope (MCP has no browser cookie):

* stdio (``ic mcp``): read-only on ``seed:world`` by default; ``--session SID`` binds the
  session's own world, and ``--incident IID`` the incident whose as-of snapshots the
  snapshot tools use (built on demand when missing and no run is active).
* HTTP (``/mcp``, streamable HTTP, stateless): an ``Authorization: Bearer`` token issued by
  ``POST /api/mcp-token`` binds the call to that session's world and existing snapshots;
  without a token only ``seed:world`` tools answer; a bad or expired token is a 401.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import logging
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from ic.agent import payloads
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.graph.snapshots import build_snapshots
from ic.locks import KeyedLocks
from ic.tools import registry
from ic.tools.registry import ToolContext, ToolSpec
from ic.tools.result import ToolError

if TYPE_CHECKING:
    from ic.store import Store

log = logging.getLogger(__name__)

SERVER_NAME = "incident-commander"
TOKEN_PREFIX = "icmcp_"
TOKEN_TTL_S = 3600
MCP_PATH = "/mcp"
INCIDENT_NOTE = (
    " Uses the incident's as-of snapshots; omit to use the incident this server was started with."
)

_build_locks: KeyedLocks[str] = KeyedLocks()


@dataclass(frozen=True)
class McpScope:
    """What a connection may touch. ``sid=None`` means read-only ``seed:world`` only."""

    sid: str | None = None
    incident_id: str | None = None  # default incident for snapshot tools
    build_missing: bool = False  # stdio only: build absent snapshots on demand
    store: Store | None = None


SEED_SCOPE = McpScope()


def exposed_tools() -> list[ToolSpec]:
    """Registry tools an MCP client may see: read-only, never targeting the ledger."""
    return [t for t in registry.all_tools() if t.read_only and t.target != "ledger"]


# --- context building ------------------------------------------------------------------


async def _incident_time(scope: McpScope, iid: str) -> int:
    assert scope.sid is not None
    if scope.store is not None:
        anchor = scope.store.get_anchor(scope.sid, iid)
        if anchor and anchor.get("t") is not None:
            return int(anchor["t"])
    rows = (
        await query(
            naming.session_world(scope.sid),
            "MATCH (i:Incident {id: $iid}) RETURN i.opened_at",
            {"iid": iid},
            read_only=True,
        )
    ).result_set
    if not rows or rows[0][0] is None:
        raise ToolError(f"unknown incident {iid!r} in this session")
    return int(rows[0][0])


def _active_run(scope: McpScope) -> bool:
    if scope.store is None or scope.sid is None:
        return False
    with scope.store.conn() as c:
        r = c.execute("SELECT active_runs FROM sessions WHERE sid = ?", (scope.sid,)).fetchone()
    return bool(r and r[0] > 0)


async def resolve_snapshots(scope: McpScope, iid: str) -> tuple[str, str]:
    """The session incident's existing as-of graphs (true, known). Never rebuilds while a
    run is active; builds only in stdio ``--incident`` mode, otherwise refuses."""
    assert scope.sid is not None
    true_g, known_g = (naming.asof(scope.sid, iid, v) for v in ("true", "known"))
    if await graph_exists(true_g) and await graph_exists(known_g):
        return true_g, known_g
    if not scope.build_missing:
        raise ToolError(
            f"no snapshots for incident {iid!r} in this session; open the incident in the "
            "console first"
        )
    async with _build_locks(f"{scope.sid}:{iid}"):
        if not (await graph_exists(true_g) and await graph_exists(known_g)):
            if _active_run(scope):
                raise ToolError("a run is active for this session; try again when it finishes")
            if not await graph_exists(naming.session_world(scope.sid)):
                raise ToolError("the session world no longer exists")
            t = await _incident_time(scope, iid)
            if scope.store is not None:
                # same bookkeeping as the API so session cleanup deletes these graphs
                with scope.store.conn() as c:
                    for g in (true_g, known_g):
                        c.execute(
                            "INSERT OR IGNORE INTO session_graphs VALUES (?, ?)", (scope.sid, g)
                        )
            await build_snapshots(naming.session_world(scope.sid), scope.sid, iid, t)
    return true_g, known_g


async def build_context(scope: McpScope, spec: ToolSpec, incident_id: str | None) -> ToolContext:
    """The ToolContext for one call. Graph names derive only from the scope's session id and
    a format-validated incident id, so a client cannot reach another session's graphs."""
    if scope.sid is None:
        if spec.target != "world":
            raise ToolError(
                f"{spec.name} needs an incident snapshot: connect with a session "
                "(`ic mcp --session SID --incident IID`, or an Authorization: Bearer token "
                "from POST /api/mcp-token). Without one only seed:world tools are available."
            )
        return ToolContext(allow_seed_ro=True, world_graph=naming.seed_world())
    ctx = ToolContext(sid=scope.sid, world_graph=naming.session_world(scope.sid))
    if spec.target == "world":
        return ctx
    iid = incident_id or scope.incident_id
    if not iid:
        raise ToolError(f"{spec.name} needs an incident_id")
    true_g, known_g = await resolve_snapshots(scope, iid)
    ctx.iid, ctx.snapshot_true, ctx.snapshot_known = iid, true_g, known_g
    return ctx


# --- tool generation -------------------------------------------------------------------


def _incident_field() -> FieldInfo:
    return FieldInfo(
        default=None,
        max_length=128,
        description="Incident whose snapshots to query." + INCIDENT_NOTE,
    )


def _signature(model: type[BaseModel], add_incident: bool) -> inspect.Signature:
    kw = inspect.Parameter.KEYWORD_ONLY
    params = [
        inspect.Parameter(name, kw, default=info, annotation=info.annotation)
        for name, info in model.model_fields.items()
    ]
    if add_incident:
        params.append(
            inspect.Parameter("incident_id", kw, default=_incident_field(), annotation=str | None)
        )
    return inspect.Signature(params, return_annotation=dict[str, Any])


def _make_handler(spec: ToolSpec, get_scope):
    fields = spec.input_model.model_fields
    add_incident = spec.target == "snapshot" and "incident_id" not in fields

    async def handler(**kwargs: Any) -> dict[str, Any]:
        scope: McpScope = get_scope()
        iid = kwargs.pop("incident_id", None) if add_incident else kwargs.get("incident_id")
        args = {k: v for k, v in kwargs.items() if v is not PydanticUndefined}
        try:
            ctx = await build_context(scope, spec, iid)
            result = await registry.call_tool(spec.name, args, ctx)
        except ToolError as e:
            raise McpToolError(str(e)) from None
        except ValueError as e:  # naming validation of a caller-supplied id
            raise McpToolError(f"invalid argument: {e}") from None
        except Exception:
            log.exception("mcp tool %s failed", spec.name)
            raise McpToolError(f"{spec.name} failed on the server") from None
        return result.model_dump(mode="json")

    handler.__name__ = spec.name
    handler.__signature__ = _signature(spec.input_model, add_incident)  # type: ignore[attr-defined]
    handler.__annotations__ = {
        **{n: f.annotation for n, f in fields.items()},
        **({"incident_id": str | None} if add_incident else {}),
        "return": dict[str, Any],
    }
    return handler


# --- session-level tools (not in the registry) -----------------------------------------

LIST_INCIDENTS = (
    "List the incidents in this connection's world: id, title, state, alert service and alert "
    "time. State is the ledger's investigation state (null until investigated). Never "
    "includes a root cause."
)
GET_PROOF = (
    "The public proof page for a proof ref (the 24-hex ref in a rollback PR's proof link): "
    "incident summary, verdict, the hash-chained proofs with their Cypher, the rehearsal and "
    "the PR. Read-only; it re-verifies nothing."
)
# the answer key lives in SQLite and the graph's own cause links are never selected here
INCIDENTS = (
    "MATCH (i:Incident) RETURN i.id, i.title, i.alert_service_id, i.opened_at "
    "ORDER BY i.opened_at DESC, i.id"
)


async def list_incidents(scope: McpScope) -> dict[str, Any]:
    """Incidents of the scope's world with their ledger state; only public fields."""
    world = naming.session_world(scope.sid) if scope.sid else naming.seed_world()
    rows = (await query(world, INCIDENTS, None, read_only=True)).result_set
    states: dict[str, str] = {}
    if scope.sid and await graph_exists(naming.ledger(scope.sid)):
        res = await query(
            naming.ledger(scope.sid),
            "MATCH (li:LedgerIncident) RETURN li.id, li.state",
            None,
            read_only=True,
        )
        states = {iid: st or "OPEN" for iid, st in res.result_set}
    return {
        "incidents": [
            {
                "id": iid,
                "title": title,
                "state": states.get(iid),
                "alert_service": service,
                "alert_time": payloads.iso(t),
            }
            for iid, title, service, t in rows
        ]
    }


async def get_proof(scope: McpScope, ref: str) -> dict[str, Any]:
    """The same payload as ``GET /api/proof/{ref}``; the ref alone is the credential."""
    from ic.api.routes_proof import proof_payload
    from ic.store import Store

    try:
        return await proof_payload(scope.store or Store(), ref)
    except HTTPException as e:
        raise ToolError(str(e.detail)) from None


INSTRUCTIONS = """\
Read-only graph tools for on-call incident investigation. Every result carries the Cypher \
that produced it. Snapshot tools need a session and an incident.

The rule that decides a root cause (KTD23). A change (deploy, config, flag) or an expiry \
qualifies when, in order:
  after_onset  it happened before the earliest symptom onset;
  origin       its own service is symptomatic and among the first to fail;
  coverage     every symptomatic service is reachable from it over IMPACTS edges;
  order        nearer hops started failing no later than farther hops;
  signature    its kind can produce every observed symptom signature.
It is necessary when no rival qualifies once it is removed.

Recommended order:
  1. list_symptoms: the incident's symptoms (service, onset, signature).
  2. screen_candidates: elimination funnel over every change in the window, cut clause by \
clause; the survivors are the entities that qualify.
  3. causal_path (does one change explain the incident, hop by hop), absence_certificate \
(no other cause qualifies) and necessity (the removal check) confirm a suspect; \
changes_in_window, impact_closure, observed_calls and exonerate are supporting evidence. \
To check a root cause you already have in mind, use verify_claim (prefer it over \
exonerate): it returns VERIFIED, RIVAL (another candidate also qualifies) or REFUTED with \
the first clause it fails and its query. Its entity_id must be a change or expiry id; a \
service id is REFUTED as not_a_change.
  4. find_precedents and recall: past incidents and dependencies learned from earlier \
resolved ones. Advisory memory, never proof.
Other tools: who_is_on_call, rank_chokepoints, reflex. Session tools: list_incidents (what \
is open, with state) and get_proof (a proof ref's public proof page).

Company tools (no incident needed; they read the world graph, the same ones the console's \
Ask panel uses): find_entities turns names into ids ("checkout" finds \
svc-checkout-api@us-east; also teams, people, incidents, postmortems, ADRs); then \
service_profile (owner, on call, recent changes with authors, incidents), team_profile, \
person_profile, decisions_for (ADRs and postmortem recommendations for a service, team or \
incident) and incident_history (incidents with their causes and authors)."""


def build_server(get_scope=lambda: SEED_SCOPE) -> MCPServer:
    """An MCP server whose tools mirror the registry's read-only tools. ``get_scope`` is
    called per tool call (a fixed scope for stdio, a per-request one for HTTP)."""
    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
    )
    for spec in exposed_tools():
        server.add_tool(
            _make_handler(spec, get_scope), name=spec.name, description=spec.description
        )

    async def list_incidents_tool() -> dict[str, Any]:
        try:
            return await list_incidents(get_scope())
        except Exception:
            log.exception("mcp tool list_incidents failed")
            raise McpToolError("list_incidents failed on the server") from None

    async def get_proof_tool(ref: str) -> dict[str, Any]:
        try:
            return await get_proof(get_scope(), ref)
        except ToolError as e:
            raise McpToolError(str(e)) from None
        except Exception:
            log.exception("mcp tool get_proof failed")
            raise McpToolError("get_proof failed on the server") from None

    server.add_tool(list_incidents_tool, name="list_incidents", description=LIST_INCIDENTS)
    server.add_tool(get_proof_tool, name="get_proof", description=GET_PROOF)
    return server


def stdio_server(session: str | None = None, incident: str | None = None) -> MCPServer:
    """The ``ic mcp`` server: seed:world read-only, or a local session (and incident)."""
    if incident and not session:
        raise ValueError("--incident needs --session")
    store = None
    if session:
        naming.session_world(session)  # format check
        if incident:
            naming.asof(session, incident, "known")
        from ic.store import Store

        store = Store()
    scope = McpScope(sid=session, incident_id=incident, build_missing=bool(incident), store=store)
    return build_server(lambda: scope)


# --- tokens (HTTP) ---------------------------------------------------------------------

TOKEN_TABLE = """
CREATE TABLE IF NOT EXISTS mcp_tokens (
    token_hash TEXT PRIMARY KEY,
    sid        TEXT NOT NULL,
    expires_at REAL NOT NULL
)
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(store: Store, sid: str, *, now: float | None = None) -> tuple[str, float]:
    """A fresh bearer token for ``sid`` (only its hash is stored); returns (token, expiry)."""
    now = time.time() if now is None else now
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = now + TOKEN_TTL_S
    with store.conn() as c:
        c.execute(TOKEN_TABLE)
        c.execute("DELETE FROM mcp_tokens WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO mcp_tokens VALUES (?, ?, ?)", (_hash(token), sid, expires))
    return token, expires


def verify_token(store: Store, token: str, *, now: float | None = None) -> str | None:
    """The session id the token is bound to, or None if unknown or expired."""
    now = time.time() if now is None else now
    with store.conn() as c:
        c.execute(TOKEN_TABLE)
        r = c.execute(
            "SELECT sid FROM mcp_tokens WHERE token_hash = ? AND expires_at > ?",
            (_hash(token), now),
        ).fetchone()
    return r[0] if r else None


_current_scope: ContextVar[McpScope] = ContextVar("ic_mcp_scope", default=SEED_SCOPE)


class McpAuthApp:
    """ASGI wrapper for the streamable-HTTP app: per-IP limit, bearer auth, scope binding."""

    def __init__(self, inner, fastapi_app: FastAPI):
        self.inner = inner
        self.fastapi_app = fastapi_app

    @staticmethod
    async def _reply(send, status: int, body: str, headers: list | None = None) -> None:
        raw = body.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode()),
                    *(headers or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        from ic.api import auth, routes

        state = routes.app_state(self.fastapi_app)
        request = Request(scope)
        ip = auth.client_ip(request, state.settings)
        if not state.limiter.allow_mcp(ip):
            await self._reply(send, 429, '{"error": "too many MCP requests"}')
            return
        header = request.headers.get("authorization")
        mcp_scope = SEED_SCOPE
        if header:
            kind, _, token = header.partition(" ")
            sid = verify_token(state.store, token.strip()) if kind.lower() == "bearer" else None
            if sid is None or state.sessions.get(sid) is None:
                await self._reply(
                    send,
                    401,
                    '{"error": "invalid or expired token"}',
                    [(b"www-authenticate", b'Bearer error="invalid_token"')],
                )
                return
            state.sessions.touch(sid)
            mcp_scope = McpScope(sid=sid, store=state.store)
        reset = _current_scope.set(mcp_scope)
        try:
            await self.inner(scope, receive, send)
        finally:
            _current_scope.reset(reset)


def mount_mcp(app: FastAPI) -> MCPServer:
    """Mount streamable HTTP at ``/mcp`` plus the ``POST /api/mcp-token`` route. Stateless
    mode: every request is self-contained, so the per-request scope can never leak between
    clients. Returns the server; its session manager must run inside the app lifespan."""
    from starlette.routing import Route

    from ic.api import auth, routes

    server = build_server(lambda: _current_scope.get())
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        # Bearer-token API with no cookies: DNS-rebinding protection would only reject the
        # real public hostname.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.routes.append(Route(MCP_PATH, endpoint=McpAuthApp(inner, app)))

    @app.post("/api/mcp-token", dependencies=[Depends(routes.require_json)])
    async def mcp_token(request: Request):
        s = routes.get_state(request)
        sess = auth.get_session_or_none(request, s.sessions)
        if sess is None:
            return JSONResponse({"error": "no session: inject a fault first"}, status_code=401)
        ip = auth.client_ip(request, s.settings)
        if not s.limiter.allow_mcp_token(ip):
            return JSONResponse({"error": "too many token requests"}, status_code=429)
        token, expires = issue_token(s.store, sess.sid)
        return {"token": token, "expires_at": expires, "ttl_s": TOKEN_TTL_S, "url": MCP_PATH}

    return server


@contextlib.asynccontextmanager
async def mcp_lifespan(server: MCPServer):
    async with server.session_manager.run():
        yield
