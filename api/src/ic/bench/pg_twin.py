"""Postgres twin of a world (R29, KTD15): the same facts in relational tables, the same core
operations in SQL, timed against FalkorDB on the same host.

Tables (one schema per load, so concurrent runs never collide):

  services(id pk, name, region)
  depends_on(caller, callee, valid_from, valid_to, recorded_at)      bitemporal
  observed_calls(caller, callee, count, first_seen)
  changes(id pk, service_id, kind, valid_from, recorded_at)
  postmortems(id pk, embedding vector(dims))

Indexes mirror FalkorDB's: id/name lookups (FalkorDB range indexes on Service.id/name and
Change.id/valid_from), both endpoints of every edge table (FalkorDB's adjacency matrices
give it both directions for free), and an HNSW cosine index on the postmortem embedding
(FalkorDB's vector index is HNSW too).

Operations, each equal by construction to its FalkorDB counterpart on an as-of snapshot:

  closure   services a service's failure reaches: recursive CTE over callee -> caller edges
            (DEPENDS_ON valid at t, OBSERVED_CALL above threshold seen by t), filtered by
            valid time inside the query;  FalkorDB: algo.BFS over IMPACTS on the snapshot
  path      shortest hop count between two services: level-by-level recursive CTE;
            FalkorDB: shortestPath over IMPACTS
  topk      top-k precedent postmortems by cosine distance (pgvector ``<=>``);
            FalkorDB: db.idx.vector.queryNodes
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

import numpy as np
import psycopg
from psycopg import sql

from ic.graph.client import query
from ic.graph.schema import OBSERVED_CALL_THRESHOLD, OPEN_END

NEG_INF = -(2**62)
POS_INF = OPEN_END * 10
MAX_PATH_DEPTH = 24
BFS_DEPTH = 64  # effectively unbounded for algo.BFS (0 means "no hops" there)

_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


@dataclass
class WorldRows:
    services: list[tuple[str, str, str]] = field(default_factory=list)
    depends_on: list[tuple[str, str, int, int, int | None]] = field(default_factory=list)
    observed_calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    changes: list[tuple[str, str, str, int, int | None]] = field(default_factory=list)
    postmortems: list[tuple[str, list[float]]] = field(default_factory=list)

    @property
    def dims(self) -> int:
        return len(self.postmortems[0][1]) if self.postmortems else 512


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True, timeout_ms=60000)).result_set


async def read_world(graph: str) -> WorldRows:
    """Everything the twin needs, read from a FalkorDB world graph (bitemporal fields intact:
    the twin filters by valid time at query time instead of materializing a snapshot)."""
    w = WorldRows()
    w.services = [
        (a, b, c)
        for a, b, c in await _rows(graph, "MATCH (s:Service) RETURN s.id, s.name, s.region")
    ]
    w.depends_on = [
        (a, b, NEG_INF if vf is None else vf, POS_INF if vt is None else vt, rec)
        for a, b, vf, vt, rec in await _rows(
            graph,
            "MATCH (a:Service)-[e:DEPENDS_ON]->(b:Service) "
            "RETURN a.id, b.id, e.valid_from, e.valid_to, e.recorded_at",
        )
    ]
    w.observed_calls = [
        (a, b, n, fs)
        for a, b, n, fs in await _rows(
            graph,
            "MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service) "
            "RETURN a.id, b.id, o.count, o.first_seen",
        )
    ]
    w.changes = [
        tuple(r)
        for r in await _rows(
            graph,
            "MATCH (c:Change)-[:CHANGES]->(s:Service) "
            "RETURN c.id, s.id, c.kind, c.valid_from, c.recorded_at",
        )
    ]
    w.postmortems = [
        (a, list(v))
        for a, v in await _rows(
            graph, "MATCH (p:Postmortem) RETURN p.id, p.embedding ORDER BY p.id"
        )
        if v is not None
    ]
    return w


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in v) + "]"


CLOSURE_SQL = """
WITH RECURSIVE reach(id) AS (
    SELECT %(src)s::text
  UNION
    SELECT e.caller FROM reach r
    JOIN (
        SELECT d.callee, d.caller FROM depends_on d
        WHERE d.valid_from <= %(t)s AND %(t)s < d.valid_to
          AND (NOT %(known)s OR (d.recorded_at IS NOT NULL AND d.recorded_at <= %(t)s))
      UNION ALL
        SELECT o.callee, o.caller FROM observed_calls o
        WHERE o."count" >= %(thr)s AND o.first_seen <= %(t)s
    ) e ON e.callee = r.id
)
SELECT id FROM reach WHERE id <> %(src)s
"""

PATH_SQL = """
WITH RECURSIVE bfs(id, depth) AS (
    SELECT %(src)s::text, 0
  UNION
    SELECT e.caller, b.depth + 1 FROM bfs b
    JOIN (
        SELECT d.callee, d.caller FROM depends_on d
        WHERE d.valid_from <= %(t)s AND %(t)s < d.valid_to
          AND (NOT %(known)s OR (d.recorded_at IS NOT NULL AND d.recorded_at <= %(t)s))
      UNION ALL
        SELECT o.callee, o.caller FROM observed_calls o
        WHERE o."count" >= %(thr)s AND o.first_seen <= %(t)s
    ) e ON e.callee = b.id
    WHERE b.depth < %(maxd)s
)
SELECT min(depth) FROM bfs WHERE id = %(dst)s AND depth > 0
"""

TOPK_SQL = """
SELECT id, embedding <=> %(v)s::vector AS distance
FROM postmortems ORDER BY embedding <=> %(v)s::vector, id LIMIT %(k)s
"""


class PgTwin:
    """A loaded twin in its own schema. Use as a context manager to drop it afterwards."""

    def __init__(self, dsn: str, schema: str):
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError(f"invalid schema name {schema!r}")
        self.dsn, self.schema = dsn, schema
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    def __enter__(self) -> PgTwin:
        return self

    def __exit__(self, *exc) -> None:
        self.drop()

    def _use_schema(self) -> None:
        self.conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema))
        )

    def load(self, w: WorldRows) -> None:
        s = sql.Identifier(self.schema)
        c = self.conn
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(s))
        c.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        self._use_schema()
        c.execute("CREATE TABLE services (id text PRIMARY KEY, name text, region text)")
        c.execute(
            "CREATE TABLE depends_on (caller text NOT NULL, callee text NOT NULL, "
            "valid_from bigint NOT NULL, valid_to bigint NOT NULL, recorded_at bigint)"
        )
        c.execute(
            'CREATE TABLE observed_calls (caller text NOT NULL, callee text NOT NULL, "count" '
            "bigint NOT NULL, first_seen bigint NOT NULL)"
        )
        c.execute(
            "CREATE TABLE changes (id text PRIMARY KEY, service_id text NOT NULL, kind text, "
            "valid_from bigint NOT NULL, recorded_at bigint)"
        )
        c.execute(
            sql.SQL("CREATE TABLE postmortems (id text PRIMARY KEY, embedding vector({}))").format(
                sql.Literal(w.dims)
            )
        )
        with c.cursor() as cur:
            for table, cols, rows in (
                ("services", "(id, name, region)", w.services),
                ("depends_on", "(caller, callee, valid_from, valid_to, recorded_at)", w.depends_on),
                ("observed_calls", '(caller, callee, "count", first_seen)', w.observed_calls),
                ("changes", "(id, service_id, kind, valid_from, recorded_at)", w.changes),
            ):
                with cur.copy(f"COPY {table} {cols} FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
            with cur.copy("COPY postmortems (id, embedding) FROM STDIN") as cp:
                for pid, v in w.postmortems:
                    cp.write_row((pid, _vec(v)))
        for stmt in (
            "CREATE INDEX ON services (name)",
            "CREATE INDEX ON depends_on (callee, valid_from, valid_to)",
            "CREATE INDEX ON depends_on (caller)",
            "CREATE INDEX ON observed_calls (callee)",
            "CREATE INDEX ON observed_calls (caller)",
            "CREATE INDEX ON changes (service_id)",
            "CREATE INDEX ON changes (valid_from)",
            "CREATE INDEX ON postmortems USING hnsw (embedding vector_cosine_ops)",
        ):
            c.execute(stmt)
        c.execute("ANALYZE")

    def counts(self) -> dict[str, int]:
        self._use_schema()
        out = {}
        for t in ("services", "depends_on", "observed_calls", "changes", "postmortems"):
            out[t] = self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        return out

    def closure(self, src: str, t: int, *, known: bool = False) -> set[str]:
        p = {"src": src, "t": t, "known": known, "thr": OBSERVED_CALL_THRESHOLD}
        return {r[0] for r in self.conn.execute(CLOSURE_SQL, p).fetchall()}

    def path_length(
        self, src: str, dst: str, t: int, *, known: bool = False, max_depth: int = MAX_PATH_DEPTH
    ) -> int | None:
        p = {
            "src": src,
            "dst": dst,
            "t": t,
            "known": known,
            "thr": OBSERVED_CALL_THRESHOLD,
            "maxd": max_depth,
        }
        return self.conn.execute(PATH_SQL, p).fetchone()[0]

    def topk(self, vector: list[float], k: int) -> list[tuple[str, float]]:
        rows = self.conn.execute(TOPK_SQL, {"v": _vec(vector), "k": k}).fetchall()
        return [(a, float(b)) for a, b in rows]

    def drop(self) -> None:
        try:
            self.conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )
        finally:
            self.conn.close()


# --- FalkorDB counterparts on an as-of snapshot -----------------------------------------

F_CLOSURE = (
    "MATCH (s:Service {id: $src}) CALL algo.BFS(s, $depth, 'IMPACTS') YIELD nodes "
    "RETURN [x IN nodes WHERE x:Service | x.id]"
)
F_PATH = (
    "MATCH (a:Service {id: $src}), (b:Service {id: $dst}) WITH a, b "
    "RETURN length(shortestPath((a)-[:IMPACTS*]->(b)))"
)
F_TOPK = (
    "CALL db.idx.vector.queryNodes('Postmortem', 'embedding', $k, vecf32($v)) YIELD node, score "
    "RETURN node.id, score ORDER BY score, node.id"
)


async def falkor_closure(snapshot: str, src: str) -> set[str]:
    rows = await _rows(snapshot, F_CLOSURE, {"src": src, "depth": BFS_DEPTH})
    return set(rows[0][0]) - {src} if rows else set()


async def falkor_path_length(snapshot: str, src: str, dst: str) -> int | None:
    rows = await _rows(snapshot, F_PATH, {"src": src, "dst": dst})
    return rows[0][0] if rows else None


async def falkor_topk(snapshot: str, vector: list[float], k: int) -> list[tuple[str, float]]:
    rows = await _rows(snapshot, F_TOPK, {"k": k, "v": vector})
    return [(a, float(b)) for a, b in rows]


# --- timing --------------------------------------------------------------------------------


def _pct(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs, dtype=float)
    return {
        "p50_ms": round(float(np.percentile(a, 50)), 3),
        "p95_ms": round(float(np.percentile(a, 95)), 3),
        "n": len(xs),
    }


async def _timed(fn, *args) -> tuple[object, float]:
    t0 = time.perf_counter()
    out = fn(*args)
    if hasattr(out, "__await__"):
        out = await out
    return out, (time.perf_counter() - t0) * 1000


async def compare(
    snapshot: str,
    twin: PgTwin,
    t: int,
    *,
    iterations: int = 50,
    seed: int = 0,
    k: int = 5,
    query_vectors: list[list[float]] | None = None,
) -> dict:
    """Time closure, shortest path and top-k precedent on both engines and check that the
    answers agree. Every input runs once untimed first (warm cache, agreement check), then
    ``iterations`` timed runs per engine and operation."""
    rng = random.Random(f"pgtwin:{seed}")
    services = sorted(r[0] for r in await _rows(snapshot, "MATCH (s:Service) RETURN s.id"))
    pm_rows = await _rows(snapshot, "MATCH (p:Postmortem) RETURN p.id, p.embedding ORDER BY p.id")
    pm_ids = [r[0] for r in pm_rows]
    pm_vecs = [list(r[1]) for r in pm_rows]
    pm_mat = np.asarray(pm_vecs, dtype=float) if pm_vecs else np.zeros((0, 512))
    if len(pm_mat):
        pm_mat = (
            pm_mat
            / np.where(np.linalg.norm(pm_mat, axis=1) == 0, 1, np.linalg.norm(pm_mat, axis=1))[
                :, None
            ]
        )
    srcs = [rng.choice(services) for _ in range(iterations)]

    # closure: warm + agreement, and the reach sets feed the path pairs
    agree = {"closure": 0, "path": 0, "topk": 0}
    reach: dict[str, set[str]] = {}
    for s in dict.fromkeys(srcs):
        f = await falkor_closure(snapshot, s)
        p = twin.closure(s, t)
        reach[s] = f
        agree["closure"] += f == p
    n_closure = len(reach)
    pairs = []
    for s in srcs:
        targets = sorted(reach[s]) or [x for x in services if x != s]  # unreachable pair
        pairs.append((s, rng.choice(targets)))
    for a, b in pairs:
        agree["path"] += (await falkor_path_length(snapshot, a, b)) == twin.path_length(a, b, t)
    if query_vectors is None:
        query_vectors = []
        for _ in range(iterations):
            base = np.asarray(rng.choice(pm_vecs) if pm_vecs else [0.0] * 512, dtype=float)
            noise = np.asarray([rng.gauss(0, 0.05) for _ in range(len(base))])
            v = base + noise
            query_vectors.append(list(v / (np.linalg.norm(v) or 1.0)))
    exact = {"falkordb": 0, "postgres": 0}  # matches brute-force cosine ranking
    for v in query_vectors:
        f_ids = [i for i, _ in await falkor_topk(snapshot, v, k)]
        p_ids = [i for i, _ in twin.topk(v, k)]
        agree["topk"] += f_ids == p_ids
        q = np.asarray(v, dtype=float)
        dist = 1 - pm_mat @ (q / (np.linalg.norm(q) or 1.0))
        truth = [pm_ids[i] for i in sorted(range(len(pm_ids)), key=lambda i: (dist[i], pm_ids[i]))]
        exact["falkordb"] += f_ids == truth[:k]
        exact["postgres"] += p_ids == truth[:k]

    timings: dict[str, dict[str, list[float]]] = {
        op: {"falkordb": [], "postgres": []} for op in ("closure", "path", "topk")
    }
    for s in srcs:
        timings["closure"]["falkordb"].append((await _timed(falkor_closure, snapshot, s))[1])
        timings["closure"]["postgres"].append((await _timed(twin.closure, s, t))[1])
    for a, b in pairs:
        timings["path"]["falkordb"].append((await _timed(falkor_path_length, snapshot, a, b))[1])
        timings["path"]["postgres"].append((await _timed(twin.path_length, a, b, t))[1])
    for v in query_vectors:
        timings["topk"]["falkordb"].append((await _timed(falkor_topk, snapshot, v, k))[1])
        timings["topk"]["postgres"].append((await _timed(twin.topk, v, k))[1])

    checked = {"closure": n_closure, "path": len(pairs), "topk": len(query_vectors)}
    labels = {
        "closure": "Upstream closure (algo.BFS vs recursive CTE, valid-time filter)",
        "path": "Shortest path (shortestPath vs recursive CTE)",
        "topk": f"Top-{k} precedent (vector index vs pgvector HNSW)",
    }
    ops = {
        op: {
            "label": labels[op],
            "falkordb": _pct(timings[op]["falkordb"]),
            "postgres": _pct(timings[op]["postgres"]),
            "checked": checked[op],
            "agreed": agree[op],
        }
        for op in ("closure", "path", "topk")
    }
    ops["topk"]["exact"] = exact  # both indexes are approximate (HNSW)
    return {"t": t, "iterations": iterations, "ops": ops}
