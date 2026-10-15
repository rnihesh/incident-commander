"""Scale comparison (``ic bench --scale N``): the Postgres twin vs FalkorDB on a LARGE
generated world, with deeper traversals than the seed-world latency table.

``generate_world(seed, n_services=N)`` is loaded into a temporary FalkorDB graph
``b:scale:<tag>:world`` (plus its as-of snapshot) and into a temporary Postgres schema; both
are dropped afterwards, whatever happens.

Operations, each on both engines over the same inputs:

  closure@d   services a service's failure reaches within ``d`` hops (d = 2, 4, 8):
              algo.BFS(s, d, 'IMPACTS') on the snapshot vs a depth-bounded recursive CTE
              with the valid-time filter inside the query
  path        shortest hop count from a source to the FARTHEST service it reaches:
              shortestPath over IMPACTS vs the level-by-level recursive CTE

Sources are not uniform: ``SAMPLE`` services are drawn at random and the ``iterations``
with the largest reach (by an in-memory BFS over the same edges) are kept, so the timings
measure deep traversals rather than leaf services that reach nothing. The in-memory BFS is
also the reference answer each engine is checked against.

Top-k precedent search is not repeated here: incident (and so postmortem) generation does
not grow with the service count, so the large world holds only a handful of postmortems.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from ic.bench import pg_twin
from ic.bench.pg_twin import PgTwin, WorldRows, _pct, _rows, _timed
from ic.config import get_settings
from ic.graph import client, naming
from ic.graph.schema import OBSERVED_CALL_THRESHOLD, WORLD_NOW
from ic.graph.snapshots import build_view
from ic.world.generator import generate_world
from ic.world.loader import load_world

SCALE_FILE = "scale.json"
CLOSURE_DEPTHS = (2, 4, 8)
SAMPLE = 400  # random candidate sources; the deepest `iterations` are timed
TIMING_KEYS = ("generate", "load_falkordb", "snapshot", "read_rows", "load_postgres")

CLOSURE_DEPTH_SQL = """
WITH RECURSIVE reach(id, depth) AS (
    SELECT %(src)s::text, 0
  UNION
    SELECT e.caller, r.depth + 1 FROM reach r
    JOIN (
        SELECT d.callee, d.caller FROM depends_on d
        WHERE d.valid_from <= %(t)s AND %(t)s < d.valid_to
      UNION ALL
        SELECT o.callee, o.caller FROM observed_calls o
        WHERE o."count" >= %(thr)s AND o.first_seen <= %(t)s
    ) e ON e.callee = r.id
    WHERE r.depth < %(maxd)s
)
SELECT DISTINCT id FROM reach WHERE id <> %(src)s
"""

F_CLOSURE_DEPTH = (
    "MATCH (s:Service {id: $src}) CALL algo.BFS(s, $depth, 'IMPACTS') YIELD nodes "
    "RETURN [x IN nodes WHERE x:Service | x.id]"
)


def impact_adjacency(w: WorldRows, t: int) -> dict[str, list[str]]:
    """callee -> callers at ``t`` (as true): the Service->Service IMPACTS edges a snapshot
    materializes (DEPENDS_ON valid at t, OBSERVED_CALL at or above threshold seen by t)."""
    adj: dict[str, set[str]] = {}
    for caller, callee, vf, vt, _rec in w.depends_on:
        if vf <= t < vt:
            adj.setdefault(callee, set()).add(caller)
    for caller, callee, n, first_seen in w.observed_calls:
        if n >= OBSERVED_CALL_THRESHOLD and first_seen <= t:
            adj.setdefault(callee, set()).add(caller)
    return {k: sorted(v) for k, v in adj.items()}


def bfs_hops(adj: dict[str, list[str]], src: str) -> dict[str, int]:
    """Hop distance from ``src`` to every service it reaches (``src`` excluded)."""
    dist = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    dist.pop(src)
    return dist


def _pg_path(twin: PgTwin, a: str, b: str, t: int, max_len: int) -> int | None:
    return twin.path_length(a, b, t, max_depth=max_len)


def closure_depth(twin: PgTwin, src: str, t: int, depth: int) -> set[str]:
    p = {"src": src, "t": t, "thr": OBSERVED_CALL_THRESHOLD, "maxd": depth}
    return {r[0] for r in twin.conn.execute(CLOSURE_DEPTH_SQL, p).fetchall()}


async def falkor_closure_depth(snapshot: str, src: str, depth: int) -> set[str]:
    rows = await _rows(snapshot, F_CLOSURE_DEPTH, {"src": src, "depth": depth})
    return set(rows[0][0]) - {src} if rows else set()


def pick_sources(
    services: list[str], adj: dict[str, list[str]], n: int, rng: random.Random
) -> list[tuple[str, dict[str, int]]]:
    """The ``n`` deepest of ``SAMPLE`` random sources (most services reached, then the
    longest farthest hop), each with its BFS distances."""
    cands = rng.sample(services, min(SAMPLE, len(services)))
    scored = [(s, bfs_hops(adj, s)) for s in cands]
    scored.sort(key=lambda x: (-len(x[1]), -max(x[1].values(), default=0), x[0]))
    return [x for x in scored[:n] if x[1]] or scored[:n]


async def compare_scale(
    snapshot: str,
    twin: PgTwin,
    rows: WorldRows,
    t: int,
    *,
    iterations: int = 50,
    seed: int = 0,
    depths: tuple[int, ...] = CLOSURE_DEPTHS,
) -> dict:
    """Agreement (untimed, once per input) then ``iterations`` timed runs per engine/op."""
    rng = random.Random(f"scale:{seed}")
    services = sorted(s[0] for s in rows.services)
    adj = impact_adjacency(rows, t)
    picked = pick_sources(services, adj, iterations, rng)
    srcs = [s for s, _ in picked]
    pairs = []
    for s, dist in picked:
        far = max(dist.values(), default=0)
        targets = sorted(x for x, d in dist.items() if d == far)
        pairs.append((s, rng.choice(targets) if targets else s, far))
    max_len = max([p[2] for p in pairs] + [pg_twin.MAX_PATH_DEPTH]) + 1

    ops: dict[str, dict] = {}
    for d in depths:
        agreed = ref_f = ref_p = 0
        sizes = []
        for s, dist in picked:
            truth = {x for x, h in dist.items() if h <= d}
            f = await falkor_closure_depth(snapshot, s, d)
            p = closure_depth(twin, s, t, d)
            agreed += f == p
            ref_f += f == truth
            ref_p += p == truth
            sizes.append(len(truth))
        tf, tp = [], []
        for s in srcs:
            tf.append((await _timed(falkor_closure_depth, snapshot, s, d))[1])
            tp.append((await _timed(closure_depth, twin, s, t, d))[1])
        ops[f"closure_d{d}"] = {
            "label": f"Upstream closure, depth {d} (algo.BFS vs recursive CTE)",
            "falkordb": _pct(tf),
            "postgres": _pct(tp),
            "checked": len(srcs),
            "agreed": agreed,
            "reference": {"falkordb": ref_f, "postgres": ref_p},
            "mean_size": round(sum(sizes) / len(sizes), 1) if sizes else None,
        }

    agreed = ref_f = ref_p = 0
    for a, b, far in pairs:
        f = await pg_twin.falkor_path_length(snapshot, a, b)
        p = _pg_path(twin, a, b, t, max_len)
        agreed += f == p
        ref_f += f == far
        ref_p += p == far
    tf, tp = [], []
    for a, b, _ in pairs:
        tf.append((await _timed(pg_twin.falkor_path_length, snapshot, a, b))[1])
        tp.append((await _timed(_pg_path, twin, a, b, t, max_len))[1])
    ops["path"] = {
        "label": "Shortest path to the farthest reachable service (shortestPath vs CTE)",
        "falkordb": _pct(tf),
        "postgres": _pct(tp),
        "checked": len(pairs),
        "agreed": agreed,
        "reference": {"falkordb": ref_f, "postgres": ref_p},
        "mean_size": round(sum(p[2] for p in pairs) / len(pairs), 2) if pairs else None,
        "max_hops": max((p[2] for p in pairs), default=None),
    }
    return {"t": t, "iterations": len(srcs), "ops": ops}


def depth_note(res: dict) -> str:
    """How deep the traversals actually went: the generated topology is tiered, so reach
    can saturate well before the depth bound. Stated so the depth-8 row is not oversold."""
    ops = res["ops"]
    path = ops.get("path", {})
    sizes = [
        (int(k.removeprefix("closure_d")), op.get("mean_size"))
        for k, op in ops.items()
        if k.startswith("closure_d")
    ]
    sizes.sort()
    seq = ", ".join(f"depth {d}: {s}" for d, s in sizes)
    deepest = max((d for d, _ in sizes), default=0)
    sat = next(
        (d for d, s in sizes if s == dict(sizes).get(deepest)),
        deepest,
    )
    tail = (
        f"; reach stops growing by depth {sat}, so deeper bounds add no work here"
        if sat < deepest
        else ""
    )
    return (
        f"Traversal depth actually reached: the farthest target of any sampled source is "
        f"{path.get('max_hops')} hops (mean {path.get('mean_size')}). Mean services reached "
        f"({seq}){tail}. The generator's topology is tiered, so this world is wide "
        "rather than deep."
    )


async def _drop_prefix(prefix: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(prefix):
            await db.select_graph(name).delete()


async def measure_scale(
    n_services: int,
    *,
    seed: int = 42,
    iterations: int = 50,
    dsn: str | None = None,
    log=print,
) -> dict:
    """Generate, load (FalkorDB + Postgres), compare, and always clean up."""
    from ic.bench.runner import git_state

    tag = f"n{n_services}-{secrets.token_hex(3)}"
    prefix = naming.bench("scale", tag, "world").rsplit(":", 1)[0] + ":"
    world_g = naming.bench("scale", tag, "world")
    snap = naming.bench("scale", tag, "asof-true")
    schema = f"twin_scale_{tag.replace('-', '_')}"
    timings: dict[str, float] = {}
    commit, dirty = git_state()
    base = {
        "n_services_requested": n_services,
        "seed": seed,
        "closure_depth": max(CLOSURE_DEPTHS),
        "closure_depths": list(CLOSURE_DEPTHS),
        "world": f"generate_world({seed}, n_services={n_services})",
        "commit": commit,
        "dirty": dirty,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def lap(key: str, t0: float) -> None:
        timings[key] = round(time.perf_counter() - t0, 1)
        log(f"scale: {key} {timings[key]} s")

    twin: PgTwin | None = None
    try:
        t0 = time.perf_counter()
        world = await asyncio.to_thread(generate_world, seed, n_services)
        lap("generate", t0)
        t0 = time.perf_counter()
        await load_world(world, world_g)
        del world
        lap("load_falkordb", t0)
        t0 = time.perf_counter()
        await build_view(world_g, snap, WORLD_NOW, "true")
        lap("snapshot", t0)
        t0 = time.perf_counter()
        rows = await pg_twin.read_world(world_g)
        lap("read_rows", t0)
        try:
            twin = PgTwin(dsn or get_settings().postgres_dsn, schema)
        except Exception as e:
            log(f"scale skipped: {e}")
            return {**base, "error": f"Postgres unavailable: {type(e).__name__}"}
        t0 = time.perf_counter()
        twin.load(rows)
        lap("load_postgres", t0)
        res = await compare_scale(snap, twin, rows, WORLD_NOW, iterations=iterations, seed=seed)
        res["counts"] = twin.counts()
        snap_rows = await _rows(snap, "MATCH (:Service)-[e:IMPACTS]->(:Service) RETURN count(e)")
        res["counts"]["impacts_service_edges"] = snap_rows[0][0] if snap_rows else 0
        for key, op in res["ops"].items():
            log(
                f"scale {key}: falkordb p50 {op['falkordb']['p50_ms']} ms "
                f"p95 {op['falkordb']['p95_ms']} ms; postgres p50 {op['postgres']['p50_ms']} "
                f"ms p95 {op['postgres']['p95_ms']} ms; agree {op['agreed']}/{op['checked']}"
            )
        notes = [
            f"Sources: the {res['iterations']} deepest of {SAMPLE} randomly drawn services "
            "(by services reached at the snapshot time); path targets are the farthest "
            "service each source reaches.",
            "Agree = FalkorDB and Postgres return the same set (closure) or hop count (path) "
            "for the same input; both are also checked against an in-memory BFS over the "
            "same edges (reference in scale.json).",
            "FalkorDB queries a materialized as-of snapshot (IMPACTS edges built ahead of "
            "time, snapshot build time listed separately); Postgres applies the valid-time "
            "filter inside each recursive query. Same host, warm cache, one client.",
            "Top-k precedent search is not repeated: postmortems do not grow with service "
            f"count ({res['counts'].get('postmortems', 0)} in this world).",
        ]
        notes.append(depth_note(res))
        return {**base, **res, "timings_s": timings, "notes": notes}
    finally:
        if twin is not None:
            twin.drop()
        await _drop_prefix(prefix)


def merge_into_report(out: str | Path, scale: dict) -> tuple[Path, Path] | None:
    """Write ``scale.json`` and fold it into an existing report.json / report.md."""
    from ic.bench import report as rpt

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / SCALE_FILE).write_text(json.dumps(scale, indent=1, sort_keys=True) + "\n")
    jp = out / "report.json"
    if not jp.exists():
        return None
    report = json.loads(jp.read_text())
    report["scale"] = scale
    return rpt.write_report(out, report)


def existing_scale(out: str | Path | None) -> dict | None:
    if out is None:
        return None
    p = Path(out) / SCALE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def main(args) -> None:
    try:
        res = asyncio.run(measure_scale(args.scale, seed=args.seed, iterations=args.latency_iters))
    finally:
        client.reset_client()
    written = merge_into_report(args.out, res)
    if written:
        print(f"wrote {written[0]} and {written[1]} (scale section)")
    else:
        print(f"wrote {Path(args.out) / SCALE_FILE} (no report.json yet to merge into)")
