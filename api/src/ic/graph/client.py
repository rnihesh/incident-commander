"""Async FalkorDB access shared by the whole API."""

import asyncio
import re

from falkordb.asyncio import FalkorDB
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from ic.config import get_settings

PING_TIMEOUT_S = 3.0
_db: FalkorDB | None = None


def get_db() -> FalkorDB:
    global _db
    if _db is None:
        _db = FalkorDB.from_url(
            get_settings().falkordb_url,
            socket_timeout=70,  # above the 60s server-side query ceiling
            socket_connect_timeout=5,
            health_check_interval=30,
            socket_keepalive=True,  # idle pooled sockets get dropped less often (see _call)
        )
    return _db


async def _ping() -> None:
    db = _db if _db is not None else await asyncio.to_thread(get_db)
    await db.list_graphs()


async def ping() -> bool:
    try:
        # get_db() may connect synchronously on first use; keep it off the event loop.
        await asyncio.wait_for(_ping(), PING_TIMEOUT_S)
        return True
    except Exception:
        return False


# redis-py's message when a pooled socket was already dead: the command never left the client.
_NOT_SENT = "while writing to socket"


async def _call(fn, *, idempotent: bool):
    """Run ``fn`` once more after a dropped connection when that is safe.

    Docker for Mac's network layer drops idle pooled sockets; the pool discards a failed
    connection, so the retry reconnects. Reads (and other idempotent commands) are retried on
    any connection error. A write is retried only when it failed before reaching the server,
    so it can never apply twice. Timeouts are not retried.
    """
    try:
        return await fn()
    except RedisConnectionError as e:
        if not (idempotent or _NOT_SENT in str(e)):
            raise
        return await fn()


def reset_client() -> None:
    """Drop the cached connection (tests: rebind to the current event loop)."""
    global _db
    _db = None


async def query(
    graph_name: str,
    cypher: str,
    params: dict | None = None,
    *,
    read_only: bool = False,
    timeout_ms: int = 10000,
):
    g = get_db().select_graph(graph_name)
    run = g.ro_query if read_only else g.query
    return await _call(lambda: run(cypher, params, timeout=timeout_ms), idempotent=read_only)


PLAN_TIMEOUT_MS = 5000
PLAN_CLIENT_SLACK_S = 2.0  # client-side bound above the server-side TIMEOUT
_STATS = re.compile(r"Records produced: (\d+), Execution time: ([\d.]+) ms")
# PROFILE runs the query, so only plans made of known read operators are profiled; anything
# else (Commit/Create/Delete/Update/Merge, ForEach, Load CSV, index DDL such as Drop Index,
# operators this list has not seen) is refused. Prefixes cover the scan/traverse families.
_READ_OPS = frozenset(
    {
        "Results",
        "Project",
        "Aggregate",
        "Sort",
        "Top",
        "Skip",
        "Limit",
        "Distinct",
        "Filter",
        "Optional",
        "Apply",
        "Semi Apply",
        "Anti Semi Apply",
        "Or Apply Multiplexer",
        "And Apply Multiplexer",
        "Argument",
        "Argument List",
        "Unwind",
        "Cartesian Product",
        "Value Hash Join",
        "Union",
        "Expand Into",
        "ProcedureCall",  # only for the read-only procedures below (checked on the text)
    }
)
_READ_PREFIXES = (
    "All Node Scan",
    "Node By ",
    "Edge By ",
    "Conditional Traverse",
    "Conditional Variable Length Traverse",
    "Optional Conditional Traverse",
)
# The plan does not name the procedure a ProcedureCall runs, so it is read off the query.
_PROCEDURE = re.compile(r"\bCALL\s+([A-Za-z_][\w.]*)\s*\(", re.IGNORECASE)
_READ_PROCEDURES = frozenset(
    {
        "algo.bfs",
        "algo.sppaths",
        "algo.sspaths",
        "algo.wcc",
        "algo.betweenness",
        "db.idx.vector.querynodes",
        "db.idx.fulltext.querynodes",
        "db.labels",
        "db.relationshiptypes",
        "db.propertykeys",
        "db.indexes",
    }
)


class QueryTimeout(TimeoutError):
    """EXPLAIN/PROFILE did not answer within its bound (server TIMEOUT or client wait)."""


def plan_tree(lines: list[str | bytes]) -> dict:
    """GRAPH.EXPLAIN / GRAPH.PROFILE output as a plain tree: ``{name, args, records, ms,
    children}``, with ``records``/``ms`` None for EXPLAIN. Each line is one operator,
    indented four spaces per level, ``Name | args | Records produced: n, Execution time:
    x ms``; args may themselves contain ``|`` (list comprehensions), so only the first and
    a trailing stats segment are split off."""
    root: dict | None = None
    stack: list[tuple[int, dict]] = []
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else raw
        level = (len(line) - len(line.lstrip(" "))) // 4
        parts = [p.strip() for p in line.strip().split(" | ")]
        records = ms = None
        m = _STATS.fullmatch(parts[-1]) if len(parts) > 1 else None
        if m:
            records, ms = int(m.group(1)), float(m.group(2))
            parts.pop()
        node = {
            "name": parts[0],
            "args": " | ".join(parts[1:]) or None,
            "records": records,
            "ms": ms,
            "children": [],
        }
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        elif root is None:
            root = node
        else:
            raise ValueError("execution plan has more than one root")
        stack.append((level, node))
    if root is None:
        raise ValueError("empty execution plan")
    return root


def _ops(tree: dict):
    yield tree
    for c in tree["children"]:
        yield from _ops(c)


async def _plan(cmd: str, graph_name: str, cypher: str, params: dict | None, timeout_ms: int):
    g = get_db().select_graph(graph_name)
    q = g._build_params_header(params) + cypher
    try:
        raw = await asyncio.wait_for(
            _call(
                lambda: g.execute_command(cmd, graph_name, q, "timeout", timeout_ms),
                idempotent=True,  # EXPLAIN, or PROFILE of a plan already checked read-only
            ),
            timeout_ms / 1000 + PLAN_CLIENT_SLACK_S,
        )
    except TimeoutError as e:
        raise QueryTimeout(f"{cmd} did not answer within {timeout_ms} ms") from e
    except ResponseError as e:
        if "timed out" in str(e).lower():
            raise QueryTimeout(f"{cmd} timed out after {timeout_ms} ms") from e
        raise
    return plan_tree(raw)


async def explain(
    graph_name: str, cypher: str, params: dict | None = None, *, timeout_ms: int = PLAN_TIMEOUT_MS
) -> dict:
    """FalkorDB's execution plan for ``cypher`` (GRAPH.EXPLAIN: planned, not run)."""
    return await _plan("GRAPH.EXPLAIN", graph_name, cypher, params, timeout_ms)


def _not_read_only(planned: dict, cypher: str) -> list[str]:
    """Operators (and procedures) of the plan that are not known to be read-only."""
    bad = {
        o["name"]
        for o in _ops(planned)
        if o["name"] not in _READ_OPS and not o["name"].startswith(_READ_PREFIXES)
    }
    if any(o["name"] == "ProcedureCall" for o in _ops(planned)):
        procs = {p.lower() for p in _PROCEDURE.findall(cypher)}
        bad |= {f"CALL {p}" for p in procs - _READ_PROCEDURES}
        if not procs:
            bad.add("ProcedureCall")  # a call the text check cannot name
    return sorted(bad)


async def profile(
    graph_name: str,
    cypher: str,
    params: dict | None = None,
    *,
    timeout_ms: int = PLAN_TIMEOUT_MS,
    planned: dict | None = None,
) -> dict:
    """The plan with per-operator records and time (GRAPH.PROFILE runs the query). There is
    no read-only PROFILE, so the plan is checked first and anything but known read
    operators is refused. ``planned`` is this query's EXPLAIN tree when the caller already
    has it (saves a round trip)."""
    if planned is None:
        planned = await explain(graph_name, cypher, params, timeout_ms=timeout_ms)
    refused = _not_read_only(planned, cypher)
    if refused:
        raise ValueError(f"PROFILE is read-only here; the plan has {', '.join(refused)}")
    return await _plan("GRAPH.PROFILE", graph_name, cypher, params, timeout_ms)


async def graph_exists(name: str) -> bool:
    return name in await _call(lambda: get_db().list_graphs(), idempotent=True)


async def delete_graph(name: str) -> None:
    if await graph_exists(name):
        await _call(lambda: get_db().select_graph(name).delete(), idempotent=False)


async def copy_graph(src: str, dst: str) -> None:
    """Replace ``dst`` with a copy of ``src``."""
    await delete_graph(dst)
    await _call(lambda: get_db().select_graph(src).copy(dst), idempotent=False)
