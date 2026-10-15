"""MCP server (U16): tool surface, parity with the registry, scope, bearer auth."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from mcp import Client

from ic import mcp_server
from ic.api import routes
from ic.api.app import create_app
from ic.graph import client, naming
from ic.graph.client import query
from ic.graph.snapshots import build_snapshots, create_session_world
from ic.mcp_server import McpScope, build_server, exposed_tools
from ic.store import Store
from ic.tools import registry
from ic.tools.registry import ToolContext
from ic.world.faults import inject_fault

sys.path.insert(0, str(Path(__file__).parent / "api"))
from api_helpers import http, test_settings  # noqa: E402

NONE_ARGS = {"at": 1_760_000_000}
SESSION_TOOLS = {"list_incidents", "get_proof"}  # read-only, built beside the registry's
COMPANY_TOOLS = {
    "find_entities",
    "service_profile",
    "team_profile",
    "person_profile",
    "decisions_for",
    "incident_history",
}
# Sample arguments (given a seed service id) for the world tools the no-writes test runs.
SEED_SAMPLES = {
    "find_entities": lambda sid: {"search": "checkout"},
    "service_profile": lambda sid: {"service_id": sid},
    "team_profile": lambda sid: {"team_id": "team-001"},
    "person_profile": lambda sid: {"person_id": "per-0001"},
    "decisions_for": lambda sid: {"entity_id": sid},
    "incident_history": lambda sid: {"days": 30},
}


async def drop_session(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


async def seed_count() -> int:
    r = await query(naming.seed_world(), "MATCH (n) RETURN count(n)", None, read_only=True)
    return r.result_set[0][0]


async def a_service(graph: str) -> str:
    r = await query(graph, "MATCH (s:Service) RETURN s.id ORDER BY s.id LIMIT 1", None)
    return r.result_set[0][0]


@pytest.fixture(scope="module")
async def world(tmp_path_factory):
    """Two sessions; session A has an injected fault and built snapshots."""
    client.reset_client()
    store = Store(tmp_path_factory.mktemp("mcp") / "ic.sqlite3")
    sid_a, sid_b = naming.new_session_id(), naming.new_session_id()
    for sid in (sid_a, sid_b):
        await create_session_world(sid)
    fault = await inject_fault(
        naming.session_world(sid_a),
        session_id=sid_a,
        fault_class="bad_deploy",
        rng_seed=3,
        store=store,
    )
    await build_snapshots(naming.session_world(sid_a), sid_a, fault.incident_id, fault.alert_time)
    yield {
        "store": store,
        "a": sid_a,
        "b": sid_b,
        "fault": fault,
        "answer": store.get_answer(fault.incident_id, sid_a),
    }
    for sid in (sid_a, sid_b):
        await drop_session(sid)
    client.reset_client()


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def comparable(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "ms"}


# --- surface ----------------------------------------------------------------------------


async def test_tool_list_is_registry_read_only_tools():
    async with Client(build_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    expect = {t.name for t in registry.all_tools() if t.read_only and t.target != "ledger"}
    assert names == expect | SESSION_TOOLS
    assert "test_hypothesis" not in names
    assert {"who_is_on_call", "causal_path", "list_symptoms", "verify_claim"} <= names
    assert COMPANY_TOOLS <= names  # Ask the graph's company tools (R7)
    for name in COMPANY_TOOLS:
        assert name in mcp_server.INSTRUCTIONS


async def test_schemas_come_from_registry_models():
    async with Client(build_server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert tools["get_proof"].input_schema["required"] == ["ref"]
    assert not tools["list_incidents"].input_schema.get("properties")
    for spec in exposed_tools():
        props = set(tools[spec.name].input_schema["properties"])
        model_props = set(spec.input_model.model_fields)
        assert model_props <= props
        assert props - model_props <= {"incident_id"}  # the only MCP-only selector
        assert tools[spec.name].description == spec.description
        required = set(tools[spec.name].input_schema.get("required", []))
        assert required == {n for n, f in spec.input_model.model_fields.items() if f.is_required()}
        assert not props & registry.FORBIDDEN_ARG_NAMES


# --- list_incidents / get_proof ----------------------------------------------------------


async def test_list_incidents_shows_public_fields_only(world):
    iid, answer = world["fault"].incident_id, world["answer"]
    scope = McpScope(sid=world["a"], store=world["store"])
    async with Client(build_server(lambda: scope)) as c:
        res = await c.call_tool("list_incidents", {})
    assert not res.is_error, res.content[0].text
    incidents = payload(res)["incidents"]
    mine = next(i for i in incidents if i["id"] == iid)
    assert set(mine) == {"id", "title", "state", "alert_service", "alert_time"}
    assert mine["alert_service"] == world["fault"].alert_service_id
    assert mine["alert_time"].endswith("Z")
    assert mine["state"] is None  # not investigated yet: no ledger
    # the answer key never rides along: not the root, not the nonce, not the class
    blob = res.content[0].text
    assert answer["root_id"] not in blob
    assert answer["nonce"] not in blob and answer["fault_class"] not in blob
    assert not any("cause" in k or "root" in k for i in incidents for k in i)


async def test_list_incidents_is_scoped_to_the_session(world):
    iid = world["fault"].incident_id
    scope_b = McpScope(sid=world["b"], store=world["store"])
    async with Client(build_server(lambda: scope_b)) as c:
        res = await c.call_tool("list_incidents", {})
    assert iid not in {i["id"] for i in payload(res)["incidents"]}


async def test_list_incidents_on_seed_has_no_state_or_cause():
    async with Client(build_server()) as c:
        res = await c.call_tool("list_incidents", {})
    assert not res.is_error, res.content[0].text
    for i in payload(res)["incidents"]:
        assert set(i) == {"id", "title", "state", "alert_service", "alert_time"}
        assert i["state"] is None


@pytest.mark.parametrize("ref", ["0123456789abcdef01234567", "xyz", "../../etc/passwd"])
async def test_get_proof_unknown_ref_is_a_tool_error(world, ref):
    scope = McpScope(sid=world["a"], store=world["store"])
    async with Client(build_server(lambda: scope)) as c:
        res = await c.call_tool("get_proof", {"ref": ref})
    assert res.is_error and "unknown proof" in res.content[0].text


# --- seed:world -------------------------------------------------------------------------


async def test_seed_call_matches_direct_registry_call():
    sid = await a_service(naming.seed_world())
    args = {"service_id": sid, **NONE_ARGS}
    async with Client(build_server()) as c:
        res = await c.call_tool("who_is_on_call", args)
    assert not res.is_error
    direct = await registry.call_tool(
        "who_is_on_call",
        args,
        ToolContext(allow_seed_ro=True, world_graph=naming.seed_world()),
    )
    assert comparable(payload(res)) == comparable(direct.model_dump(mode="json"))
    assert set(payload(res)) >= {"result", "cypher", "params", "nodes_touched", "ms"}


async def test_seed_scope_refuses_snapshot_tools():
    async with Client(build_server()) as c:
        res = await c.call_tool("list_symptoms", {"incident_id": "inc-001"})
    assert res.is_error
    assert "snapshot" in res.content[0].text


async def test_no_mcp_call_writes_seed():
    before = await seed_count()
    sid = await a_service(naming.seed_world())
    async with Client(build_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "test_hypothesis" not in names
        bad = await c.call_tool(
            "test_hypothesis", {"kind": "suspect_qualifies", "entity_id": "x", "claim": "y"}
        )
        assert bad.is_error
        for name in sorted(names):
            if name == "who_is_on_call":
                await c.call_tool(name, {"service_id": sid, **NONE_ARGS})
            elif name == "reflex":
                await c.call_tool(name, {"alert_service_id": sid, "t": 1_760_000_000})
            elif name in SEED_SAMPLES:
                res = await c.call_tool(name, SEED_SAMPLES[name](sid))
                assert not res.is_error, (name, res.content[0].text)
            else:
                await c.call_tool(name, {})
    assert await seed_count() == before


async def test_bad_arguments_are_tool_errors():
    async with Client(build_server()) as c:
        res = await c.call_tool("who_is_on_call", {"service_id": "x", "at": -5})
        assert res.is_error
        # an unknown 'graph' argument is dropped by the MCP layer, never forwarded
        other = naming.session_world("0" * 32)
        res = await c.call_tool("who_is_on_call", {"service_id": "x", "at": 1, "graph": other})
        assert not res.is_error and payload(res)["graph"] == "seed:world"


# --- session scope (stdio mode) ---------------------------------------------------------


def session_scope(world, sid_key="a", *, build_missing=False) -> McpScope:
    return McpScope(
        sid=world[sid_key],
        incident_id=world["fault"].incident_id,
        build_missing=build_missing,
        store=world["store"],
    )


async def test_session_call_matches_direct_and_hides_answer_key(world):
    iid = world["fault"].incident_id
    sid = world["a"]
    scope = session_scope(world)
    ctx = ToolContext(
        sid=sid,
        iid=iid,
        world_graph=naming.session_world(sid),
        snapshot_true=naming.asof(sid, iid, "true"),
        snapshot_known=naming.asof(sid, iid, "known"),
    )
    seen = []
    root = world["answer"]["root_id"]
    async with Client(build_server(lambda: scope)) as c:
        for name, args in [
            ("list_symptoms", {"incident_id": iid}),
            ("screen_candidates", {"incident_id": iid}),
            ("absence_certificate", {"incident_id": iid, "suspect_id": root}),
            ("necessity", {"incident_id": iid, "suspect_id": root}),
            ("verify_claim", {"incident_id": iid, "entity_id": root}),
            ("causal_path", {"entity_id": root, "service_id": world["fault"].alert_service_id}),
            ("find_precedents", {"incident_id": iid}),
            ("rank_chokepoints", {}),  # incident from the scope default
        ]:
            res = await c.call_tool(name, args)
            assert not res.is_error, res.content[0].text
            direct = await registry.call_tool(name, args, ctx)
            got, want = payload(res), direct.model_dump(mode="json")
            assert comparable(got) == comparable(want), name
            seen.append(res.content[0].text)
    blob = "\n".join(seen)
    answer = world["answer"]
    assert answer["nonce"] not in blob
    assert answer["fault_class"] not in blob


async def test_other_sessions_resources_are_refused(world):
    iid = world["fault"].incident_id
    # session B has no snapshots for A's incident id
    scope_b = McpScope(sid=world["b"], incident_id=None, store=world["store"])
    async with Client(build_server(lambda: scope_b)) as c:
        res = await c.call_tool("list_symptoms", {"incident_id": iid})
        assert res.is_error and "no snapshots" in res.content[0].text
        # graph names smuggled through the incident id are rejected by name validation
        victim = naming.session_world(world["a"])
        for evil in (victim, f"{iid}:true", "../x"):
            res = await c.call_tool("list_symptoms", {"incident_id": evil})
            assert res.is_error
            assert "invalid" in res.content[0].text


async def test_snapshots_built_on_demand_only_when_idle(world):
    store = world["store"]
    sid = world["b"]
    # a session B incident that exists in its world
    inc = await query(
        naming.session_world(sid), "MATCH (i:Incident) RETURN i.id LIMIT 1", None, read_only=True
    )
    if not inc.result_set:  # a generator change must not hide this regression
        pytest.fail("session world has no Incident to snapshot; the seed world lost its incidents")
    iid = inc.result_set[0][0]
    scope = McpScope(sid=sid, incident_id=iid, build_missing=True, store=store)
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions VALUES (?, '10.0.0.9', 0, 0, 1)", (sid,))
    async with Client(build_server(lambda: scope)) as c:
        res = await c.call_tool("list_symptoms", {"incident_id": iid})
        assert res.is_error and "run is active" in res.content[0].text
        with store.conn() as conn:
            conn.execute("UPDATE sessions SET active_runs = 0 WHERE sid = ?", (sid,))
        res = await c.call_tool("list_symptoms", {"incident_id": iid})
        assert not res.is_error or "no symptoms" in res.content[0].text


# --- HTTP: bearer tokens ----------------------------------------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def mcp_call(c, name: str, args: dict, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await c.post(
        "/mcp", json=rpc("tools/call", {"name": name, "arguments": args}), headers=headers
    )


@pytest.fixture(scope="module")
async def http_app(world, tmp_path_factory):
    store = world["store"]
    app = create_app(test_settings(tmp_path_factory.mktemp("mcpapi")), llm=None, store=store)
    # anyio's task group must be entered and exited in one task: give it its own
    ready, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            ready.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await ready.wait()
    yield app
    stop.set()
    await task


async def session_client(app, sid: str, ip: str):
    """An HTTP client holding the cookie of an existing session row."""
    state = routes.app_state(app)
    with state.store.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, 0)",
            (sid, ip, time.time(), time.time()),
        )
    cl = http(app, ip)
    cl.cookies.set("ic_sid", sid)
    return cl


async def test_http_token_needs_session(http_app):
    async with http(http_app, "10.1.0.1") as c:
        r = await c.post("/api/mcp-token", json={})
        assert r.status_code == 401


async def test_http_tokens_hashed_and_scope_bound(world, http_app):
    sid_a, sid_b = world["a"], world["b"]
    iid = world["fault"].incident_id
    state = routes.app_state(http_app)
    async with await session_client(http_app, sid_a, "10.1.0.2") as ca:
        r = await ca.post("/api/mcp-token", json={})
        assert r.status_code == 200
        body = r.json()
        token_a = body["token"]
        assert body["ttl_s"] == 3600
    async with await session_client(http_app, sid_b, "10.1.0.3") as cb:
        token_b = (await cb.post("/api/mcp-token", json={})).json()["token"]
    with state.store.conn() as c:
        stored = [r[0] for r in c.execute("SELECT token_hash FROM mcp_tokens")]
    assert token_a not in stored and all(len(h) == 64 for h in stored)

    async with http(http_app, "10.1.0.4") as c:
        # no token: seed tools only
        sid = await a_service(naming.seed_world())
        r = await mcp_call(c, "who_is_on_call", {"service_id": sid, **NONE_ARGS})
        assert r.status_code == 200 and not r.json()["result"].get("isError")
        r = await mcp_call(c, "list_symptoms", {"incident_id": iid})
        assert r.json()["result"]["isError"] is True
        # invalid token: 401, never falls back to seed
        r = await mcp_call(
            c, "who_is_on_call", {"service_id": sid, **NONE_ARGS}, token="icmcp_nope"
        )
        assert r.status_code == 401
        # token A reaches A's snapshots
        r = await mcp_call(c, "list_symptoms", {"incident_id": iid}, token=token_a)
        out = r.json()["result"]
        assert not out.get("isError"), out
        assert json.loads(out["content"][0]["text"])["tool"] == "list_symptoms"
        assert json.loads(out["content"][0]["text"])["graph"].startswith(f"s:{sid_a}:")
        # token B (a different session) cannot reach A's incident
        r = await mcp_call(c, "list_symptoms", {"incident_id": iid}, token=token_b)
        assert r.json()["result"]["isError"] is True
        # an external agent checks a claimed root cause (F2): same answer as a direct call
        claim = {"incident_id": iid, "entity_id": world["answer"]["root_id"]}
        r = await mcp_call(c, "verify_claim", claim, token=token_a)
        out = r.json()["result"]
        assert not out.get("isError"), out
        got = json.loads(out["content"][0]["text"])
        ctx = ToolContext(
            sid=sid_a,
            iid=iid,
            world_graph=naming.session_world(sid_a),
            snapshot_true=naming.asof(sid_a, iid, "true"),
            snapshot_known=naming.asof(sid_a, iid, "known"),
        )
        direct = await registry.call_tool("verify_claim", claim, ctx)
        assert comparable(got) == comparable(direct.model_dump(mode="json"))
        assert got["result"]["verdict"] == "VERIFIED"
        r = await mcp_call(c, "verify_claim", claim, token=token_b)
        assert r.json()["result"]["isError"] is True
        # tools/list works with a token and matches the stdio surface
        r = await c.post(
            "/mcp",
            json=rpc("tools/list"),
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token_a}"},
        )
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {t.name for t in exposed_tools()} | SESSION_TOOLS
        # list_incidents follows the token's session
        r = await mcp_call(c, "list_incidents", {}, token=token_a)
        listed = json.loads(r.json()["result"]["content"][0]["text"])["incidents"]
        assert iid in {i["id"] for i in listed}
        r = await mcp_call(c, "list_incidents", {}, token=token_b)
        listed_b = json.loads(r.json()["result"]["content"][0]["text"])["incidents"]
        assert iid not in {i["id"] for i in listed_b}


async def test_http_token_expiry(world, http_app):
    state = routes.app_state(http_app)
    token, expires = mcp_server.issue_token(state.store, world["a"], now=1000.0)
    assert expires == 1000.0 + 3600
    assert mcp_server.verify_token(state.store, token, now=1000.0 + 3599) == world["a"]
    assert mcp_server.verify_token(state.store, token, now=1000.0 + 3601) is None
    async with http(http_app, "10.1.0.5") as c:
        with state.store.conn() as conn:
            conn.execute("UPDATE mcp_tokens SET expires_at = ?", (time.time() - 1,))
        r = await mcp_call(c, "who_is_on_call", {"service_id": "x", "at": 1}, token=token)
        assert r.status_code == 401


async def test_http_per_ip_limit(world, http_app):
    state = routes.app_state(http_app)
    assert all(state.limiter.allow_mcp("10.9.9.9") for _ in range(600))
    assert not state.limiter.allow_mcp("10.9.9.9")
    async with http(http_app, "10.9.9.9") as c:
        r = await mcp_call(c, "who_is_on_call", {"service_id": "x", "at": 1})
        assert r.status_code == 429


async def test_stdio_subprocess_smoke():
    """`ic mcp` over a real stdio pipe: list tools, call who_is_on_call on seed:world."""
    from mcp import StdioServerParameters

    sid = await a_service(naming.seed_world())
    params = StdioServerParameters(command=sys.executable, args=["-m", "ic.cli", "mcp"])
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "who_is_on_call" in names and "test_hypothesis" not in names
        res = await c.call_tool("who_is_on_call", {"service_id": sid, "at": 1_760_000_000})
    assert not res.is_error
    assert payload(res)["graph"] == "seed:world"


def test_rehearse_rollback_is_not_exposed_over_mcp():
    """It clones and deletes graphs, so it stays off the read-only MCP surface."""
    from ic import mcp_server

    assert "rehearse_rollback" not in {t.name for t in mcp_server.exposed_tools()}


def test_instructions_carry_the_domain_primer():
    text = build_server().instructions
    for clause in ("after_onset", "origin", "coverage", "order", "signature"):
        assert clause in text
    for tool in (
        "list_symptoms",
        "screen_candidates",
        "causal_path",
        "absence_certificate",
        "necessity",
        "find_precedents",
        "recall",
        "verify_claim",
    ):
        assert tool in text
    assert (
        text.index("list_symptoms")
        < text.index("screen_candidates")
        < text.index("find_precedents")
    )
    assert len(text) < 3000
