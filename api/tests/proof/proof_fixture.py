"""A completed fixture incident: bad_deploy injected into a fresh session world, as-of
snapshots at alert time, and the proof chain an investigation would leave behind."""

from __future__ import annotations

from dataclasses import dataclass, field

from ic.graph import client, naming
from ic.graph.snapshots import SnapshotHandles, build_snapshots, create_session_world
from ic.proof import ledger
from ic.store import Store
from ic.tools.registry import ToolContext, call_tool
from ic.world.faults import inject_fault

SEED = 3


@dataclass
class Incident:
    sid: str
    world: str
    iid: str
    t: int
    root: str
    root_service: str
    decoys: list[str]
    ctx: ToolContext
    store: Store
    handles: SnapshotHandles
    proofs: list[ledger.ProofRecord] = field(default_factory=list)


async def rows(g: str, q: str, params: dict | None = None) -> list[list]:
    return (await client.query(g, q, params, read_only=True)).result_set


async def drop_session(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


async def setup_incident(sid: str, store: Store, *, run: bool = True) -> Incident:
    world = await create_session_world(sid)
    f = await inject_fault(
        world, session_id=sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    iid, t, root = f.incident_id, f.alert_time, f.answer.root_id
    h = await build_snapshots(world, sid, iid, t)
    ctx = ToolContext(
        sid=sid,
        iid=iid,
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        ledger_graph=naming.ledger(sid),
    )
    funnel = await call_tool("screen_candidates", {"incident_id": iid}, ctx)
    decoys = sorted(v["id"] for v in funnel.result["verdicts"] if not v["ok"] and v["id"] != root)
    assert len(decoys) >= 2, funnel.result["verdicts"]
    root_service = (
        await rows(world, "MATCH (:Change {id: $id})-[:CHANGES]->(s) RETURN s.id", {"id": root})
    )[0][0]
    inc = Incident(sid, world, iid, t, root, root_service, decoys[:2], ctx, store, h)
    await ledger.open_incident(sid, iid, t, world)
    if run:
        for kind in ("suspect_qualifies", "suspect_necessary", "no_other_qualifying"):
            inc.proofs.append(await ledger.test_hypothesis(ctx, kind, root, f"{kind} {root}"))
        for d in inc.decoys:
            inc.proofs.append(await ledger.test_hypothesis(ctx, "entity_refuted", d, f"not {d}"))
        await ledger.anchor(sid, iid, store)
    return inc
