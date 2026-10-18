"""POST /api/ask (Ask the graph, R1-R6, KTD5): session-scoped, JSON only, read-only,
rate limited per session, limited mode without a model or past the spend cap. Real
FalkorDB on a session copy of the seed world; ScriptedLLM only."""

from __future__ import annotations

import json

import pytest
from api_helpers import cleanup_store_graphs, http, inject, sid_of, stream, test_settings

from ic.agent import ask as A
from ic.agent.llm import ScriptedLLM, call, reply
from ic.api import routes
from ic.api.app import create_app
from ic.graph import client, naming
from ic.graph.client import graph_exists, query
from ic.store import Store

CHECKOUT_API = "svc-checkout-api@us-east"


@pytest.fixture(scope="module")
async def aapp(tmp_path_factory):
    client.reset_client()
    tmp = tmp_path_factory.mktemp("ask")
    store = Store(tmp / "ic.sqlite3")
    settings = test_settings(tmp, ip_concurrent_sessions=20, ip_sessions_per_hour=100)
    app = create_app(settings, llm=None, store=store)
    yield app
    await cleanup_store_graphs(store)
    client.reset_client()


@pytest.fixture(scope="module")
def astate(aapp) -> routes.AppState:
    return routes.app_state(aapp)


async def counts(graph: str) -> tuple[int, int]:
    n = await query(graph, "MATCH (n) RETURN count(n)", None, read_only=True)
    e = await query(graph, "MATCH ()-[e]->() RETURN count(e)", None, read_only=True)
    return n.result_set[0][0], e.result_set[0][0]


async def test_ask_without_a_model_answers_in_limited_mode_and_writes_nothing(aapp):
    async with http(aapp, "10.80.0.1") as c:
        r = await c.post("/api/ask", json={"question": f"Who owns {CHECKOUT_API}?"})
        assert r.status_code == 200, r.text
        body = r.json()
        sid = sid_of(c)  # asking starts a session like inject does
        world = naming.session_world(sid)
        assert await graph_exists(world)
        assert body["mode"] == "limited" and body["note"] and body["grounded"] and body["found"]
        assert set(body) >= {"answer", "mode", "grounded", "citations", "calls", "ms", "tokens"}
        kinds = {x["id"]: x["kind"] for x in body["citations"]}
        assert kinds[CHECKOUT_API] == "Service" and "Team" in kinds.values()
        assert all(x["label"] for x in body["citations"])
        assert [x["tool"] for x in body["calls"]] == ["find_entities", "service_profile"]
        assert all(x["cypher"] and x["params"] for x in body["calls"])

        before = await counts(world)
        for q in (
            "Who is on call for checkout?",
            "Recent changes to checkout",
            "Which changes caused incidents last week?",
            "What did we decide after the last config-service outage?",
        ):
            r = await c.post("/api/ask", json={"question": q})
            assert r.status_code == 200 and r.json()["grounded"], (q, r.text)
        assert await counts(world) == before
        assert not await graph_exists(naming.ledger(sid))  # nothing written anywhere


async def test_ask_with_a_model_is_grounded_on_the_session_world(aapp, astate, monkeypatch):
    def answer(messages, tools):
        owner = json.loads(messages[-1]["content"])["result"]["owner"]["team_id"]
        return reply(
            call(
                "submit_answer",
                answer=f"Owned by {owner}.",
                citations=[CHECKOUT_API, owner, "per-9999"],
                found=True,
            ),
            tokens=(300, 20),
        )

    llm = ScriptedLLM([reply(call("service_profile", service_id=CHECKOUT_API)), answer])
    monkeypatch.setattr(astate, "llm", llm)
    spent = astate.breaker.spend()
    async with http(aapp, "10.80.0.2") as c:
        r = await c.post(
            "/api/ask", json={"question": "Who owns checkout-api?", "incident_id": "inc-001"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "llm" and body["grounded"] and body["note"] is None
    assert [x["id"] for x in body["citations"]][0] == CHECKOUT_API
    assert len(body["citations"]) == 2 and body["dropped"] == ["per-9999"]
    assert body["tokens"] == 320
    assert astate.breaker.spend() > spent  # usage counts toward the daily breaker
    # no snapshots for that incident in this session: only the world tools are offered
    offered = {t["function"]["name"] for t in llm.calls[0][1]}
    assert offered == set(A.ASK_TOOLS) | {"submit_answer"}


async def test_spend_cap_answers_in_limited_mode(aapp, astate, monkeypatch):
    llm = ScriptedLLM([])
    monkeypatch.setattr(astate, "llm", llm)
    monkeypatch.setattr(astate.breaker, "tripped", lambda *a: True)
    async with http(aapp, "10.80.0.3") as c:
        r = await c.post("/api/ask", json={"question": "Who owns checkout-api?"})
    assert r.status_code == 200
    assert r.json()["mode"] == "limited" and "spend cap" in r.json()["note"]
    assert llm.calls == []


async def test_ae4_twenty_first_question_in_an_hour_is_429(aapp, astate):
    limit = astate.settings.session_asks_per_hour
    assert limit == 20
    async with http(aapp, "10.80.0.4") as c:
        for _ in range(limit):
            r = await c.post("/api/ask", json={"question": "Tell me a joke"})
            assert r.status_code == 200, r.text
            assert r.json()["found"] is False and "Try" in r.json()["answer"]
        r = await c.post("/api/ask", json={"question": "Tell me a joke"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert "per hour" in r.json()["error"]


async def test_bad_requests(aapp):
    async with http(aapp, "10.80.0.5") as c:
        r = await c.post("/api/ask", json={"question": "x" * (A.QUESTION_MAX + 1)})
        assert r.status_code == 422
        r = await c.post("/api/ask", json={"question": ""})
        assert r.status_code == 422
        r = await c.post("/api/ask", json={"question": "   "})
        assert r.status_code == 422
        r = await c.post("/api/ask", json={"question": "hi", "graph": "seed:world"})
        assert r.status_code == 422  # extra=forbid
        r = await c.post("/api/ask", json={"question": "hi", "incident_id": "s:x:*"})
        assert r.status_code == 422
        r = await c.post("/api/ask", content="question=hi", headers={"Content-Type": "text/plain"})
        assert r.status_code == 415


# --- the visitor's own incident (incident_proofs over the session's ledger) -------------


@pytest.fixture(scope="module")
async def solved(aapp, astate):
    """One finished deterministic investigation in its own session."""
    c = http(aapp, "10.80.1.1")
    r = await inject(c, "bad_deploy", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    _, events = await stream(c, body["run_id"])
    assert events[-1]["type"] == "done"
    answer = astate.store.get_answer(body["incident_id"], sid_of(c))
    yield c, body["incident_id"], answer["root_id"]
    await c.aclose()


async def ask_about(c, iid: str, question: str) -> dict:
    r = await c.post("/api/ask", json={"question": question, "incident_id": iid})
    assert r.status_code == 200, r.text
    return r.json()


async def test_limited_mode_explains_the_blamed_change_from_the_proofs(solved):
    c, iid, root = solved
    body = await ask_about(c, iid, f"Why did you blame {root}?")
    assert body["mode"] == "limited" and body["found"] and body["grounded"]
    assert [x["tool"] for x in body["calls"]] == ["incident_proofs"]
    assert body["answer"].startswith(f"{root} was blamed for {iid}")
    assert "Ruled out:" in body["answer"] and "KTD" not in body["answer"]
    assert root in {x["id"] for x in body["citations"]}
    assert all(x["cypher"] and x["params"] for x in body["calls"])


async def test_limited_mode_names_the_failing_clause_of_a_ruled_out_change(solved):
    c, iid, root = solved
    body = await ask_about(c, iid, "What got ruled out?")
    assert [x["tool"] for x in body["calls"]] == ["incident_proofs"]
    ruled = [x["id"] for x in body["citations"] if x["id"] not in (root, iid)]
    assert ruled, body["answer"]
    body = await ask_about(c, iid, f"Why was {ruled[0]} ruled out?")
    assert body["answer"].startswith(f"{ruled[0]} was ruled out for {iid}: it fails the ")


async def test_incident_proofs_is_offered_only_for_the_sessions_own_incident(
    aapp, astate, solved, monkeypatch
):
    c, iid, root = solved

    def answer(messages, tools):
        res = json.loads(messages[-1]["content"])["result"]
        return reply(call("submit_answer", answer="Blamed.", citations=[res["blamed"]], found=True))

    llm = ScriptedLLM([reply(call("incident_proofs")), answer])
    monkeypatch.setattr(astate, "llm", llm)
    body = await ask_about(c, iid, "Why this change?")
    assert "incident_proofs" in {t["function"]["name"] for t in llm.calls[0][1]}
    assert [x["id"] for x in body["citations"]] == [root] and body["grounded"]

    # another session asking about the same incident id sees nothing of this ledger
    llm = ScriptedLLM([reply(call("submit_answer", answer="No.", citations=[], found=False))])
    monkeypatch.setattr(astate, "llm", llm)
    async with http(aapp, "10.80.1.2") as other:
        await ask_about(other, iid, "Why this change?")
    assert "incident_proofs" not in {t["function"]["name"] for t in llm.calls[0][1]}
