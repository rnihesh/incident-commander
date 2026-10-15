"""The spend breaker end to end: it refuses new runs over HTTP, and trips a run in flight."""

from __future__ import annotations

import pytest
from api_helpers import cleanup_store_graphs, http, inject, test_settings

from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.agent.llm import ScriptedLLM, call, reply
from ic.api import routes
from ic.api.app import create_app
from ic.api.events import EventType
from ic.graph import client, naming
from ic.runs.orchestrator import default_deps, start_run
from ic.runs.registry import RunRegistry
from ic.sessions.breaker import Breaker
from ic.sessions.manager import SessionManager
from ic.store import Store
from ic.world.faults import inject_fault

pytestmark = pytest.mark.asyncio

OVER_CAP_TOKENS = 10_000_000  # at the default 5 USD per million prompt tokens: 50 USD


async def test_inject_over_the_daily_cap_serves_the_recorded_run(tmp_path):
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    app = create_app(test_settings(tmp_path), llm=None, store=store)
    state = routes.app_state(app)
    try:
        assert state.breaker.daily_usd == state.settings.daily_usd_breaker
        state.breaker.record_usage("any-model", OVER_CAP_TOKENS, 0)
        assert state.breaker.tripped()
        async with http(app, "10.9.0.1") as c:
            r = await inject(c)
        assert r.status_code == 503
        body = r.json()
        assert body["fallback"] == "recorded_run"
        assert "spend cap" in body["reason"] and body["recorded_url"] == "/api/recorded/hero"
        with store.conn() as db:  # no run was started
            assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        await state.runs.wait_all(limit_s=60)
        await cleanup_store_graphs(store)
        client.reset_client()


async def test_breaker_tripped_mid_run_degrades_to_spend_cap(tmp_path):
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    s = test_settings(tmp_path)
    sessions = SessionManager(store, max_sessions=10)
    breaker = Breaker(store, daily_usd=5.0, prices={"default": [5.0, 20.0]})
    deps = default_deps(
        store,
        s,
        None,
        runs=RunRegistry(store),
        sessions=sessions,
        breaker=breaker,
        github=GitHubClient(None, None, mock=True),
        pager=DiscordPager(None, mock=True),
    )
    sess = await sessions.create("127.0.0.1")
    world = naming.session_world(sess.sid)
    try:
        f = await inject_fault(
            world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=3, store=store
        )
        # One LLM step that reports usage far over the cap; the loop must stop before step 2.
        llm = ScriptedLLM(
            [reply(call("list_symptoms", incident_id=f.incident_id), tokens=(OVER_CAP_TOKENS, 7))]
        )
        assert not breaker.tripped()
        run_id = await start_run(
            sess,
            f.incident_id,
            f.alert_time,
            f.alert_service_id,
            llm=llm,
            store=store,
            settings=s,
            deps=deps,
        )
        run = deps.runs.get(run_id)
        await run.task
        assert run.finished
        verdict = run.of(EventType.VERDICT)[0]
        assert verdict["degraded_reason"] == "spend_cap"
        assert verdict["outcome"] == "DIAGNOSED"  # the deterministic funnel still concludes
        assert len(llm.calls) == 1  # stopped before a second model call
        with store.conn() as db:  # tokens recorded through on_usage
            rows = db.execute("SELECT prompt_tokens, completion_tokens FROM llm_usage").fetchall()
        assert [tuple(r) for r in rows] == [(OVER_CAP_TOKENS, 7)]
        assert breaker.tripped()
    finally:
        await deps.runs.wait_all(limit_s=60)
        await cleanup_store_graphs(store)
        client.reset_client()
