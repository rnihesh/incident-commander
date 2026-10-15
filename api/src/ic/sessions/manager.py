"""Session lifecycle (KTD10): server-generated ids, SQLite registry, exact-name cleanup."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ic.graph import naming
from ic.graph.client import delete_graph
from ic.graph.snapshots import create_session_world
from ic.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Session:
    sid: str
    ip: str
    created_at: float
    last_seen: float
    active_runs: int


def _row(r) -> Session:
    return Session(r["sid"], r["ip"], r["created_at"], r["last_seen"], r["active_runs"])


class SessionManager:
    def __init__(self, store: Store, *, max_sessions: int, idle_ttl_s: float = 1800):
        self.store = store
        self.max_sessions = max_sessions
        self.idle_ttl_s = idle_ttl_s

    async def create(self, ip: str, *, now: float | None = None) -> Session:
        sid = naming.new_session_id()
        now = time.time() if now is None else now
        with self.store.conn() as c:
            c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, 0)", (sid, ip, now, now))
        try:
            world = await create_session_world(sid)
        except Exception:
            self._drop_rows(sid)
            raise
        self.track_graph(sid, world)
        return Session(sid, ip, now, now, 0)

    def get(self, sid: str | None) -> Session | None:
        """Malformed or unknown ids return None so the caller starts a fresh session."""
        if not isinstance(sid, str):
            return None
        try:
            naming.session_world(sid)  # format validation
        except ValueError:
            return None
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
        return _row(r) if r else None

    def touch(self, sid: str, *, now: float | None = None) -> None:
        with self.store.conn() as c:
            c.execute(
                "UPDATE sessions SET last_seen = ? WHERE sid = ?",
                (time.time() if now is None else now, sid),
            )

    def track_graph(self, sid: str, name: str) -> None:
        with self.store.conn() as c:
            c.execute("INSERT OR IGNORE INTO session_graphs VALUES (?, ?)", (sid, name))

    def tracked_graphs(self, sid: str) -> list[str]:
        with self.store.conn() as c:
            rows = c.execute("SELECT graph_name FROM session_graphs WHERE sid = ?", (sid,))
            return [r[0] for r in rows]

    def run_started(self, sid: str) -> None:
        with self.store.conn() as c:
            c.execute("UPDATE sessions SET active_runs = active_runs + 1 WHERE sid = ?", (sid,))

    def run_finished(self, sid: str) -> None:
        with self.store.conn() as c:
            c.execute(
                "UPDATE sessions SET active_runs = MAX(active_runs - 1, 0) WHERE sid = ?", (sid,)
            )

    def count_active(self) -> int:
        with self.store.conn() as c:
            return c.execute("SELECT count(*) FROM sessions").fetchone()[0]

    def _drop_rows(self, sid: str) -> None:
        with self.store.conn() as c:
            c.execute("DELETE FROM session_graphs WHERE sid = ?", (sid,))
            c.execute("DELETE FROM sessions WHERE sid = ?", (sid,))

    async def cleanup(self, now: float | None = None) -> list[str]:
        """Delete tracked graphs (exact names only) of idle sessions with no run in progress."""
        now = time.time() if now is None else now
        with self.store.conn() as c:
            idle = [
                r[0]
                for r in c.execute(
                    "SELECT sid FROM sessions WHERE last_seen < ? AND active_runs = 0",
                    (now - self.idle_ttl_s,),
                )
            ]
        removed: list[str] = []
        for sid in idle:
            try:
                for name in self.tracked_graphs(sid):
                    await delete_graph(name)
                    removed.append(name)
            except Exception:
                log.exception("session cleanup failed for %s; keeping rows for retry", sid)
                continue
            self._drop_rows(sid)
        return removed
