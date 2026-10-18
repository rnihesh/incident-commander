"""In-process run registry: one append-only event log per run (R32).

Every event gets the next integer id of its run (0, 1, 2, ...), is persisted to SQLite
(``run_events``) before subscribers are woken, and is kept in memory for live streams. A
client reconnecting with ``Last-Event-ID: n`` gets exactly the events after ``n``, then the
live tail; a reconnect after a process restart replays from SQLite. A run that was still
in flight when its process died is closed on load with an ``error`` and a ``done`` event,
so a replaying client never waits forever.

Event ``ts`` is epoch milliseconds, matching the console's recorded fixtures.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ic import usage
from ic.api.events import Agent, Event, EventType
from ic.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    sid         TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_sid ON runs (sid);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    id     INTEGER NOT NULL,
    json   TEXT NOT NULL,
    PRIMARY KEY (run_id, id)
);
"""

RUNNING, DONE = "running", "done"
MEMORY_TTL_S = 3600.0  # finished runs leave memory after this; SQLite still has them


def new_run_id() -> str:
    return f"run-{secrets.token_hex(8)}"


def _json_safe(data: dict) -> dict:
    return json.loads(json.dumps(data, default=str))


@dataclass
class Run:
    id: str
    sid: str
    incident_id: str
    status: str = RUNNING
    created_at: float = field(default_factory=time.time)
    events: list[Event] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # LLM usage attributed to this run (the orchestrator's usage hook adds to these).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    def add_usage(self, prompt_tokens: int, completion_tokens: int, usd: float) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.usd += usd

    @property
    def finished(self) -> bool:
        return self.status != RUNNING

    def of(self, type_: EventType) -> list[dict]:
        return [e.data for e in self.events if e.type == type_]

    @property
    def types(self) -> list[str]:
        return [str(e.type) for e in self.events]


def _ready(run: Run, seen: int) -> bool:
    return len(run.events) > seen or run.finished


class RunRegistry:
    def __init__(self, store: Store):
        self.store = store
        self._runs: dict[str, Run] = {}
        with store.conn() as c:
            c.executescript(SCHEMA)

    # --- lifecycle --------------------------------------------------------------------

    def create(self, sid: str, incident_id: str, *, run_id: str | None = None) -> Run:
        self._evict()
        run = Run(run_id or new_run_id(), sid, incident_id)
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (run.id, sid, incident_id, RUNNING, run.created_at),
            )
        self._runs[run.id] = run
        return run

    async def emit(
        self, run: Run, type_: EventType | str, data: dict, *, agent: Agent | str = Agent.SYSTEM
    ) -> Event:
        """Append one event (persisted first), wake subscribers. ``done`` closes the run."""
        async with run.emit_lock:  # one writer per run: ids stay dense and ordered
            if run.finished:
                raise RuntimeError(f"run {run.id} is finished")
            ev = Event(
                id=len(run.events),
                run_id=run.id,
                ts=round(time.time() * 1000, 1),
                type=EventType(type_),
                agent=Agent(agent),
                data=_json_safe(data),
            )
            await asyncio.to_thread(self._persist, run, ev)
            async with run.cond:
                run.events.append(ev)
                if ev.type == EventType.DONE:
                    run.status = DONE
                run.cond.notify_all()
        return ev

    def _persist(self, run: Run, ev: Event) -> None:
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO run_events VALUES (?, ?, ?)", (run.id, ev.id, ev.model_dump_json())
            )
            if ev.type == EventType.DONE:
                c.execute("UPDATE runs SET status = ? WHERE run_id = ?", (DONE, run.id))
        if ev.type == EventType.DONE:  # the admin panel's per-run summary, before anyone wakes
            usage.log_run(self.store, run, [*run.events, ev])

    def emitter(self, run: Run):
        """An ``emit(type, data, *, agent)`` callback (the agent loop's shape) for ``run``."""

        async def emit(type_: EventType, data: dict, *, agent: Agent = Agent.TRIAGE) -> None:
            await self.emit(run, type_, data, agent=agent)

        return emit

    def active_for(self, sid: str, incident_id: str) -> bool:
        """True while a run for this session's incident is still in flight in this process."""
        return any(
            r.sid == sid and r.incident_id == incident_id and not r.finished
            for r in self._runs.values()
        )

    def incident_events(self, sid: str, incident_id: str, type_: EventType) -> list[dict]:
        """Data of every ``type_`` event in this session's runs of the incident, newest run
        first. Read from SQLite (events are persisted before anyone sees them), so finished
        runs evicted from memory and runs from before a restart are included."""
        return [data for _, data in self.incident_events_by_run(sid, incident_id, type_)]

    def incident_events_by_run(
        self, sid: str, incident_id: str, type_: EventType
    ) -> list[tuple[str, dict]]:
        """``incident_events`` as (run_id, data): newest run first, each run in event order."""
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT e.json FROM run_events e JOIN runs r ON r.run_id = e.run_id "
                "WHERE r.sid = ? AND r.incident_id = ? AND json_extract(e.json, '$.type') = ? "
                "ORDER BY r.created_at DESC, e.id",
                (sid, incident_id, str(type_)),
            ).fetchall()
        events = [Event.model_validate_json(r[0]) for r in rows]
        return [(e.run_id, e.data) for e in events]

    def get(self, run_id: str) -> Run | None:
        if not isinstance(run_id, str) or len(run_id) > 64:
            return None
        run = self._runs.get(run_id)
        if run is not None:
            return run
        return self._load(run_id)

    def _load(self, run_id: str) -> Run | None:
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            rows = c.execute(
                "SELECT json FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        run = Run(row["run_id"], row["sid"], row["incident_id"], row["status"], row["created_at"])
        run.events = [Event.model_validate_json(r[0]) for r in rows]
        if not run.finished:
            # Not live in this process: its process died mid-run. Close it on the record.
            self._close_interrupted(run)
        self._runs[run.id] = run
        return run

    def _close_interrupted(self, run: Run) -> None:
        now = round(time.time() * 1000, 1)
        tail = [
            (
                EventType.ERROR,
                {"code": "failed", "message": "the run was interrupted by a server restart"},
            ),
            (EventType.DONE, {"ms": None, "interrupted": True}),
        ]
        with self.store.conn() as c:
            for type_, data in tail:
                ev = Event(
                    id=len(run.events),
                    run_id=run.id,
                    ts=now,
                    type=type_,
                    agent=Agent.SYSTEM,
                    data=data,
                )
                c.execute(
                    "INSERT INTO run_events VALUES (?, ?, ?)",
                    (run.id, ev.id, ev.model_dump_json()),
                )
                run.events.append(ev)
            c.execute("UPDATE runs SET status = ? WHERE run_id = ?", (DONE, run.id))
        run.status = DONE
        usage.log_run(self.store, run, run.events)

    def _evict(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for rid in [
            r.id for r in self._runs.values() if r.finished and r.created_at < now - MEMORY_TTL_S
        ]:
            del self._runs[rid]

    def prune(self, older_than_s: float = 24 * 3600, *, now: float | None = None) -> int:
        """Drop persisted finished runs older than the cutoff. Returns how many."""
        cutoff = (time.time() if now is None else now) - older_than_s
        with self.store.conn() as c:
            ids = [
                r[0]
                for r in c.execute(
                    "SELECT run_id FROM runs WHERE status = ? AND created_at < ?", (DONE, cutoff)
                )
            ]
            for rid in ids:
                c.execute("DELETE FROM run_events WHERE run_id = ?", (rid,))
                c.execute("DELETE FROM runs WHERE run_id = ?", (rid,))
        for rid in ids:
            self._runs.pop(rid, None)
        return len(ids)

    # --- reading ----------------------------------------------------------------------

    async def stream(self, run: Run, after: int = -1) -> AsyncIterator[Event]:
        """Events with id > ``after`` in order, then live ones, ending after ``done``."""
        i = max(after + 1, 0)
        while True:
            async with run.cond:
                await run.cond.wait_for(partial(_ready, run, i))
                batch = run.events[i:]
                finished = run.finished
            for ev in batch:
                yield ev
            i += len(batch)
            if finished and i >= len(run.events):
                return

    def track(self, run: Run, task: asyncio.Task) -> None:
        run.task = task

    def active(self) -> list[Run]:
        return [r for r in self._runs.values() if not r.finished]

    async def wait_all(self, limit_s: float | None = None) -> None:
        """Wait for every in-flight run task (tests, graceful shutdown)."""
        tasks: list[Any] = [r.task for r in self._runs.values() if r.task and not r.task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=limit_s)
