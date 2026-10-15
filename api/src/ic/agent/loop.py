"""The thin agent loop (KTD5): send messages and tool schemas, run each tool call through
the registry, feed the result back, repeat until a terminal tool accepts or a limit hits.

Nothing here chooses graphs: tools run on whatever the server-side ``ToolContext`` says,
and the model can only name a tool and its domain arguments. Tool failures go back to the
model as error messages; only the LLM itself failing (twice in a step) ends the loop early.

Events (``emit(type, data, agent=...)``): ``tool_call`` per registry call, ``hypothesis`` and
``proof`` per ``test_hypothesis``, ``narration`` for any text the model writes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from ic.agent import payloads
from ic.agent.llm import LLMClient, LLMReply, ToolCall
from ic.api.events import Agent, Event, EventType
from ic.tools import registry
from ic.tools.registry import ToolContext
from ic.tools.result import ToolResult

Emit = Callable[..., Any]  # emit(EventType, data: dict, *, agent: Agent) -> None | awaitable
UsageHook = Callable[[str, int, int], Any]  # (model, prompt_tokens, completion_tokens)

# Keys never sent back to the model: timings, hashes, graph names, positional query
# indexes and raw Cypher. They are noise for reasoning, and dropping them keeps the
# transcript key stable across sessions (KTD21).
MODEL_DROP_KEYS = frozenset(
    {
        "ms",
        "graph",
        "cypher",
        "params",
        "nodes_touched",
        "hash",
        "prev_hash",
        "prev_id",
        "result_hash",
        "conclusion_hash",
        "registered_at",
        "executed_at",
        "sid",
        "query",
        "queries",
        "display_path",
        "hops",
        "falsifier_args",
        "evidence_hash",
        "hypothesis_hash",
    }
)
NUDGE = "Continue the investigation by calling a tool. When the proofs are in, call submit_verdict."


@dataclass
class LoopLimits:
    max_steps: int = 25  # LLM turns
    step_timeout_s: float = 20.0  # per LLM call
    retries: int = 1  # extra attempts per step after a failure or timeout


@dataclass
class TerminalDecision:
    """What a terminal tool (``submit_verdict``) decided: accepted ends the loop."""

    accepted: bool
    message: str
    data: Any = None


TerminalHandler = Callable[[dict], Awaitable[TerminalDecision]]


@dataclass
class LoopOutcome:
    status: Literal["verdict", "step_cap", "llm_error", "stopped"]
    steps: int
    tool_calls: int
    messages: list[dict]
    decision: TerminalDecision | None = None
    error: str | None = None


async def call_emit(emit: Emit | None, type_: EventType, data: dict, agent: Agent) -> None:
    if emit is None:
        return
    out = emit(type_, data, agent=agent)
    if inspect.isawaitable(out):
        await out


class EventLog:
    """An ``emit`` callback that builds Event envelopes in memory (tests, recordings).

    ``forward`` (optional) receives each Event, e.g. the SSE broadcaster."""

    def __init__(self, run_id: str, forward: Callable[[Event], Any] | None = None):
        self.run_id = run_id
        self.events: list[Event] = []
        self.forward = forward

    async def __call__(self, type_: EventType, data: dict, *, agent: Agent = Agent.TRIAGE):
        ev = Event(
            id=len(self.events),
            run_id=self.run_id,
            ts=int(time.time() * 1000),  # epoch milliseconds, like the run layer
            type=type_,
            agent=agent,
            data=json.loads(json.dumps(data, default=str)),
        )
        self.events.append(ev)
        if self.forward is not None:
            out = self.forward(ev)
            if inspect.isawaitable(out):
                await out

    def of(self, type_: EventType) -> list[dict]:
        return [e.data for e in self.events if e.type == type_]

    @property
    def types(self) -> list[str]:
        return [str(e.type) for e in self.events]


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in MODEL_DROP_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def compact_result(r: ToolResult) -> str:
    """The tool message the model reads: the result (bounded) and how many queries ran."""
    return json.dumps(
        {
            "tool": r.tool,
            "result": payloads.compact(_strip(r.result)),
            "cypher_count": len(r.cypher),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def error_message(tool: str, message: str) -> str:
    return json.dumps({"tool": tool, "error": message[:800]}, sort_keys=True)


class ToolRunner:
    """Runs registry tools for one run and emits their events. The LLM loop and the
    deterministic fallback share it, so both leave identical evidence behind.

    ``pinned`` arguments (e.g. the run's ``incident_id``) must match if the model supplies
    them: a run investigates its own incident only."""

    def __init__(
        self,
        ctx: ToolContext,
        emit: Emit | None,
        *,
        agent: Agent = Agent.TRIAGE,
        pinned: dict[str, Any] | None = None,
        on_result: Callable[[str, dict, ToolResult], Awaitable[None]] | None = None,
    ):
        # Events go through ``emit`` only; a ctx-level emitter would double them.
        self.ctx = dataclasses.replace(ctx, emit=None)
        self.emit = emit
        self.agent = agent
        self.pinned = dict(pinned or {})
        self.on_result = on_result
        self.calls = 0
        self.proofs: list[dict] = []  # test_hypothesis results, in chain order
        self.results: list[ToolResult] = []

    async def run(self, name: str, args: dict) -> ToolResult:
        for k, v in self.pinned.items():
            if k in args and args[k] is not None and args[k] != v:
                raise registry.ToolArgumentError(f"{name}: this run investigates {k}={v} only")
        self.calls += 1
        r = await registry.call_tool(name, args, self.ctx)
        self.results.append(r)
        await call_emit(self.emit, EventType.TOOL_CALL, payloads.tool_call(r), self.agent)
        if name == "test_hypothesis":
            p = r.result
            self.proofs.append(p)
            await call_emit(self.emit, EventType.HYPOTHESIS, payloads.hypothesis(p), self.agent)
            data = payloads.proof(p, len(self.proofs))
            await call_emit(self.emit, EventType.PROOF, data, self.agent)
        if self.on_result is not None:
            await self.on_result(name, args, r)
        return r


async def _complete(llm: LLMClient, messages: list[dict], tools: list[dict], limits: LoopLimits):
    """One step's LLM call with a timeout and ``limits.retries`` retries.
    Returns (reply, None) or (None, last error text)."""
    err = "no attempt"
    for _ in range(1 + max(0, limits.retries)):
        try:
            reply = await asyncio.wait_for(
                llm.complete(list(messages), tools), timeout=limits.step_timeout_s
            )
            return reply, None
        except TimeoutError:
            err = f"LLM step timed out after {limits.step_timeout_s:g} s"
        except Exception as e:  # provider errors, transcript misses, exhausted scripts
            err = f"{type(e).__name__}: {e}"
    return None, err


async def run_loop(
    llm: LLMClient,
    messages: list[dict],
    tools: list[dict],
    runner: ToolRunner,
    *,
    allowed: Iterable[str],
    terminal: dict[str, TerminalHandler] | None = None,
    limits: LoopLimits | None = None,
    on_usage: UsageHook | None = None,
    should_stop: Callable[[], bool] | None = None,
    nudge: str = NUDGE,
) -> LoopOutcome:
    """Drive ``llm`` until a terminal tool accepts, the step cap, an LLM failure, or
    ``should_stop()`` (e.g. the spend breaker). ``messages`` is extended in place;
    ``nudge`` is the user turn sent back when the model replies without a tool call."""
    limits = limits or LoopLimits()
    terminal = terminal or {}
    allowed = set(allowed)
    steps = 0

    def done(status, decision=None, error=None) -> LoopOutcome:
        return LoopOutcome(status, steps, runner.calls, messages, decision, error)

    while True:
        if steps >= limits.max_steps:
            return done("step_cap", error=f"step cap of {limits.max_steps} reached")
        if should_stop is not None and should_stop():
            return done("stopped", error="spend cap reached")
        steps += 1
        reply, err = await _complete(llm, messages, tools, limits)
        if reply is None:
            return done("llm_error", error=err)
        if on_usage is not None:
            out = on_usage(reply.model or llm.model, *_usage(reply))
            if inspect.isawaitable(out):
                await out
        messages.append(reply.assistant_message())
        if reply.content and reply.content.strip():
            text = reply.content.strip()[:600]
            await call_emit(
                runner.emit, EventType.NARRATION, {"topic": "general", "text": text}, runner.agent
            )
        if not reply.tool_calls:
            messages.append({"role": "user", "content": nudge})
            continue
        for tc in reply.tool_calls:
            content, decision = await _dispatch(tc, runner, allowed, terminal)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
            if decision is not None and decision.accepted:
                return done("verdict", decision)


def _usage(reply: LLMReply) -> tuple[int, int]:
    return reply.usage.prompt_tokens, reply.usage.completion_tokens


async def _dispatch(
    tc: ToolCall, runner: ToolRunner, allowed: set[str], terminal: dict[str, TerminalHandler]
) -> tuple[str, TerminalDecision | None]:
    try:
        args = tc.arguments()
    except ValueError as e:
        return error_message(tc.name, f"arguments are not a JSON object: {e}"), None
    if tc.name in terminal:
        decision = await terminal[tc.name](args)
        if decision.accepted:
            return json.dumps({"tool": tc.name, "accepted": True, "message": decision.message}), (
                decision
            )
        return error_message(tc.name, decision.message), decision
    if tc.name not in allowed:
        msg = f"unknown tool {tc.name!r}; use one of the listed tools"
        return error_message(tc.name, msg), None
    try:
        r = await runner.run(tc.name, args)
    except Exception as e:  # ToolError, validation, scope, query errors: all go to the model
        return error_message(tc.name, str(e)), None
    return compact_result(r), None


def schemas_for(names: Iterable[str]) -> list[dict]:
    """Registry schemas filtered to an allowlist, in registry order."""
    wanted = set(names)
    return [s for s in registry.openai_tool_schemas() if s["function"]["name"] in wanted]
