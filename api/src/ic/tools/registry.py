"""One typed tool registry for the agent, the MCP server and the UI (KTD3).

A tool is a plain async function ``fn(graph, **args) -> ToolResult`` plus a pydantic input
model (tools registered with ``with_context=True`` also get ``ctx=`` so they can call other
tools, e.g. ``test_hypothesis`` running its falsifier on the snapshot). The LLM only ever
picks a tool name and its domain arguments; which graph the tool runs on is decided here,
from the server-side ``ToolContext``, and checked against the caller's scope before
anything executes.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ic.graph import naming
from ic.tools.result import ToolError, ToolResult

Target = Literal["snapshot", "world", "ledger"]

# Names that must never appear in an LLM-facing schema: graph choice is server-side only.
FORBIDDEN_ARG_NAMES = frozenset({"graph", "graph_name", "world", "sid", "session", "view"})


class ScopeError(ToolError):
    """The tool would touch a graph outside the caller's scope (KTD3)."""


class ToolArgumentError(ToolError, ValueError):
    """Arguments failed validation against the tool's input model."""


class ToolInput(BaseModel):
    """Base for tool input models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolContext:
    """Server-side facts about the run; the LLM never sees or sets any of these."""

    sid: str | None = None
    iid: str | None = None
    run: str | None = None
    allow_seed_ro: bool = False
    world_graph: str | None = None
    snapshot_true: str | None = None
    snapshot_known: str | None = None
    ledger_graph: str | None = None
    view: Literal["known", "true"] = "known"  # verdicts use what was known at alert time
    emit: Callable[[ToolResult], Any] | None = None
    # The run's own alert time and world graph. When set, the proof ledger roots the hash
    # chain at these instead of reading them back from the (editable) ledger graph.
    trusted_t: int | None = None
    trusted_world: str | None = None
    # Called with the exact name of any scratch graph a tool creates (the rehearsal clone),
    # before it exists, so the session cleanup can delete it even if the process dies.
    track_graph: Callable[[str], Any] | None = None

    def graph_for(self, target: Target) -> str:
        if target == "world":
            graph = self.world_graph
        elif target == "ledger":
            graph = self.ledger_graph
        elif self.view in naming.VIEWS:
            graph = self.snapshot_known if self.view == "known" else self.snapshot_true
        else:
            raise ToolError(f"invalid view {self.view!r}")
        if graph is None:
            raise ToolError(f"no {target} graph is available for this run yet")
        return graph

    def allows(self, graph: str) -> bool:
        return naming.scope_allows(
            graph, sid=self.sid, iid=self.iid, run=self.run, allow_seed_ro=self.allow_seed_ro
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[..., Awaitable[ToolResult]]
    read_only: bool = True
    target: Target = "snapshot"
    with_context: bool = False


_TOOLS: dict[str, ToolSpec] = {}


def register(
    input_model: type[BaseModel],
    *,
    name: str | None = None,
    target: Target = "snapshot",
    read_only: bool = True,
    with_context: bool = False,
):
    """Decorator: register ``fn(graph, **args)``; the docstring's first paragraph describes it."""

    bad = FORBIDDEN_ARG_NAMES & set(input_model.model_fields)
    if bad:
        raise TypeError(f"{input_model.__name__} exposes server-side fields: {sorted(bad)}")

    def deco(fn: Callable[..., Awaitable[ToolResult]]):
        doc = inspect.getdoc(fn) or ""
        spec = ToolSpec(
            name=name or fn.__name__,
            description=" ".join(doc.split("\n\n")[0].split()),
            input_model=input_model,
            fn=fn,
            read_only=read_only,
            target=target,
            with_context=with_context,
        )
        _TOOLS[spec.name] = spec
        return fn

    return deco


def _load_builtin_tools() -> None:
    # Imported lazily: the tool modules import ``register`` from here.
    import ic.tools.storm  # noqa: F401  (U17 alert-storm collapse)
    from ic.proof import ledger  # noqa: F401
    from ic.tools import (  # noqa: F401
        claims,
        company,
        fragility,
        investigate,
        precedent,
        recall,
        reflex,
        rehearse,
    )


def all_tools() -> list[ToolSpec]:
    _load_builtin_tools()
    return [_TOOLS[n] for n in sorted(_TOOLS)]


def get_tool(name: str) -> ToolSpec:
    _load_builtin_tools()
    try:
        return _TOOLS[name]
    except KeyError:
        raise ToolError(f"unknown tool {name!r}") from None


def check_scope(spec: ToolSpec, graphs: list[str], ctx: ToolContext) -> None:
    for g in graphs:
        if g == naming.seed_world() and ctx.allow_seed_ro and not spec.read_only:
            raise ScopeError(f"{spec.name} writes; {g} is read-only")
        if not ctx.allows(g):
            raise ScopeError(f"{spec.name} may not touch {g} from this run's scope")


async def call_tool(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
    """Validate args, pick and scope-check the graph, run the tool, time it, emit it."""
    spec = get_tool(name)
    # Strict schemas send null for omitted optional fields: let the model's default apply.
    supplied = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        parsed = spec.input_model.model_validate(supplied)
    except ValidationError as e:
        raise ToolArgumentError(f"{name}: {e}") from e
    graph = ctx.graph_for(spec.target)
    check_scope(spec, [graph], ctx)

    t0 = time.perf_counter()
    kwargs = parsed.model_dump()
    if spec.with_context:
        kwargs["ctx"] = ctx
    result = await spec.fn(graph, **kwargs)
    result.ms = round((time.perf_counter() - t0) * 1000, 2)
    result.graph = graph
    if ctx.emit is not None:
        maybe = ctx.emit(result)
        if inspect.isawaitable(maybe):
            await maybe
    return result


# --- OpenAI Chat Completions schemas (strict mode) -------------------------------------

_KEEP = ("type", "description", "enum", "anyOf", "items")


def strict_parameters(model: type[BaseModel]) -> dict:
    """JSON schema for strict function calling: every property required, optional ones
    nullable (null means "use the default"), no additional properties."""
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    props: dict[str, dict] = {}
    for prop_name, prop in schema.get("properties", {}).items():
        p = {k: v for k, v in prop.items() if k in _KEEP}
        if prop_name not in required:
            default = model.model_fields[prop_name].default
            p["description"] = f"{p.get('description', '')} (null = default {default})".strip()
            if isinstance(p.get("type"), str):
                p["type"] = [p["type"], "null"]
            elif "anyOf" in p and {"type": "null"} not in p["anyOf"]:
                p["anyOf"] = [*p["anyOf"], {"type": "null"}]
        props[prop_name] = p
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def openai_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": strict_parameters(spec.input_model),
                "strict": True,
            },
        }
        for spec in all_tools()
    ]
