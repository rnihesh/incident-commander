"""Helpers for the HTTP tests (imported by conftest and the test modules)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from ic.actions import pr_store
from ic.api.auth import COOKIE
from ic.config import Settings
from ic.graph import client
from ic.store import Store

SECRET = "test-alert-secret"


def test_settings(tmp: Path, **kw) -> Settings:
    base = dict(
        store_path=str(tmp / "ic.sqlite3"),
        cookie_secure=False,
        alert_hmac_secret=SECRET,
        max_sessions=50,
        global_runs_per_hour=1000,
        actions_per_hour=1000,
        github_token=None,
        github_repo=None,
        discord_webhook_url=None,
        llm_api_key=None,
        sse_ping_s=15.0,
        session_verify_interval_s=0.0,  # tests re-verify back to back
        session_verifies_per_hour=1000,
        session_fragility_interval_s=0.0,
        session_fragility_per_hour=1000,
    )
    return Settings(**{**base, **kw})


test_settings.__test__ = False  # type: ignore[attr-defined]


def http(app, ip: str = "10.0.0.1") -> httpx.AsyncClient:
    """An in-process client; ``ip`` is the socket peer the app sees."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 51000)),
        base_url="http://testserver",
        timeout=120,
    )


def sid_of(c: httpx.AsyncClient) -> str:
    return c.cookies[COOKIE]


def parse_sse(text: str) -> list[dict]:
    """Event dicts from an SSE body (comments and pings skipped)."""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = block.split("\n")
        data = [ln[5:].lstrip() for ln in lines if ln.startswith("data:")]
        ids = [ln[3:].strip() for ln in lines if ln.startswith("id:")]
        if data:
            ev = json.loads("\n".join(data))
            if ids:
                assert ids[0] == str(ev["id"])  # SSE id mirrors the envelope id
            out.append(ev)
    return out


async def stream(c: httpx.AsyncClient, run_id: str, **headers: str) -> tuple[httpx.Response, list]:
    r = await c.get(f"/api/runs/{run_id}/events", headers=headers)
    return r, (parse_sse(r.text) if r.status_code == 200 else [])


async def inject(c: httpx.AsyncClient, fault_class: str = "bad_deploy", seed: int = 3):
    return await c.post("/api/inject", json={"fault_class": fault_class, "seed": seed})


async def cleanup_store_graphs(store: Store) -> None:
    """Drop every graph the store's sessions (and their PR copies) created."""
    with store.conn() as c:
        sids = {r[0] for r in c.execute("SELECT sid FROM sessions")}
        sids |= {r[0] for r in c.execute("SELECT session_id FROM answer_key")}
    db = client.get_db()
    names = await db.list_graphs()
    prs = set(pr_store.tracked_pr_graphs(store))
    for name in names:
        if name in prs or any(name.startswith(f"s:{sid}:") for sid in sids):
            await db.select_graph(name).delete()


def types(events: list[dict]) -> list[str]:
    out = []
    for e in events:
        t = e["type"]
        if t == "state":
            t = f"state:{e['data']['state']}"
        elif t == "action":
            t = f"action:{e['data']['kind']}"
        out.append(t)
    return out


def is_subsequence(needle: list[str], hay: list[str]) -> bool:
    it = iter(hay)
    return all(any(x == y for y in it) for x in needle)
