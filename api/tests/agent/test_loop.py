"""The thin loop (KTD5) against a fake registry: no graph, no network."""

from __future__ import annotations

import asyncio
import json

import pytest

from ic.agent import loop as L
from ic.agent.llm import ScriptedLLM, call, reply
from ic.api.events import EventType
from ic.tools.registry import ToolArgumentError, ToolContext
from ic.tools.result import ToolError, ToolResult

SID = "a" * 32


def _proof_result(seq: int, kind: str = "suspect_qualifies", holds: bool = True) -> dict:
    return {
        "id": f"prf-inc-1-{seq}",
        "hypothesis_id": f"hyp-inc-1-{seq}",
        "sid": SID,
        "incident_id": "inc-1",
        "seq": seq,
        "kind": kind,
        "entity_id": "chg-1",
        "claim": "chg-1 qualifies",
        "state": "HOLDS" if holds else "REFUTED",
        "holds": holds,
        "falsifier_tool": "exonerate",
        "falsifier_args": {"incident_id": "inc-1", "change_id": "chg-1"},
        "expected": {"ok": True},
        "conclusion": {"ok": holds, "clause": None},
        "hash": f"h{seq}",
        "prev_hash": f"h{seq - 1}",
        "registered_at": 1.0,
        "executed_at": 2.0,
        "cypher": ["MATCH (n) RETURN n", "MATCH (c:Change {id: $entity_id}) RETURN c"],
        "params": [{}, {"entity_id": "chg-1"}],
        "falsifier_result": {
            "qualifies": holds,
            "verdict": {"clause": None, "detail": "all clauses hold", "query": 1},
        },
    }


class FakeRegistry:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.seq = 0

    async def __call__(self, name, args, ctx):
        self.calls.append((name, dict(args)))
        assert ctx.emit is None  # events go through the loop's emit only
        if name == "boom":
            raise ToolError("incident inc-9 is not in this snapshot")
        if name == "test_hypothesis":
            self.seq += 1
            result = _proof_result(self.seq)
        else:
            result = {"items": list(range(40)), "ms": 3, "hops": {"a": 1}, "ok": True}
        return ToolResult(
            tool=name,
            result=result,
            cypher=["MATCH (n) RETURN n"],
            params=[{}],
            graph=f"s:{SID}:asof:inc-1:known",
            ms=1.5,
        )


@pytest.fixture
def fake(monkeypatch):
    f = FakeRegistry()
    monkeypatch.setattr(L.registry, "call_tool", f)
    return f


def ctx() -> ToolContext:
    return ToolContext(sid=SID, iid="inc-1", emit=lambda r: None)


async def accept(args):
    return L.TerminalDecision(True, "ok", args)


def runner(log, **kw):
    return L.ToolRunner(ctx(), log, pinned={"incident_id": "inc-1"}, **kw)


ALLOWED = {"list_symptoms", "test_hypothesis", "boom"}


async def test_runs_tools_feeds_results_and_stops_on_terminal(fake):
    log = L.EventLog("run-1")
    usage = []
    llm = ScriptedLLM(
        [
            reply(call("list_symptoms", incident_id="inc-1"), content="Looking.", tokens=(10, 2)),
            reply(call("test_hypothesis", kind="suspect_qualifies", entity_id="chg-1", claim="c")),
            reply(call("submit_verdict", verdict="DIAGNOSED")),
        ]
    )
    msgs = [{"role": "user", "content": "go"}]
    out = await L.run_loop(
        llm,
        msgs,
        [],
        runner(log),
        allowed=ALLOWED,
        terminal={"submit_verdict": accept},
        on_usage=lambda m, p, c: usage.append((m, p, c)),
    )
    assert out.status == "verdict" and out.steps == 3 and out.tool_calls == 2
    assert out.decision.data == {"verdict": "DIAGNOSED"}
    assert [n for n, _ in fake.calls] == ["list_symptoms", "test_hypothesis"]
    assert usage[0] == ("scripted", 10, 2) and len(usage) == 3
    assert log.types == ["narration", "tool_call", "tool_call", "hypothesis", "proof"]

    # the model sees a bounded result without timings, hashes or graph names
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    first = json.loads(tool_msgs[0]["content"])
    assert first["cypher_count"] == 1 and first["tool"] == "list_symptoms"
    assert len(first["result"]["items"]) == 16 and first["result"]["items"][-1] == "... 25 more"
    assert "ms" not in first["result"] and "hops" not in first["result"]
    second = tool_msgs[1]["content"]
    assert SID not in second and '"hash"' not in second and "registered_at" not in second
    assert json.loads(second)["result"]["id"] == "prf-inc-1-1"


async def test_event_payloads_match_web_shapes(fake):
    log = L.EventLog("run-1")
    llm = ScriptedLLM(
        [
            reply(call("test_hypothesis", kind="suspect_qualifies", entity_id="chg-1", claim="c")),
            reply(call("submit_verdict")),
        ]
    )
    await L.run_loop(llm, [], [], runner(log), allowed=ALLOWED, terminal={"submit_verdict": accept})
    tc = log.of(EventType.TOOL_CALL)[0]
    assert set(tc) >= {"tool", "result", "cypher", "params", "nodes_touched", "ms", "graph"}
    h = log.of(EventType.HYPOTHESIS)[0]
    assert h["state"] == "REGISTERED" and h["id"] == "hyp-inc-1-1" and h["suspect"] == "chg-1"
    assert isinstance(h["expected"], str) and h["falsifier_tool"] == "exonerate"
    p = log.of(EventType.PROOF)[0]
    assert p["hypothesis_id"] == h["id"] and p["position"] == 1 and p["state"] == "HOLDS"
    assert p["hash"] == "h1" and p["query"].startswith("MATCH (c:Change")
    assert p["params"] == {"entity_id": "chg-1"} and "qualifies" in p["summary"]
    assert [e.id for e in log.events] == list(range(len(log.events)))


async def test_tool_errors_go_back_to_the_model(fake):
    log = L.EventLog("r")
    llm = ScriptedLLM(
        [
            reply(call("boom", incident_id="inc-1")),
            reply(call("not_a_tool")),
            reply(call("list_symptoms", incident_id="inc-OTHER")),
            reply(L.ToolCall("bad", "list_symptoms", "{not json")),
            reply(call("submit_verdict")),
        ]
    )
    msgs: list[dict] = []
    out = await L.run_loop(
        llm, msgs, [], runner(log), allowed=ALLOWED, terminal={"submit_verdict": accept}
    )
    assert out.status == "verdict" and out.steps == 5
    errors = [json.loads(m["content"]).get("error") for m in msgs if m["role"] == "tool"]
    assert "not in this snapshot" in errors[0]
    assert "unknown tool" in errors[1]
    assert "investigates incident_id=inc-1 only" in errors[2]
    assert "not a JSON object" in errors[3]
    assert [n for n, _ in fake.calls] == ["boom"]  # pinned mismatch never reached the registry


async def test_pinned_mismatch_raises_argument_error(fake):
    with pytest.raises(ToolArgumentError):
        await runner(None).run("list_symptoms", {"incident_id": "inc-2"})


async def test_rejected_terminal_continues_and_text_only_turn_is_nudged(fake):
    seen = []

    async def gate(args):
        seen.append(args)
        if len(seen) == 1:
            return L.TerminalDecision(False, "missing HOLDS proofs of kind suspect_necessary")
        return L.TerminalDecision(True, "ok", "done")

    llm = ScriptedLLM(
        [reply(call("submit_verdict")), reply(content="hmm"), reply(call("submit_verdict"))]
    )
    msgs: list[dict] = []
    out = await L.run_loop(
        llm, msgs, [], runner(None), allowed=ALLOWED, terminal={"submit_verdict": gate}
    )
    assert out.status == "verdict" and out.steps == 3 and out.decision.data == "done"
    assert "suspect_necessary" in json.loads(msgs[1]["content"])["error"]
    assert msgs[3] == {"role": "user", "content": L.NUDGE}


async def test_llm_failure_is_retried_once_then_reported(fake):
    ok = ScriptedLLM([RuntimeError("503"), reply(call("submit_verdict"))])
    out = await L.run_loop(
        ok, [], [], runner(None), allowed=ALLOWED, terminal={"submit_verdict": accept}
    )
    assert out.status == "verdict" and len(ok.calls) == 2

    bad = ScriptedLLM([RuntimeError("503"), RuntimeError("503 again")])
    out = await L.run_loop(bad, [], [], runner(None), allowed=ALLOWED)
    assert out.status == "llm_error" and "503 again" in out.error and out.steps == 1


async def test_step_timeout_retries_then_fails(fake):
    async def slow(messages, tools):
        await asyncio.sleep(1)
        return reply()

    llm = ScriptedLLM([slow, slow])
    limits = L.LoopLimits(step_timeout_s=0.05)
    out = await L.run_loop(llm, [], [], runner(None), allowed=ALLOWED, limits=limits)
    assert out.status == "llm_error" and "timed out" in out.error and len(llm.calls) == 2


async def test_step_cap_and_stop_check(fake):
    llm = ScriptedLLM([reply(call("list_symptoms", incident_id="inc-1"))] * 10)
    out = await L.run_loop(
        llm, [], [], runner(None), allowed=ALLOWED, limits=L.LoopLimits(max_steps=3)
    )
    assert out.status == "step_cap" and out.steps == 3 and out.tool_calls == 3

    out = await L.run_loop(
        ScriptedLLM([reply(call("list_symptoms", incident_id="inc-1"))] * 5),
        [],
        [],
        runner(None),
        allowed=ALLOWED,
        should_stop=lambda: True,
    )
    assert out.status == "stopped" and out.steps == 0


def test_schemas_for_filters_the_registry():
    names = {s["function"]["name"] for s in L.schemas_for(["list_symptoms", "test_hypothesis"])}
    assert names == {"list_symptoms", "test_hypothesis"}
