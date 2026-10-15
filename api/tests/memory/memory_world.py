"""Helpers for the memory tests: a two-incident sequence in ONE session world.

Incident 1 is an undeclared-coupling fault investigated end to end (deterministic triage,
CLEARED rehearsal, mocked PR and page, write-back). Incident 2 is another undeclared-coupling
fault rooted on the same callee whose symptoms include both ends of the learned coupling
(probed on a scratch copy first, exactly as the learning-curve runner does)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ic.bench.learning_curve import LearningSession
from ic.graph import client, naming


@dataclass
class Coupled:
    sess: LearningSession
    sid: str
    world: str
    caller: str
    callee: str
    faults: list = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    edge_after_first: dict = field(default_factory=dict)
    root_after_first: dict = field(default_factory=dict)


async def drop_session(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith((f"s:{sid}:", f"pr:{sid}:")):
            await db.select_graph(name).delete()


async def rows(g: str, q: str, params: dict | None = None) -> list[list]:
    return (await client.query(g, q, params, read_only=True)).result_set


async def edge(world: str, caller: str, callee: str) -> dict:
    rs = await rows(
        world,
        "MATCH (:Service {id: $a})-[l:LEARNED_DEPENDS_ON]->(:Service {id: $b}) "
        "RETURN properties(l)",
        {"a": caller, "b": callee},
    )
    return dict(rs[0][0]) if rs else {}


def ledger_of(c: Coupled) -> str:
    return naming.ledger(c.sid)
