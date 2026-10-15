"""SQLite store owned by the API process (KTD11).

Holds ground truth that no tool, MCP handler or agent context may read: the fault answer
key, and the proof-chain head hashes anchored at incident completion (KTD4).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ic.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_key (
    fault_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    fault_class TEXT NOT NULL,
    root_id     TEXT NOT NULL,
    nonce       TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS answer_key_incident ON answer_key (incident_id, session_id);
CREATE TABLE IF NOT EXISTS chain_anchor (
    session_id  TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    head_hash   TEXT NOT NULL,
    anchored_at INTEGER NOT NULL,
    t           INTEGER,
    world       TEXT,
    PRIMARY KEY (session_id, incident_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    sid         TEXT PRIMARY KEY,
    ip          TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    active_runs INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_ip ON sessions (ip);
CREATE TABLE IF NOT EXISTS session_graphs (
    sid        TEXT NOT NULL,
    graph_name TEXT NOT NULL,
    PRIMARY KEY (sid, graph_name)
);
CREATE TABLE IF NOT EXISTS limit_events (
    kind TEXT NOT NULL,
    key  TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS limit_events_k ON limit_events (kind, key, ts);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts                REAL NOT NULL,
    day               TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    usd               REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_day ON llm_usage (day);

-- PR copies, their frozen anchor and the rehearsal seal (ic.actions.pr_store).
CREATE TABLE IF NOT EXISTS pr_graphs (
    graph_name  TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    sid         TEXT NOT NULL,
    pr_url      TEXT,
    created_at  REAL NOT NULL,
    proof_ref   TEXT NOT NULL,
    head_sha    TEXT,
    simulated   INTEGER
);
CREATE INDEX IF NOT EXISTS pr_graphs_ref ON pr_graphs (proof_ref);
CREATE TABLE IF NOT EXISTS pr_anchor (
    sid            TEXT NOT NULL,
    incident_id    TEXT NOT NULL,
    head_hash      TEXT NOT NULL,
    t              INTEGER,
    world          TEXT,
    rehearsal_hash TEXT,
    frozen_at      REAL NOT NULL,
    PRIMARY KEY (sid, incident_id)
);
CREATE TABLE IF NOT EXISTS rehearsal_seal (
    sid          TEXT NOT NULL,
    incident_id  TEXT NOT NULL,
    rehearsal_id TEXT,
    content_hash TEXT NOT NULL,
    sealed_at    REAL NOT NULL,
    PRIMARY KEY (sid, incident_id)
);

-- Judge challenge budget per incident (ic.api.routes_challenge); pruned with its session.
CREATE TABLE IF NOT EXISTS judge_challenges (
    sid         TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    used        INTEGER NOT NULL,
    PRIMARY KEY (sid, incident_id)
);
"""

# Columns added after the first release: (table, column, SQL type). Added in place by
# ``Store.init`` when an older database lacks them; existing rows read back as NULL.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("chain_anchor", "t", "INTEGER"),
    ("chain_anchor", "world", "TEXT"),
    ("answer_key", "commitment", "TEXT"),
    ("pr_graphs", "head_sha", "TEXT"),  # the PR head commit the proof status goes on
    ("pr_graphs", "simulated", "INTEGER"),
)


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path if path is not None else get_settings().store_path)
        self.init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:  # commit or roll back
                yield conn
        finally:
            conn.close()

    def conn(self):
        """Context-managed connection for modules that own their own tables."""
        return self._conn()

    def init(self) -> None:
        """Create the parent directory and all tables. Safe to call repeatedly."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")  # persistent; set once, readers don't block
            c.executescript(SCHEMA)
            for table, column, sql_type in MIGRATIONS:
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def put_answer(
        self,
        *,
        fault_id: str,
        session_id: str,
        incident_id: str,
        fault_class: str,
        root_id: str,
        nonce: str,
        created_at: int | None = None,
        commitment: str | None = None,
    ) -> None:
        """``commitment``: the public commit-reveal hash of this answer (KTD11)."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO answer_key (fault_id, session_id, incident_id, "
                "fault_class, root_id, nonce, created_at, commitment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fault_id,
                    session_id,
                    incident_id,
                    fault_class,
                    root_id,
                    nonce,
                    created_at if created_at is not None else int(time.time()),
                    commitment,
                ),
            )

    def get_answer(self, incident_id: str, session_id: str | None = None) -> dict | None:
        """Latest answer for an incident. Incident ids repeat across sessions, so pass
        ``session_id`` whenever it is known."""
        sql, args = "SELECT * FROM answer_key WHERE incident_id = ?", [incident_id]
        if session_id is not None:
            sql, args = sql + " AND session_id = ?", [*args, session_id]
        with self._conn() as c:
            row = c.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None

    def put_anchor(
        self,
        *,
        session_id: str,
        incident_id: str,
        head_hash: str,
        anchored_at: int | None = None,
        t: int | None = None,
        world: str | None = None,
    ) -> None:
        """Anchor (or re-anchor) an incident's proof-chain head hash (KTD4), with the alert
        time and world graph the chain was proven against (Verify trusts these, not the
        ledger graph's copies)."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO chain_anchor "
                "(session_id, incident_id, head_hash, anchored_at, t, world) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    incident_id,
                    head_hash,
                    anchored_at if anchored_at is not None else int(time.time()),
                    t,
                    world,
                ),
            )

    def get_anchor(self, session_id: str, incident_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM chain_anchor WHERE session_id = ? AND incident_id = ?",
                (session_id, incident_id),
            ).fetchone()
        return dict(row) if row else None
