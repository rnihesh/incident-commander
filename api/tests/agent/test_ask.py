"""Ask the graph (KTD2-KTD4): the answer loop over the company tools with a server-side
grounding guard, and the deterministic router used without a model. ScriptedLLM only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ic.agent import ask as A
from ic.agent.llm import ScriptedLLM, call, reply
from ic.graph import client, naming
from ic.tools.registry import ToolContext

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from company_world import CHECKOUT_API, counts, write_company  # noqa: E402


async def drop(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


@pytest.fixture(scope="module")
async def ctx():
    client.reset_client()
    sid = naming.new_session_id()
    g = naming.session_world(sid)
    await write_company(g)
    yield ToolContext(sid=sid, world_graph=g)
    await drop(sid)
    client.reset_client()


def submit(answer: str, citations: list[str], found: bool = True):
    return reply(call("submit_answer", answer=answer, citations=citations, found=found))


def ids(res: A.AskResult) -> list[str]:
    return [c["id"] for c in res.citations]


# --- the LLM loop ----------------------------------------------------------------------


async def test_ae1_grounded_answer_with_sources_and_queries(ctx):
    llm = ScriptedLLM(
        [
            reply(call("find_entities", search="checkout-api"), tokens=(100, 10)),
            reply(call("service_profile", service_id=CHECKOUT_API), tokens=(200, 10)),
            submit(
                "Payments Core (team-pay) owns it; Ada Park (per-0001) is on call.",
                [CHECKOUT_API, "team-pay", "per-0001"],
            ),
        ]
    )
    res = await A.ask("Who owns svc-checkout-api@us-east and who is on call now?", ctx, llm)
    assert res.mode == "llm" and res.grounded and res.found
    assert res.citations == [
        {"id": CHECKOUT_API, "kind": "Service", "label": "checkout-api"},
        {"id": "team-pay", "kind": "Team", "label": "Payments Core"},
        {"id": "per-0001", "kind": "Person", "label": "Ada Park"},
    ]
    assert [c["tool"] for c in res.calls] == ["find_entities", "service_profile"]
    for c in res.calls:
        assert c["cypher"] and len(c["params"]) == len(c["cypher"])
    assert res.calls[1]["args"] == {"service_id": CHECKOUT_API}
    assert res.tokens == 320 and res.dropped == [] and res.ms > 0
    body = res.to_dict()
    assert set(body) >= {"answer", "mode", "grounded", "citations", "calls", "ms", "tokens"}
    # the model saw the tools it may use, plus submit_answer, and the quoted question
    _, tools = llm.calls[0]
    names = {t["function"]["name"] for t in tools}
    assert names == set(A.ASK_TOOLS) | {"submit_answer"}
    assert "test_hypothesis" not in names and "fragility_map" not in names
    assert "<question>" in llm.calls[0][0][1]["content"]


async def test_ae2_no_information_has_no_sources(ctx):
    llm = ScriptedLLM(
        [
            reply(call("find_entities", search="CEO favorite color")),
            submit("The graph has no information about that.", [], found=False),
        ]
    )
    res = await A.ask("What is the CEO's favorite color?", ctx, llm)
    assert res.mode == "llm"
    assert not res.grounded and not res.found and res.citations == []
    assert "no information" in res.answer


async def test_citation_not_touched_by_any_call_is_dropped(ctx):
    llm = ScriptedLLM(
        [
            reply(call("service_profile", service_id=CHECKOUT_API)),
            submit("Owned by team-pay; per-0003 also helps.", ["team-pay", "per-0003", "nope"]),
        ]
    )
    res = await A.ask("Who owns checkout-api?", ctx, llm)
    assert ids(res) == ["team-pay"] and res.grounded
    assert res.dropped == ["per-0003", "nope"]  # real node, but no call touched it


async def test_facts_without_surviving_citations_are_ungrounded(ctx):
    llm = ScriptedLLM([submit("Ada Park owns everything.", ["per-0001"])])  # no tool call
    res = await A.ask("Who owns everything?", ctx, llm)
    assert res.found and not res.grounded and res.citations == []
    assert res.dropped == ["per-0001"]


async def test_text_reply_is_nudged_toward_submit_answer(ctx):
    llm = ScriptedLLM(
        [
            reply(content="Let me think."),
            reply(call("team_profile", team_id="team-search")),
            submit("Cy Moss (per-0003) is on call for Search Platform.", ["per-0003"]),
        ]
    )
    res = await A.ask("Who is on call for search?", ctx, llm)
    assert res.grounded and ids(res) == ["per-0003"]
    nudge = llm.calls[1][0][-1]
    assert nudge["role"] == "user" and "submit_answer" in nudge["content"]
    assert "submit_verdict" not in nudge["content"]


async def test_disallowed_tool_is_refused_to_the_model(ctx):
    llm = ScriptedLLM(
        [
            reply(call("test_hypothesis", kind="suspect_qualifies", entity_id="x", claim="y")),
            submit("No information.", [], found=False),
        ]
    )
    res = await A.ask("Prove something", ctx, llm)
    assert res.calls == []
    tool_msg = llm.calls[1][0][-1]
    assert tool_msg["role"] == "tool" and "unknown tool" in tool_msg["content"]


async def test_llm_failure_falls_back_to_the_router(ctx):
    llm = ScriptedLLM([RuntimeError("provider down"), RuntimeError("provider down")])
    res = await A.ask("Who owns checkout-api?", ctx, llm)
    assert res.mode == "limited" and res.note and "limited" in res.note
    assert res.grounded and "team-pay" in ids(res)


async def test_step_cap_is_small(ctx):
    looping = [reply(call("find_entities", search="checkout")) for _ in range(20)]
    llm = ScriptedLLM(looping)
    res = await A.ask("Who owns checkout?", ctx, llm)
    assert len(llm.calls) == A.ASK_LIMITS.max_steps == 8
    assert res.mode == "limited"  # step cap -> router


async def test_token_budget_stops_the_loop(ctx):
    llm = ScriptedLLM(
        [reply(call("find_entities", search="checkout"), tokens=(A.MAX_TOKENS, 1))] * 5
    )
    res = await A.ask("Who owns checkout?", ctx, llm)
    assert len(llm.calls) == 1 and res.mode == "limited"
    assert res.tokens == A.MAX_TOKENS + 1


async def test_should_stop_skips_the_model(ctx):
    llm = ScriptedLLM([])
    res = await A.ask("Who owns checkout-api?", ctx, llm, should_stop=lambda: True)
    assert res.mode == "limited" and llm.calls == []


async def test_asking_writes_nothing(ctx):
    before = await counts(ctx.world_graph)
    llm = ScriptedLLM(
        [
            reply(call("incident_history", days=30)),
            reply(call("decisions_for", entity_id="inc-002")),
            submit("rec-003", ["rec-003"]),
        ]
    )
    await A.ask("What did we decide after the last search outage?", ctx, llm)
    for q in ROUTER_QUESTIONS:
        await A.ask(q, ctx, None)
    assert await counts(ctx.world_graph) == before


# --- the deterministic router (no model) ------------------------------------------------

ROUTER_QUESTIONS = [
    "Who owns checkout-api?",
    "Who is on call for search-api?",
    "Recent changes to checkout-api",
    "Incidents on search-api",
    "What did we decide after the last search outage?",
]


async def test_router_owner(ctx):
    res = await A.ask("Who owns checkout-api?", ctx, None)
    assert res.mode == "limited" and res.note and res.grounded
    assert "Payments Core" in res.answer and "team-pay" in ids(res)
    assert CHECKOUT_API in ids(res)


async def test_router_owner_and_on_call(ctx):
    res = await A.ask("Who owns svc-checkout-api@us-east and who is on call now?", ctx, None)
    assert "Payments Core" in res.answer and "Ada Park" in res.answer
    assert {"team-pay", "per-0001", CHECKOUT_API} <= set(ids(res))


async def test_router_on_call(ctx):
    res = await A.ask("Who is on call for search-api?", ctx, None)
    assert "Cy Moss" in res.answer and "per-0003" in ids(res)


async def test_router_recent_changes_with_authors(ctx):
    res = await A.ask("recent changes to checkout-api", ctx, None)
    assert "chg-001" in res.answer and "Ada Park" in res.answer and "Ben Ito" in res.answer
    assert {"chg-001", "chg-002", "per-0001"} <= set(ids(res))
    assert [c["tool"] for c in res.calls] == ["find_entities", "service_profile"]


async def test_router_changes_by_person(ctx):
    res = await A.ask("Which changes did Cy Moss make?", ctx, None)
    assert "chg-003" in res.answer and "per-0003" in ids(res)


async def test_router_incidents_on_a_service_and_last_week(ctx):
    res = await A.ask("Incidents on search-api", ctx, None)
    assert "inc-002" in res.answer and "chg-004" in res.answer and "inc-002" in ids(res)
    res = await A.ask("Which changes caused incidents last week?", ctx, None)
    assert "inc-002" in res.answer and "inc-001" not in res.answer
    assert {"inc-002", "chg-004", "per-0003"} <= set(ids(res))
    assert res.calls[-1]["args"] == {"days": 7}


async def test_router_decisions_after_last_outage(ctx):
    res = await A.ask("What did we decide after the last search outage?", ctx, None)
    assert "rec-003" in res.answer and "circuit breaker" in res.answer
    assert {"inc-002", "rec-003"} <= set(ids(res))
    res = await A.ask("What decisions exist for checkout-api?", ctx, None)
    assert "adr-001" in ids(res) and "rec-001" in ids(res)


async def test_router_unknown_shape_suggests_questions(ctx):
    res = await A.ask("Tell me a joke", ctx, None)
    assert res.mode == "limited" and not res.grounded and not res.found
    assert "Try" in res.answer and res.citations == []


async def test_router_unknown_entity_says_no_information(ctx):
    res = await A.ask("Who owns the zebra service?", ctx, None)
    assert not res.found and not res.grounded and "no information" in res.answer


async def test_router_answers_are_concise(ctx):
    for q in ROUTER_QUESTIONS:
        res = await A.ask(q, ctx, None)
        assert res.grounded, q
        assert len(res.answer) <= A.ANSWER_MAX, q


async def test_incident_context_adds_snapshot_tools(ctx):
    llm = ScriptedLLM([submit("No information.", [], found=False)])
    await A.ask("What happened?", ctx, llm, incident_id="inc-002", incident_tools=True)
    names = {t["function"]["name"] for t in llm.calls[0][1]}
    assert set(A.INCIDENT_TOOLS) <= names
    assert "inc-002" in llm.calls[0][0][1]["content"]
