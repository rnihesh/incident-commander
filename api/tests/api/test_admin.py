"""Admin usage panel API: off without a token, bearer auth, failed-attempt limit, and
responses that carry no client addresses, session ids or keys."""

from __future__ import annotations

import pytest
from api_helpers import cleanup_store_graphs, http, inject, sid_of, test_settings

from ic.api import routes
from ic.api.app import create_app
from ic.api.routes_admin import FAIL_LIMIT
from ic.graph import client
from ic.store import Store

pytestmark = pytest.mark.asyncio

TOKEN = "admin-test-token-0123456789abcdef"
API_KEY = "sk-test-not-a-real-key-4242"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def admin_app(tmp_path):
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    app = create_app(
        test_settings(tmp_path, admin_token=TOKEN, llm_api_key=API_KEY), llm=None, store=store
    )
    state = routes.app_state(app)
    yield app
    await state.runs.wait_all(limit_s=60)
    await cleanup_store_graphs(store)
    client.reset_client()


async def test_admin_routes_are_404_without_a_configured_token(tmp_path):
    app = create_app(test_settings(tmp_path), llm=None, store=Store(tmp_path / "ic.sqlite3"))
    async with http(app) as c:
        for path in ("/api/admin/summary", "/api/admin/runs"):
            for headers in ({}, {"Authorization": "Bearer "}, BEARER):
                r = await c.get(path, headers=headers)
                assert r.status_code == 404, (path, headers)
                assert "set-cookie" not in r.headers


async def test_wrong_or_missing_token_is_401_and_sets_no_cookie(admin_app):
    async with http(admin_app, "10.61.0.1") as c:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": TOKEN},  # no scheme
            {"Authorization": f"Basic {TOKEN}"},
            {"Authorization": f"Bearer {TOKEN}x"},
        ):
            r = await c.get("/api/admin/summary", headers=headers)
            assert r.status_code == 401, headers
            assert "set-cookie" not in r.headers
        assert not c.cookies


async def test_right_token_returns_summary_and_runs(admin_app):
    async with http(admin_app, "10.61.0.2") as c:
        r = await c.get("/api/admin/summary", headers=BEARER)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert "set-cookie" not in r.headers
        body = r.json()
        for key in (
            "spend",
            "runs",
            "daily",
            "sessions",
            "clients",
            "activity_today",
            "refusals_today",
            "falkordb",
            "process",
            "config",
            "breaker",
        ):
            assert key in body, key
        assert set(body["spend"]) == {"today", "last_7_days", "all_time"}
        assert len(body["daily"]) == 7
        assert body["sessions"]["max"] == 50
        assert body["falkordb"]["reachable"] is True
        assert body["falkordb"]["graphs"]["total"] >= 1
        assert body["config"]["llm"] == {"provider": "openai", "model": None, "configured": False}
        r = await c.get("/api/admin/runs?limit=5", headers=BEARER)
        assert r.status_code == 200 and r.json() == {"runs": [], "limit": 5}
        assert (await c.get("/api/admin/runs?limit=0", headers=BEARER)).status_code == 422
        assert not c.cookies


async def test_failed_attempts_are_rate_limited_per_client(admin_app):
    async with http(admin_app, "10.61.0.3") as c:
        for _ in range(FAIL_LIMIT):
            r = await c.get("/api/admin/summary", headers={"Authorization": "Bearer guess"})
            assert r.status_code == 401
        r = await c.get("/api/admin/summary", headers=BEARER)  # locked out, even when right
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) > 0
    async with http(admin_app, "10.61.0.4") as other:  # another address is unaffected
        assert (await other.get("/api/admin/summary", headers=BEARER)).status_code == 200
        body = (await other.get("/api/admin/summary", headers=BEARER)).json()
        assert body["admin_auth_failures_today"] == FAIL_LIMIT


async def test_usage_is_counted_without_raw_ips_sids_or_keys(admin_app):
    ip = "10.61.7.77"
    async with http(admin_app, ip) as visitor:
        r = await inject(visitor)
        assert r.status_code == 200, r.text
        sid = sid_of(visitor)
        run_id = r.json()["run_id"]
    await routes.app_state(admin_app).runs.wait_all(limit_s=120)

    async with http(admin_app, "10.61.0.5") as c:
        summary = await c.get("/api/admin/summary", headers=BEARER)
        runs = await c.get("/api/admin/runs", headers=BEARER)
    assert summary.status_code == 200 and runs.status_code == 200
    for text in (summary.text, runs.text):
        assert ip not in text
        assert sid not in text
        assert API_KEY not in text
        assert TOKEN not in text

    body = summary.json()
    assert body["sessions"]["created_today"] == 1
    assert body["clients"]["unique_today"] == 1
    assert len(body["clients"]["top_today"][0]["client"]) == 8
    assert body["activity_today"]["runs_started"] == 1
    assert body["runs"]["today"]["total"] == 1
    assert body["runs"]["today"]["by_fault_class"] == {"bad_deploy": 1}
    assert body["runs"]["today"]["degraded"] == 1  # no LLM: deterministic fallback
    assert body["daily"][-1]["runs"] == 1

    (row,) = runs.json()["runs"]
    assert row["run_id"] == run_id
    assert row["fault_class"] == "bad_deploy"
    assert row["kind"] == "investigation"
    assert row["verdict"] == "DIAGNOSED"
    assert row["degraded"] is True
    assert row["tool_calls"] > 0
    assert row["tokens"] == 0 and row["usd"] == 0
    assert row["duration_ms"] > 0
