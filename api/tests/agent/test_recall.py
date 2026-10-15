"""Recall and memory-seeded triage (U13, KTD8, F3): memory reorders, proofs still decide."""

from __future__ import annotations

import json

from triage_world import rows

from ic.agent.llm import ScriptedLLM, call, reply
from ic.agent.loop import EventLog
from ic.agent.triage import REQUIRED_KINDS, TRIAGE_TOOLS, run_triage
from ic.api.events import EventType
from ic.graph import client
from ic.tools.registry import call_tool


async def symptoms(ctx, iid: str) -> list[tuple[str, int]]:
    return [
        (r[0], r[1])
        for r in await rows(
            ctx.snapshot_known,
            "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom) RETURN y.service_id, y.onset "
            "ORDER BY y.onset, y.service_id",
            {"iid": iid},
        )
    ]


async def learn(ctx, caller: str, callee: str, confidence: float = 0.6, at: int = 0) -> None:
    """A learned edge as the write-back leaves it, in the snapshot the tools read."""
    await client.query(
        ctx.snapshot_known,
        "MATCH (a:Service {id: $a}), (b:Service {id: $b}) "
        "MERGE (a)-[l:LEARNED_DEPENDS_ON]->(b) "
        "SET l.confidence = $c, l.confirmations = 0, l.contradictions = 0, "
        "l.first_learned_incident = 'inc-000', l.first_learned_at = $at, l.updated_at = $at",
        {"a": caller, "b": callee, "c": confidence, "at": at},
    )


async def caller_of_root(c) -> str:
    syms = await symptoms(c.ctx, c.base.iid)
    return next(s for s, _ in syms if s != c.base.root_service)


async def test_recall_without_memory_offers_no_learned_seed(base, cloner):
    c = await cloner(base)
    r = await call_tool("recall", {"incident_id": base.iid}, c.ctx)
    assert r.result["advisory"] is True and r.result["learned"] == []
    assert base.root_service in r.result["first_to_fail"]
    assert all("learned" not in " ".join(s["reasons"]) for s in r.result["seeds"])
    assert r.graph == c.ctx.snapshot_known and r.cypher


async def test_learned_edge_seeds_the_root_first(base, cloner):
    c = await cloner(base)
    caller = await caller_of_root(c)
    await learn(c.ctx, caller, base.root_service)
    r = await call_tool("recall", {"incident_id": base.iid}, c.ctx)
    [edge] = r.result["learned"]
    assert edge["caller"] == caller and edge["callee"] == base.root_service
    assert edge["explains_hop"] is True and edge["confidence"] == 0.6
    top = r.result["seeds"][0]
    assert top["entity_id"] == base.root and top["plausible"] and r.result["used"]
    assert any("learned" in x for x in top["reasons"])


async def test_weak_or_future_memory_does_not_seed(base, cloner, store):
    c = await cloner(base)
    caller = await caller_of_root(c)
    await learn(c.ctx, caller, base.root_service, confidence=0.1)
    r = await call_tool("recall", {"incident_id": base.iid}, c.ctx)
    assert r.result["learned"] and not any(
        "learned" in " ".join(s["reasons"]) for s in r.result["seeds"]
    )
    await learn(c.ctx, caller, base.root_service, confidence=0.9, at=base.t + 3600)
    r = await call_tool("recall", {"incident_id": base.iid}, c.ctx)
    assert r.result["learned"] == []  # learned after the alert: not visible as of t


async def test_memory_seeded_fallback_needs_fewer_calls_and_every_proof(base, cloner, store):
    plain = await cloner(base)
    before = await run_triage(plain.ctx, base.iid, llm=None, emit=None, store=store)
    assert before.verdict == "DIAGNOSED" and not before.memory_seeded

    c = await cloner(base)
    await learn(c.ctx, await caller_of_root(c), base.root_service)
    log = EventLog("seeded")
    out = await run_triage(c.ctx, base.iid, llm=None, emit=log, store=store)
    assert out.verdict == "DIAGNOSED" and out.suspect_id == base.root
    assert out.recall_used and out.memory_seeded
    assert out.tool_calls < before.tool_calls
    v = log.of(EventType.VERDICT)[0]
    assert v["tool_calls"] == out.tool_calls and v["steps"] == out.steps == 0
    assert v["memory"]["seed_path"] is True and v["memory"]["seeded"] is True
    tools = [d["tool"] for d in log.of(EventType.TOOL_CALL)]
    assert tools[0] == "recall" and "screen_candidates" not in tools
    held = {p["kind"] for p in log.of(EventType.PROOF) if p["state"] == "HOLDS"}
    assert set(REQUIRED_KINDS) <= held  # memory reordered the work; it proved nothing
    assert any(n.get("topic") == "memory" for n in log.of(EventType.NARRATION))
    assert log.of(EventType.CERTIFICATE)  # the absence certificate still ran


async def test_memory_pointing_at_a_non_origin_service_changes_nothing(base, cloner, store):
    c = await cloner(base)
    syms = await symptoms(c.ctx, base.iid)
    late = syms[-1][0]  # the last service to fail cannot pass the origin clause
    other = next(s for s, _ in syms if s != late)
    await learn(c.ctx, other, late, confidence=0.95)
    out = await run_triage(c.ctx, base.iid, llm=None, emit=None, store=store)
    assert out.verdict == "DIAGNOSED" and out.suspect_id == base.root
    assert not out.memory_seeded


async def test_scripted_model_tests_the_seed_first(base, cloner, store):
    c = await cloner(base)
    await learn(c.ctx, await caller_of_root(c), base.root_service)
    memo: dict = {}

    def seed_first(messages, tools):
        res = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])
        assert res["tool"] == "recall"
        memo["s"] = res["result"]["seeds"][0]["entity_id"]
        return reply(
            call("test_hypothesis", kind="suspect_qualifies", entity_id=memo["s"], claim="q")
        )

    def prove(kind):
        return lambda m, t: reply(
            call("test_hypothesis", kind=kind, entity_id=memo["s"], claim=kind)
        )

    def submit(m, t):
        return reply(
            call(
                "submit_verdict",
                verdict="DIAGNOSED",
                suspect_id=memo["s"],
                rival_ids=[],
                summary="seeded by memory, proven",
            )
        )

    llm = ScriptedLLM(
        [
            reply(call("recall", incident_id=base.iid)),
            seed_first,
            prove("suspect_necessary"),
            prove("no_other_qualifying"),
            submit,
        ]
    )
    log = EventLog("llm")
    out = await run_triage(c.ctx, base.iid, llm=llm, emit=log, store=store)
    assert out.verdict == "DIAGNOSED" and out.suspect_id == base.root and not out.degraded
    assert out.steps == 5 and out.memory_seeded
    assert out.tool_calls == 5  # recall + three proofs + the verdict's causal path
    assert "recall" in TRIAGE_TOOLS
    assert "recall" in {t["function"]["name"] for t in llm.calls[0][1]}
