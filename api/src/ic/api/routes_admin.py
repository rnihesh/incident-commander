"""Admin usage panel API: read-only usage, spend and capacity for the demo's owner.

  GET /api/admin/summary        spend, runs, sessions, clients, refusals, FalkorDB, limits
  GET /api/admin/runs?limit=50  recent runs, newest first

Off by default: with ``IC_ADMIN_TOKEN`` empty every route answers 404. Otherwise a request
needs ``Authorization: Bearer <token>`` (constant-time compare). Failed attempts are counted
per client address; past ``FAIL_LIMIT`` in ``FAIL_WINDOW_S`` the address gets 429 until the
window clears, even with the right token. The routes never read or create a visitor
session and set no cookies. Responses carry no client addresses (only salted-hash
prefixes), no session ids and no keys.
"""

from __future__ import annotations

import asyncio
import hmac
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ic import usage
from ic.api import auth
from ic.api.state import AppState, get_state
from ic.graph.client import _call, get_db

FAIL_LIMIT = 10
FAIL_WINDOW_S = 900.0
FAIL_KIND = "admin_auth_fail"
FALKOR_TIMEOUT_S = 3.0
DAYS = 7
TOP_CLIENTS = 5

router = APIRouter(prefix="/api/admin")


def _failures(s: AppState, ip: str, now: float) -> tuple[int, float | None]:
    with s.store.conn() as c:
        n, oldest = c.execute(
            "SELECT count(*), min(ts) FROM limit_events WHERE kind = ? AND key = ? AND ts > ?",
            (FAIL_KIND, ip, now - FAIL_WINDOW_S),
        ).fetchone()
    return n, oldest


async def require_admin(request: Request, response: Response) -> AppState:
    s = get_state(request)
    token = s.settings.admin_token
    if not token:
        raise HTTPException(404, "Not Found")
    ip = auth.client_ip(request, s.settings)
    now = time.time()
    n, oldest = _failures(s, ip, now)
    if n >= FAIL_LIMIT:
        wait = max(1, math.ceil((oldest or now) + FAIL_WINDOW_S - now))
        raise HTTPException(
            429, "too many failed admin attempts; try again later", {"Retry-After": str(wait)}
        )
    scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
    ok = scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), token.encode())
    if not ok:
        with s.store.conn() as c:
            c.execute("INSERT INTO limit_events VALUES (?, ?, ?)", (FAIL_KIND, ip, now))
        usage.bump(s.store, "admin_auth_failures", now=now)
        raise HTTPException(401, "invalid admin token", {"WWW-Authenticate": "Bearer"})
    response.headers["Cache-Control"] = "no-store"
    return s


ADMIN = [Depends(require_admin)]


# --- summary ------------------------------------------------------------------------------


def _days(now: float) -> list[str]:
    today = datetime.fromtimestamp(now, UTC).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]


def _spend(c, where: str, args: tuple) -> dict:
    rows = c.execute(
        "SELECT model, count(*), COALESCE(SUM(prompt_tokens), 0), "
        "COALESCE(SUM(completion_tokens), 0), COALESCE(SUM(usd), 0) FROM llm_usage "
        f"WHERE {where} GROUP BY model ORDER BY 5 DESC",
        args,
    ).fetchall()
    models = [
        {
            "model": m,
            "calls": n,
            "prompt_tokens": p,
            "completion_tokens": comp,
            "usd": round(usd, 6),
        }
        for m, n, p, comp, usd in rows
    ]
    return {
        "usd": round(sum(m["usd"] for m in models), 6),
        "calls": sum(m["calls"] for m in models),
        "prompt_tokens": sum(m["prompt_tokens"] for m in models),
        "completion_tokens": sum(m["completion_tokens"] for m in models),
        "by_model": models,
    }


def _runs(c, since_day: str) -> dict:
    def group(col: str) -> dict:
        return {
            (k if k is not None else "none"): n
            for k, n in c.execute(
                f"SELECT {col}, count(*) FROM run_log WHERE day >= ? GROUP BY {col} "
                "ORDER BY 2 DESC",
                (since_day,),
            )
        }

    total, degraded, prs, errors, usd, tokens, avg_ms = c.execute(
        "SELECT count(*), COALESCE(SUM(degraded), 0), COALESCE(SUM(pr), 0), "
        "COUNT(error), COALESCE(SUM(usd), 0), "
        "COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0), "
        "AVG(duration_ms) FROM run_log WHERE day >= ?",
        (since_day,),
    ).fetchone()
    return {
        "total": total,
        "degraded": degraded,
        "prs": prs,
        "errors": errors,
        "usd": round(usd, 6),
        "tokens": tokens,
        "avg_duration_ms": round(avg_ms, 1) if avg_ms is not None else None,
        "by_verdict": group("verdict"),
        "by_fault_class": group("fault_class"),
        "by_kind": group("kind"),
    }


def _metrics(c, d: str) -> dict[str, int]:
    return dict(c.execute("SELECT metric, n FROM usage_daily WHERE day = ?", (d,)).fetchall())


def _daily(c, days: list[str]) -> list[dict]:
    lo = days[0]
    spend = {
        d: (usd, tok)
        for d, usd, tok in c.execute(
            "SELECT day, SUM(usd), SUM(prompt_tokens + completion_tokens) FROM llm_usage "
            "WHERE day >= ? GROUP BY day",
            (lo,),
        )
    }
    runs = {
        d: (n, deg)
        for d, n, deg in c.execute(
            "SELECT day, count(*), COALESCE(SUM(degraded), 0) FROM run_log "
            "WHERE day >= ? GROUP BY day",
            (lo,),
        )
    }
    metrics: dict[str, dict[str, int]] = {}
    for d, m, n in c.execute("SELECT day, metric, n FROM usage_daily WHERE day >= ?", (lo,)):
        metrics.setdefault(d, {})[m] = n
    clients = dict(
        c.execute(
            "SELECT day, count(*) FROM usage_clients WHERE day >= ? GROUP BY day", (lo,)
        ).fetchall()
    )
    out = []
    for d in days:
        m = metrics.get(d, {})
        usd, tok = spend.get(d, (0.0, 0))
        n, deg = runs.get(d, (0, 0))
        out.append(
            {
                "day": d,
                "usd": round(usd or 0.0, 6),
                "tokens": tok or 0,
                "runs": n,
                "degraded_runs": deg,
                "sessions": m.get("sessions", 0),
                "asks": m.get("asks", 0),
                "clients": clients.get(d, 0),
                "refusals": sum(v for k, v in m.items() if k.startswith("refused:")),
            }
        )
    return out


def _db_summary(s: AppState, now: float) -> dict:
    usage.backfill(s.store)
    days = _days(now)
    today, week = days[-1], days[0]
    with s.store.conn() as c:
        m = _metrics(c, today)
        clients = c.execute(
            "SELECT client, n FROM usage_clients WHERE day = ? ORDER BY n DESC LIMIT ?",
            (today, TOP_CLIENTS),
        ).fetchall()
        unique = c.execute("SELECT count(*) FROM usage_clients WHERE day = ?", (today,))
        unique = unique.fetchone()[0]
        active = c.execute("SELECT count(*) FROM sessions").fetchone()[0]
        recent = c.execute(
            "SELECT count(*) FROM sessions WHERE last_seen > ?", (now - 300,)
        ).fetchone()[0]
        return {
            "spend": {
                "today": _spend(c, "day = ?", (today,)),
                "last_7_days": _spend(c, "day >= ?", (week,)),
                "all_time": _spend(c, "1 = 1", ()),
            },
            "runs": {"today": _runs(c, today), "last_7_days": _runs(c, week)},
            "daily": _daily(c, days),
            "sessions": {
                "active": active,
                "seen_last_5_min": recent,
                "created_today": m.get("sessions", 0),
            },
            "clients": {
                "unique_today": unique,
                # Salted-hash prefixes only: enough to tell heavy clients apart.
                "top_today": [{"client": k[:8], "events": n} for k, n in clients],
            },
            "activity_today": {
                k: m.get(k, 0)
                for k in (
                    "runs_started",
                    "asks",
                    "verifies",
                    "proof_verifies",
                    "fragility",
                    "challenges",
                )
            },
            "refusals_today": {
                "total": sum(v for k, v in m.items() if k.startswith("refused:")),
                "by_kind": {
                    k.removeprefix("refused:"): v
                    for k, v in sorted(m.items())
                    if k.startswith("refused:")
                },
            },
            "admin_auth_failures_today": m.get("admin_auth_failures", 0),
        }


def _graph_kind(name: str) -> str:
    for prefix, kind in (("s:", "session"), ("pr:", "pr"), ("b:", "bench")):
        if name.startswith(prefix):
            return kind
    return "other"


async def _falkordb() -> dict:
    async def read() -> dict:
        db = await asyncio.to_thread(get_db)
        info = await _call(lambda: db.connection.info("memory"), idempotent=True)
        names = await _call(lambda: db.list_graphs(), idempotent=True)
        kinds = {"session": 0, "pr": 0, "bench": 0, "other": 0}
        for n in names:
            kinds[_graph_kind(n.decode() if isinstance(n, bytes) else str(n))] += 1
        used, cap = int(info.get("used_memory", 0)), int(info.get("maxmemory", 0))
        return {
            "reachable": True,
            "used_memory": used,
            "used_memory_human": info.get("used_memory_human"),
            "peak_memory_human": info.get("used_memory_peak_human"),
            "maxmemory": cap,
            "maxmemory_human": info.get("maxmemory_human"),
            "maxmemory_policy": info.get("maxmemory_policy"),
            "used_pct": round(100 * used / cap, 1) if cap else None,
            "graphs": {"total": len(names), **kinds},
        }

    try:
        return await asyncio.wait_for(read(), FALKOR_TIMEOUT_S)
    except Exception as e:
        return {"reachable": False, "error": type(e).__name__}


def _config(s: AppState) -> dict:
    st = s.settings
    model = getattr(s.llm, "model", None) or st.llm_model
    return {
        "llm": {
            "provider": st.llm_provider,
            "model": model,
            "configured": s.llm is not None,  # never the key itself
        },
        "limits": {
            "max_sessions": st.max_sessions,
            "session_idle_ttl_s": st.session_idle_ttl_s,
            "session_runs_per_hour": st.session_runs_per_hour,
            "ip_concurrent_sessions": st.ip_concurrent_sessions,
            "ip_sessions_per_hour": st.ip_sessions_per_hour,
            "ip_runs_per_hour": st.ip_runs_per_hour,
            "global_runs_per_hour": st.global_runs_per_hour,
            "actions_per_hour": st.actions_per_hour,
            "session_asks_per_hour": st.session_asks_per_hour,
            "session_verifies_per_hour": st.session_verifies_per_hour,
            "session_fragility_per_hour": st.session_fragility_per_hour,
            "proof_verifies_per_ip_hour": st.proof_verifies_per_ip_hour,
            "daily_usd_breaker": st.daily_usd_breaker,
        },
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/summary", dependencies=ADMIN)
async def admin_summary(request: Request) -> dict[str, Any]:
    s = get_state(request)
    now = time.time()
    body = await asyncio.to_thread(_db_summary, s, now)
    spent = body["spend"]["today"]["usd"]
    cap = s.settings.daily_usd_breaker
    body["breaker"] = {
        "daily_usd": cap,
        "spent_today": spent,
        "remaining_usd": round(max(0.0, cap - spent), 6),
        "tripped": spent >= cap,
    }
    body["sessions"]["max"] = s.settings.max_sessions
    body["sessions"]["runs_in_flight"] = len(s.runs.active())
    body["falkordb"] = await _falkordb()
    body["process"] = {"started_at": _iso(s.started_at), "uptime_s": int(now - s.started_at)}
    body["config"] = _config(s)
    body["generated_at"] = _iso(now)
    body["day_basis"] = "UTC"
    return body


RUN_COLUMNS = (
    "run_id, ts, kind, incident_id, fault_class, verdict, degraded, degraded_reason, "
    "final_state, tool_calls, prompt_tokens, completion_tokens, usd, duration_ms, pr, "
    "simulated, error"
)


def _recent_runs(s: AppState, limit: int) -> list[dict]:
    usage.backfill(s.store)
    with s.store.conn() as c:
        rows = c.execute(
            f"SELECT {RUN_COLUMNS} FROM run_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["time"] = _iso(d.pop("ts"))
        for k in ("degraded", "pr", "simulated"):
            d[k] = None if d[k] is None else bool(d[k])
        p, comp = d["prompt_tokens"], d["completion_tokens"]
        d["tokens"] = None if p is None and comp is None else (p or 0) + (comp or 0)
        out.append(d)
    return out


@router.get("/runs", dependencies=ADMIN)
async def admin_runs(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    s = get_state(request)
    runs = await asyncio.to_thread(_recent_runs, s, limit)
    return {"runs": runs, "limit": limit}
