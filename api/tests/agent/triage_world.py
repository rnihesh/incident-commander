"""Session worlds for the triage tests.

A *base* is built once per module: seed:world copied into a session, a bad_deploy fault
injected, as-of snapshots built at alert time. Each test then *clones* the base into a fresh
session (three GRAPH.COPY calls), so every run gets its own empty ledger without paying for
injection and snapshot builds again.
"""

from __future__ import annotations

from dataclasses import dataclass

from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.graph.snapshots import build_snapshots, create_session_world
from ic.store import Store
from ic.tools.registry import ToolContext
from ic.world.faults import inject_fault

SEED = 3


@dataclass
class Base:
    sid: str
    iid: str
    t: int
    root: str
    root_service: str
    alert_service: str
    store: Store
    second: str | None = None  # a second qualifying change (AE1 world)


@dataclass
class Clone:
    sid: str
    base: Base
    ctx: ToolContext


async def rows(g: str, q: str, params: dict | None = None) -> list[list]:
    return (await client.query(g, q, params, read_only=True)).result_set


async def drop_session(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


async def build_base(store: Store, *, second_qualifying: bool = False) -> Base:
    sid = naming.new_session_id()
    world = await create_session_world(sid)
    f = await inject_fault(
        world, session_id=sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    answer = store.get_answer(f.incident_id, sid)
    assert answer is not None
    root = answer["root_id"]
    vf, svc = (
        await rows(
            world,
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) RETURN c.valid_from, s.id",
            {"id": root},
        )
    )[0]
    second = None
    if second_qualifying:
        # AE1: a second deploy on the same (earliest-failing) service, also before onset.
        second = "chg-99001"
        await client.query(
            world,
            "MATCH (s:Service {id: $svc}) "
            "CREATE (c:Change {id: $id, kind: 'deploy', service_id: $svc, "
            "summary: 'Deploy hotfix: connection pool tuning', valid_from: $vf, "
            "valid_to: $vt, recorded_at: $rec, value: 'v9.9.9', prev_value: null}) "
            "CREATE (c)-[:CHANGES]->(s)",
            {"svc": svc, "id": second, "vf": vf + 30, "vt": OPEN_END, "rec": vf + 40},
        )
    await build_snapshots(world, sid, f.incident_id, f.alert_time)
    return Base(sid, f.incident_id, f.alert_time, root, svc, f.alert_service_id, store, second)


async def clone(base: Base) -> Clone:
    sid = naming.new_session_id()
    pairs = [(naming.session_world(base.sid), naming.session_world(sid))] + [
        (naming.asof(base.sid, base.iid, v), naming.asof(sid, base.iid, v)) for v in naming.VIEWS
    ]
    for src, dst in pairs:
        await client.copy_graph(src, dst)
    ctx = ToolContext(
        sid=sid,
        iid=base.iid,
        world_graph=naming.session_world(sid),
        snapshot_true=naming.asof(sid, base.iid, "true"),
        snapshot_known=naming.asof(sid, base.iid, "known"),
        ledger_graph=naming.ledger(sid),
    )
    return Clone(sid, base, ctx)
