import asyncio
import time
from types import SimpleNamespace

import pytest

from ic.graph import client, naming

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset():
    client.reset_client()
    yield
    client.reset_client()


async def test_ping_false_quickly_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        client, "get_settings", lambda: SimpleNamespace(falkordb_url="falkor://127.0.0.1:1")
    )
    t = time.monotonic()
    assert await client.ping() is False
    assert time.monotonic() - t < 3.8


async def test_ping_bounded_when_server_hangs(monkeypatch):
    class Hang:
        async def list_graphs(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(client, "_db", Hang())
    monkeypatch.setattr(client, "PING_TIMEOUT_S", 0.2)
    t = time.monotonic()
    assert await client.ping() is False
    assert time.monotonic() - t < 2


async def test_client_has_timeouts(monkeypatch):
    seen = {}

    class Fake:
        @staticmethod
        def from_url(url, **kw):
            seen.update(kw)
            return object()

    monkeypatch.setattr(client, "FalkorDB", Fake)
    client.get_db()
    assert seen == {
        "socket_timeout": 70,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
        "socket_keepalive": True,
    }


# --- query X-ray: EXPLAIN / PROFILE as a plain operator tree (U4) -------------------------

VAR_LEN = (
    "MATCH p=(u:Service)-[:IMPACTS*1..4]->(s:Service {id: $sid}) "
    "WHERE all(r IN relationships(p) WHERE r.vf < $t) RETURN DISTINCT u.id"
)


@pytest.fixture
async def chain_graph():
    name = naming.session_world(naming.new_session_id())
    await client.query(
        name, "UNWIND range(1, 30) AS i CREATE (:Service {id: 'svc-' + toString(i), n: i})"
    )
    await client.query(
        name,
        "MATCH (a:Service), (b:Service) WHERE a.n + 1 = b.n CREATE (a)-[:IMPACTS {vf: a.n}]->(b)",
    )
    yield name
    await client.delete_graph(name)


def _walk(node):
    yield node
    for c in node["children"]:
        yield from _walk(c)


def _above(tree, upper: str, lower: str) -> bool:
    """True when some ``upper`` operator has a ``lower`` operator beneath it."""
    return any(
        any(d["name"].startswith(lower) for d in _walk(n) if d is not n)
        for n in _walk(tree)
        if n["name"] == upper
    )


async def test_explain_returns_a_plain_operator_tree(chain_graph):
    tree = await client.explain(chain_graph, VAR_LEN, {"sid": "svc-5", "t": 100})
    assert set(tree) == {"name", "args", "records", "ms", "children"}
    names = [n["name"] for n in _walk(tree)]
    assert "Conditional Variable Length Traverse" in names
    traverse = next(n for n in _walk(tree) if n["name"].startswith("Conditional Variable"))
    assert "IMPACTS*1..4" in traverse["args"]
    # EXPLAIN plans only: no runtime numbers.
    assert all(n["records"] is None and n["ms"] is None for n in _walk(tree))
    # The R7 claim: the path predicate is a Filter above the traversal, not inside it.
    assert _above(tree, "Filter", "Conditional Variable Length Traverse")


async def test_profile_returns_records_and_time_per_operator(chain_graph):
    tree = await client.profile(chain_graph, VAR_LEN, {"sid": "svc-5", "t": 100})
    ops = list(_walk(tree))
    assert all(isinstance(n["records"], int) and isinstance(n["ms"], float) for n in ops)
    assert tree["records"] == 4  # svc-1..svc-4 reach svc-5 within 4 hops
    scan = next(n for n in ops if "Scan" in n["name"])
    assert scan["records"] >= 1


async def test_profile_refuses_a_write_query(chain_graph):
    with pytest.raises(ValueError, match="read-only"):
        await client.profile(chain_graph, "MATCH (s:Service) SET s.x = 1 RETURN count(s)")
    q = "MATCH (s:Service) WHERE s.x = 1 RETURN count(s)"
    assert (await client.query(chain_graph, q)).result_set[0][0] == 0


@pytest.mark.parametrize(
    "cypher",
    [
        "DROP INDEX FOR (n:Service) ON (n.id)",
        "CREATE INDEX FOR (n:Service) ON (n.n)",
        "CALL db.idx.fulltext.drop('Service')",
        "LOAD CSV FROM 'file://x.csv' AS r RETURN r",
        "MATCH (s:Service) FOREACH (x IN [1] | SET s.p = x)",
        "MATCH (s:Service) MERGE (s)-[:X]->(:Y) RETURN 1",
    ],
)
async def test_profile_refuses_schema_writes_and_unknown_operators(chain_graph, cypher):
    """An allowlist of read operators: index DDL, writing procedures and anything the guard
    does not know are refused before GRAPH.PROFILE runs them."""
    with pytest.raises(ValueError, match="read-only"):
        await client.profile(chain_graph, cypher)
    idx = await client.query(chain_graph, "CALL db.indexes() YIELD label RETURN count(label)")
    assert idx.result_set[0][0] == 0  # no index was created or dropped


async def test_profile_allows_read_only_procedures(chain_graph):
    q = "MATCH (s:Service {id: 'svc-1'}) CALL algo.BFS(s, 3, 'IMPACTS') YIELD nodes RETURN nodes"
    tree = await client.profile(chain_graph, q)
    assert any(n["name"] == "ProcedureCall" for n in _walk(tree))


async def test_profile_reuses_a_precomputed_plan(chain_graph, monkeypatch):
    planned = await client.explain(chain_graph, VAR_LEN, {"sid": "svc-5", "t": 100})
    cmds: list[str] = []
    real = client._plan

    async def counting(cmd, *a, **kw):
        cmds.append(cmd)
        return await real(cmd, *a, **kw)

    monkeypatch.setattr(client, "_plan", counting)
    tree = await client.profile(chain_graph, VAR_LEN, {"sid": "svc-5", "t": 100}, planned=planned)
    assert cmds == ["GRAPH.PROFILE"] and tree["records"] == 4
    write = await client.explain(chain_graph, "MATCH (s:Service) SET s.x = 1")
    with pytest.raises(ValueError, match="read-only"):
        await client.profile(chain_graph, "MATCH (s:Service) SET s.x = 1", planned=write)


async def test_profile_server_side_timeout_raises_query_timeout(chain_graph):
    slow = "UNWIND range(1, 100000000) AS x WITH x WHERE x < 0 RETURN count(x)"
    t = time.monotonic()
    with pytest.raises(client.QueryTimeout):
        await client.profile(chain_graph, slow, timeout_ms=50)
    assert time.monotonic() - t < 5


async def test_plan_bounded_client_side_when_server_hangs(monkeypatch):
    class Graph:
        async def execute_command(self, *a):
            await asyncio.sleep(60)

        def _build_params_header(self, params):
            return ""

    class Hang:
        def select_graph(self, name):
            return Graph()

    monkeypatch.setattr(client, "_db", Hang())
    monkeypatch.setattr(client, "PLAN_CLIENT_SLACK_S", 0.1)
    t = time.monotonic()
    with pytest.raises(client.QueryTimeout):
        await client.explain("s:x:world", "RETURN 1", timeout_ms=100)
    assert time.monotonic() - t < 2


async def test_plan_tree_parses_pipes_in_args_and_nesting():
    lines = [
        "Results | Records produced: 2, Execution time: 0.010000 ms",
        "    Project | Records produced: 2, Execution time: 0.020000 ms",
        "        Filter | [x IN l | x] | Records produced: 2, Execution time: 0.5 ms",
        "            Node By Label Scan | (a:S) | Records produced: 9, Execution time: 1.2 ms",
        "        Argument",
    ]
    tree = client.plan_tree(lines)
    assert tree["name"] == "Results" and tree["records"] == 2 and tree["ms"] == 0.01
    (proj,) = tree["children"]
    filt, arg = proj["children"]
    assert filt["args"] == "[x IN l | x]" and filt["ms"] == 0.5
    assert filt["children"][0]["args"] == "(a:S)" and filt["children"][0]["records"] == 9
    assert filt["children"][0]["ms"] == 1.2
    assert arg == {"name": "Argument", "args": None, "records": None, "ms": None, "children": []}


async def test_dropped_connection_retry_is_safe():
    """A dead pooled socket is retried once when the command never reached the server, or
    when the command is idempotent; a write that may have run is never sent twice."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    def flaky(msg):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RedisConnectionError(msg)
            return "ok"

        return fn, calls

    not_sent = "Error UNKNOWN while writing to socket. Connection lost."
    fn, calls = flaky(not_sent)
    assert await client._call(fn, idempotent=False) == "ok" and len(calls) == 2

    fn, calls = flaky("Connection closed by server.")
    assert await client._call(fn, idempotent=True) == "ok" and len(calls) == 2

    fn, calls = flaky("Connection closed by server.")
    with pytest.raises(RedisConnectionError):
        await client._call(fn, idempotent=False)
    assert len(calls) == 1
