"""Sliding-window abuse limits backed by SQLite so they survive restarts (KTD10).

Over any limit the visitor gets a recorded run (``fallback="recorded_run"``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ic.config import get_settings
from ic.store import Store

HOUR = 3600.0
SESSION_RUNS_PER_HOUR = 3
SESSION_CONCURRENT_RUNS = 1
IP_CONCURRENT_SESSIONS = 2
IP_SESSIONS_PER_HOUR = 6
IP_RUNS_PER_HOUR = 6
MCP_REQUESTS_PER_HOUR = 600
MCP_TOKENS_PER_HOUR = 20


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    fallback: str | None = None


OK = Decision(True)


def _deny(reason: str) -> Decision:
    return Decision(False, reason, "recorded_run")


class Limiter:
    def __init__(
        self,
        store: Store,
        *,
        max_sessions: int | None = None,
        global_runs_per_hour: int | None = None,
        idle_ttl_s: float | None = None,
        proof_verifies_per_ip_hour: int | None = None,
    ):
        s = get_settings()
        self.store = store
        self.max_sessions = s.max_sessions if max_sessions is None else max_sessions
        self.global_runs_per_hour = (
            s.global_runs_per_hour if global_runs_per_hour is None else global_runs_per_hour
        )
        self.idle_ttl_s = s.session_idle_ttl_s if idle_ttl_s is None else idle_ttl_s
        self.session_runs_per_hour = s.session_runs_per_hour
        self.ip_concurrent_sessions = s.ip_concurrent_sessions
        self.ip_sessions_per_hour = s.ip_sessions_per_hour
        self.ip_runs_per_hour = s.ip_runs_per_hour
        self.proof_verifies_per_ip_hour = (
            s.proof_verifies_per_ip_hour
            if proof_verifies_per_ip_hour is None
            else proof_verifies_per_ip_hour
        )

    def _count(self, kind: str, key: str, now: float) -> int:
        with self.store.conn() as c:
            return c.execute(
                "SELECT count(*) FROM limit_events WHERE kind = ? AND key = ? AND ts > ?",
                (kind, key, now - HOUR),
            ).fetchone()[0]

    def _record(self, kind: str, key: str, now: float) -> None:
        with self.store.conn() as c:
            c.execute("INSERT INTO limit_events VALUES (?, ?, ?)", (kind, key, now))
            c.execute("DELETE FROM limit_events WHERE ts < ?", (now - 2 * HOUR,))

    def check_session_create(self, ip: str, *, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        with self.store.conn() as c:
            total = c.execute("SELECT count(*) FROM sessions").fetchone()[0]
            mine = c.execute(
                "SELECT count(*) FROM sessions WHERE ip = ? AND last_seen > ?",
                (ip, now - self.idle_ttl_s),
            ).fetchone()[0]
        if total >= self.max_sessions:
            return _deny(f"server is at capacity ({self.max_sessions} concurrent sessions)")
        if mine >= self.ip_concurrent_sessions:
            return _deny(f"at most {self.ip_concurrent_sessions} concurrent sessions per client")
        if self._count("session_create_ip", ip, now) >= self.ip_sessions_per_hour:
            return _deny(f"at most {self.ip_sessions_per_hour} new sessions per hour per client")
        return OK

    def record_session_create(self, ip: str, *, now: float | None = None) -> None:
        self._record("session_create_ip", ip, time.time() if now is None else now)

    def check_run(self, sid: str, ip: str, *, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        with self.store.conn() as c:
            r = c.execute("SELECT active_runs FROM sessions WHERE sid = ?", (sid,)).fetchone()
        if r is not None and r[0] >= SESSION_CONCURRENT_RUNS:
            return _deny("a run is already in progress for this session")
        if self._count("run_session", sid, now) >= self.session_runs_per_hour:
            return _deny(f"limit of {self.session_runs_per_hour} runs per hour per session reached")
        if self._count("run_ip", ip, now) >= self.ip_runs_per_hour:
            return _deny(f"limit of {self.ip_runs_per_hour} runs per hour per client reached")
        if self._count("run_global", "*", now) >= self.global_runs_per_hour:
            return _deny("global hourly run ceiling reached")
        return OK

    def record_run(self, sid: str, ip: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._record("run_session", sid, now)
        self._record("run_ip", ip, now)
        self._record("run_global", "*", now)

    def reserve_run(self, sid: str, ip: str, *, now: float | None = None) -> float:
        """``record_run`` right after ``check_run`` passed, in the same synchronous stretch,
        so concurrent requests from other sessions cannot all pass the check before any of
        them records. Returns the reservation's timestamp for ``release_run``."""
        now = time.time() if now is None else now
        self.record_run(sid, ip, now=now)
        return now

    def release_run(self, sid: str, ip: str, ts: float) -> None:
        """Undo a ``reserve_run`` whose run never started (injection failed, refused)."""
        with self.store.conn() as c:
            for kind, key in (("run_session", sid), ("run_ip", ip), ("run_global", "*")):
                c.execute(
                    "DELETE FROM limit_events WHERE rowid = (SELECT rowid FROM limit_events "
                    "WHERE kind = ? AND key = ? AND ts = ? LIMIT 1)",
                    (kind, key, ts),
                )

    def session_verify_retry(
        self, sid: str, limit: int, *, now: float | None = None
    ) -> float | None:
        """Count one session Verify against the per-session hourly cap: None when allowed,
        else the seconds until the oldest counted Verify leaves the window."""
        return self._hourly_retry("verify_session", sid, limit, now)

    def session_fragility_retry(
        self, sid: str, limit: int, *, now: float | None = None
    ) -> float | None:
        """``session_verify_retry`` for the judge's fragility map."""
        return self._hourly_retry("fragility_session", sid, limit, now)

    def session_ask_retry(self, sid: str, limit: int, *, now: float | None = None) -> float | None:
        """``session_verify_retry`` for Ask the graph questions."""
        return self._hourly_retry("ask_session", sid, limit, now)

    def _hourly_retry(self, kind: str, key: str, limit: int, now: float | None) -> float | None:
        now = time.time() if now is None else now
        if self._allow_hourly(kind, key, limit, now):
            return None
        with self.store.conn() as c:
            oldest = c.execute(
                "SELECT min(ts) FROM limit_events WHERE kind = ? AND key = ? AND ts > ?",
                (kind, key, now - HOUR),
            ).fetchone()[0]
        return max(1.0, (oldest or now) + HOUR - now)

    def _allow_hourly(self, kind: str, key: str, limit: int, now: float | None) -> bool:
        now = time.time() if now is None else now
        if self._count(kind, key, now) >= limit:
            return False
        self._record(kind, key, now)
        return True

    def allow_mcp(self, ip: str, *, now: float | None = None) -> bool:
        """Count one MCP HTTP request against the per-IP hourly limit."""
        return self._allow_hourly("mcp_ip", ip, MCP_REQUESTS_PER_HOUR, now)

    def allow_proof_verify(self, ip: str, *, now: float | None = None) -> bool:
        """Count one public proof page re-verify against the per-IP hourly limit."""
        return self._allow_hourly("proof_verify_ip", ip, self.proof_verifies_per_ip_hour, now)

    def allow_mcp_token(self, ip: str, *, now: float | None = None) -> bool:
        return self._allow_hourly("mcp_token_ip", ip, MCP_TOKENS_PER_HOUR, now)
