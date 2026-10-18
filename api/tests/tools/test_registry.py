"""Registry contract (KTD3): strict LLM schemas, argument validation, scope checks."""

import pytest

from ic.graph import client, naming
from ic.tools.registry import (
    FORBIDDEN_ARG_NAMES,
    ScopeError,
    ToolArgumentError,
    ToolContext,
    ToolInput,
    call_tool,
    openai_tool_schemas,
    register,
)
from ic.tools.result import ToolError

EXPECTED_TOOLS = {
    "list_symptoms",
    "changes_in_window",
    "impact_closure",
    "causal_path",
    "screen_candidates",
    "absence_certificate",
    "necessity",
    "exonerate",
    "verify_claim",
    "who_is_on_call",
    "observed_calls",
    "rank_chokepoints",
    "reflex",
    "test_hypothesis",
    "find_precedents",
    "recall",
    "rehearse_rollback",
    "collapse_storm",
    "fragility_map",
    "find_entities",
    "service_profile",
    "team_profile",
    "person_profile",
    "decisions_for",
    "incident_history",
    "incident_proofs",
}


def _keys(obj) -> set[str]:
    """Every dict key anywhere in a JSON-like structure."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def test_strict_schemas():
    schemas = openai_tool_schemas()
    assert {s["function"]["name"] for s in schemas} == EXPECTED_TOOLS
    for s in schemas:
        f = s["function"]
        assert s["type"] == "function" and f["strict"] is True and f["description"]
        params = f["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert "default" not in _keys(params) and "title" not in _keys(params)
        for prop in params["properties"].values():
            assert "type" in prop or "anyOf" in prop


def test_optional_fields_are_nullable():
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in openai_tool_schemas()}
    window = by_name["screen_candidates"]["properties"]["window_minutes"]
    assert window["type"] == ["integer", "null"]
    assert by_name["screen_candidates"]["properties"]["incident_id"]["type"] == "string"


def test_no_graph_or_session_fields_in_any_schema():
    for s in openai_tool_schemas():
        props = set(s["function"]["parameters"]["properties"])
        assert not props & FORBIDDEN_ARG_NAMES, s["function"]["name"]
        assert not any("graph" in p or "world" in p or "session" in p for p in props)


def test_register_refuses_server_side_fields():
    class Leaky(ToolInput):
        graph: str

    with pytest.raises(TypeError, match="server-side"):
        register(Leaky)


def _ctx(sid: str, **kw) -> ToolContext:
    return ToolContext(sid=sid, iid="i1", **kw)


async def test_unknown_and_invalid_args_rejected():
    sid = naming.new_session_id()
    ctx = _ctx(sid, snapshot_known=naming.asof(sid, "i1", "known"))
    with pytest.raises(ToolArgumentError, match="graph"):
        await call_tool("list_symptoms", {"incident_id": "inc-1", "graph": "seed:world"}, ctx)
    with pytest.raises(ToolArgumentError):
        await call_tool("list_symptoms", {}, ctx)
    with pytest.raises(ToolArgumentError):
        await call_tool("rank_chokepoints", {"top_k": 0}, ctx)
    with pytest.raises(ToolError, match="unknown tool"):
        await call_tool("drop_graph", {}, ctx)


async def test_other_sessions_graph_is_out_of_scope():
    a, b = naming.new_session_id(), naming.new_session_id()
    stolen = _ctx(a, snapshot_known=naming.asof(b, "i1", "known"))
    with pytest.raises(ScopeError):
        await call_tool("list_symptoms", {"incident_id": "inc-1"}, stolen)
    world_b = _ctx(a, world_graph=naming.session_world(b))
    with pytest.raises(ScopeError):
        await call_tool("reflex", {"alert_service_id": "x", "t": 1}, world_b)
    seed = _ctx(a, world_graph=naming.seed_world())  # seed is read-only and opt-in
    with pytest.raises(ScopeError):
        await call_tool("who_is_on_call", {"service_id": "x", "at": 1}, seed)
    with pytest.raises(ToolError, match="no snapshot graph"):
        await call_tool("list_symptoms", {"incident_id": "inc-1"}, _ctx(a))


async def test_view_picks_the_snapshot_and_emit_sees_results():
    client.reset_client()
    sid = naming.new_session_id()
    true_g, known_g = naming.asof(sid, "i1", "true"), naming.asof(sid, "i1", "known")
    try:
        for g, onset in ((true_g, 10), (known_g, 20)):
            await client.query(
                g,
                "CREATE (:Incident {id: 'inc-1', opened_at: 30})-[:HAS]->"
                "(:Symptom {id: 'sym-1', onset: $onset, signature: 'latency'})-[:ON]->"
                "(:Service {id: 'svc-a'})",
                {"onset": onset},
            )
        seen = []

        async def emit(result):
            seen.append(result.tool)

        ctx = ToolContext(sid=sid, snapshot_true=true_g, snapshot_known=known_g, emit=emit)
        r = await call_tool("list_symptoms", {"incident_id": "inc-1"}, ctx)
        assert r.graph == known_g and r.result["earliest_onset"] == 20  # known by default
        ctx.view = "true"
        args = {"incident_id": "inc-1", "window_minutes": None}
        r = await call_tool("changes_in_window", args, ctx)
        assert r.graph == true_g and r.result["window"]["minutes"] == 120  # null -> default
        assert seen == ["list_symptoms", "changes_in_window"]
        sync_seen = []
        ctx.emit = sync_seen.append
        await call_tool("list_symptoms", {"incident_id": "inc-1"}, ctx)
        assert len(sync_seen) == 1
    finally:
        for g in (true_g, known_g):
            await client.delete_graph(g)
        client.reset_client()
