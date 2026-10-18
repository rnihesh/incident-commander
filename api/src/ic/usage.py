"""Usage counters for the admin panel (tables in ``ic.store``).

* ``bump``: one per-day metric count (sessions, asks, verifies, refusals, ...) and, when an
  address is given, one per-day activity count for that client. The address is stored only
  as a salted HMAC (salt kept in ``usage_meta``), never in clear.
* ``log_run``: one ``run_log`` row per finished run (verdict, degraded flag, tool calls,
  tokens, cost, duration), written when the run's ``done`` event lands. Run events are
  pruned after a day; these rows are not.

Counting never fails a request: errors are logged and dropped. Days are UTC, as for the
spend breaker.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from ic.api.events import Event, EventType
from ic.store import Store

log = logging.getLogger(__name__)

CLIENT_RETENTION_DAYS = 30
_SID = re.compile(r"[0-9a-f]{32}")
_salts: dict[str, bytes] = {}


def day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).date().isoformat()


def _salt(store: Store, c: sqlite3.Connection) -> bytes:
    key = str(store.path)
    salt = _salts.get(key)
    if salt is None:
        c.execute(
            "INSERT OR IGNORE INTO usage_meta VALUES ('client_salt', ?)", (secrets.token_hex(32),)
        )
        salt = bytes.fromhex(
            c.execute("SELECT v FROM usage_meta WHERE k = 'client_salt'").fetchone()[0]
        )
        _salts[key] = salt
    return salt


def client_key(store: Store, ip: str, c: sqlite3.Connection) -> str:
    """A stable, non-reversible id for a client address (16 hex chars)."""
    return hmac.new(_salt(store, c), ip.encode(), hashlib.sha256).hexdigest()[:16]


def bump(store: Store, metric: str, *, ip: str | None = None, now: float | None = None) -> None:
    """Count one ``metric`` for today (and one activity for the client at ``ip``)."""
    d = day(now)
    try:
        with store.conn() as c:
            c.execute(
                "INSERT INTO usage_daily VALUES (?, ?, 1) "
                "ON CONFLICT (day, metric) DO UPDATE SET n = n + 1",
                (d, metric),
            )
            if ip:
                c.execute(
                    "INSERT INTO usage_clients VALUES (?, ?, 1) "
                    "ON CONFLICT (day, client) DO UPDATE SET n = n + 1",
                    (d, client_key(store, ip, c)),
                )
    except Exception:
        log.warning("usage counter %s not recorded", metric, exc_info=True)


def prune(store: Store, *, now: float | None = None) -> int:
    """Drop per-client rows older than ``CLIENT_RETENTION_DAYS``. Returns how many."""
    cutoff = day((time.time() if now is None else now) - CLIENT_RETENTION_DAYS * 86400)
    with store.conn() as c:
        return c.execute("DELETE FROM usage_clients WHERE day < ?", (cutoff,)).rowcount


# --- run summaries ------------------------------------------------------------------------


def _scrub(text: str) -> str:
    return _SID.sub("…", text)[:200]


def _fault_class(c: sqlite3.Connection, sid: str, iid: str, started: dict) -> str | None:
    if started.get("storm"):
        return "storm"
    if started.get("scenario"):
        return str(started["scenario"])
    row = c.execute(
        "SELECT fault_class FROM answer_key WHERE incident_id = ? AND session_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (iid, sid),
    ).fetchone()
    return row[0] if row else None


def _kind(c: sqlite3.Connection, sid: str, iid: str, started: dict) -> str:
    if started.get("storm"):
        return "storm"
    if started.get("recorded"):
        return "recorded"
    try:
        judged = c.execute(
            "SELECT 1 FROM judge_rounds WHERE sid = ? AND incident_id = ?", (sid, iid)
        ).fetchone()
    except sqlite3.OperationalError:  # judge_rounds is created on first judge use
        judged = None
    return "judge" if judged else "investigation"


def summarize(events: list[Event]) -> dict[str, Any]:
    """The run_log fields that come from a run's event stream."""
    started: dict = {}
    verdict: dict = {}
    out: dict[str, Any] = {"tool_calls": 0, "pr": 0, "simulated": None, "error": None}
    final_state = None
    for e in events:
        if e.type == EventType.RUN_STARTED and not started:
            started = e.data
        elif e.type == EventType.TOOL_CALL:
            out["tool_calls"] += 1
        elif e.type == EventType.VERDICT:
            verdict = e.data
        elif e.type == EventType.STATE:
            final_state = e.data.get("state")
        elif e.type == EventType.ACTION and e.data.get("kind") == "pr":
            if e.data.get("status") == "ok":
                out["pr"] = 1
                out["simulated"] = int(bool(e.data.get("simulated")))
        elif e.type == EventType.ERROR and out["error"] is None:
            out["error"] = _scrub(str(e.data.get("message") or e.data.get("code") or "error"))
    duration = None
    if events and events[-1].type == EventType.DONE:
        duration = events[-1].data.get("ms")
        if duration is None and len(events) > 1:
            duration = round(events[-1].ts - events[0].ts, 1)
    out.update(
        started=started,
        verdict=verdict.get("outcome"),
        degraded=None if not verdict else int(bool(verdict.get("degraded"))),
        degraded_reason=verdict.get("degraded_reason"),
        final_state=final_state,
        duration_ms=duration,
    )
    return out


def _insert(
    c: sqlite3.Connection,
    *,
    verb: str,
    run_id: str,
    sid: str,
    incident_id: str,
    created_at: float,
    events: list[Event],
    tokens: tuple[int, int, float] | None,
) -> None:
    s = summarize(events)
    started = s.pop("started")
    p, comp, usd = tokens if tokens is not None else (None, None, None)
    c.execute(
        f"{verb} INTO run_log (run_id, ts, day, kind, incident_id, fault_class, verdict, "
        "degraded, degraded_reason, final_state, tool_calls, prompt_tokens, completion_tokens, "
        "usd, duration_ms, pr, simulated, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            created_at,
            day(created_at),
            _kind(c, sid, incident_id, started),
            incident_id,
            _fault_class(c, sid, incident_id, started),
            s["verdict"],
            s["degraded"],
            s["degraded_reason"],
            s["final_state"],
            s["tool_calls"],
            p,
            comp,
            usd,
            s["duration_ms"],
            s["pr"],
            s["simulated"],
            s["error"],
        ),
    )


def log_run(store: Store, run: Any, events: list[Event]) -> None:
    """Record a finished run (a ``ic.runs.registry.Run``) from its full event list, ``done``
    included. Never raises."""
    try:
        with store.conn() as c:
            _insert(
                c,
                verb="INSERT OR REPLACE",
                run_id=run.id,
                sid=run.sid,
                incident_id=run.incident_id,
                created_at=run.created_at,
                events=events,
                tokens=(run.prompt_tokens, run.completion_tokens, run.usd),
            )
    except Exception:
        log.warning("run summary for %s not recorded", getattr(run, "id", "?"), exc_info=True)


def backfill(store: Store) -> int:
    """Summarize finished runs still in ``runs`` that have no ``run_log`` row (runs from
    before this table existed). Token counts are unknown for those and stay NULL."""
    with store.conn() as c:
        rows = c.execute(
            "SELECT r.run_id, r.sid, r.incident_id, r.created_at FROM runs r "
            "WHERE r.status = 'done' AND NOT EXISTS "
            "(SELECT 1 FROM run_log l WHERE l.run_id = r.run_id)"
        ).fetchall()
        for run_id, sid, iid, created_at in rows:
            evs = [
                Event.model_validate_json(r[0])
                for r in c.execute(
                    "SELECT json FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
                )
            ]
            _insert(
                c,
                verb="INSERT OR IGNORE",
                run_id=run_id,
                sid=sid,
                incident_id=iid,
                created_at=created_at,
                events=evs,
                tokens=None,
            )
    return len(rows)
