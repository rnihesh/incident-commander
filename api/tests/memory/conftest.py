"""Fixtures for the memory tests (helpers live in memory_world)."""

from __future__ import annotations

import pytest
from memory_world import Coupled, drop_session, edge, rows

from ic.bench.learning_curve import FAULT_CLASS, LearningSession, _settings, probe
from ic.graph import client
from ic.store import Store
from ic.world.faults import InjectionError, inject_fault


@pytest.fixture(scope="module")
async def coupled(tmp_path_factory) -> Coupled:
    client.reset_client()
    tmp = tmp_path_factory.mktemp("memory")
    store, probe_store = Store(tmp / "ic.sqlite3"), Store(tmp / "probe.sqlite3")
    sess = LearningSession(store, _settings(tmp / "ic.sqlite3"), None)
    made: list[str] = []
    c: Coupled | None = None
    try:
        for s0 in range(42000, 42010):
            sid = await sess.open()
            made.append(sid)
            try:
                f = await inject_fault(
                    sess.world, session_id=sid, fault_class=FAULT_CLASS, rng_seed=s0, store=store
                )
            except InjectionError:
                continue
            row = await sess.run(f)
            new = [e for e in row["learned"] if e["action"] == "created"]
            if row["correct"] and new:
                c = Coupled(sess, sid, sess.world, new[0]["caller"], new[0]["callee"])
                c.faults.append(f)
                c.rows.append(row)
                break
        assert c is not None, "no seed taught a coupling"
        c.edge_after_first = await edge(c.world, c.caller, c.callee)
        c.root_after_first = dict(
            (
                await rows(
                    c.world,
                    "MATCH (r:Change {id: $id}) RETURN properties(r)",
                    {"id": c.faults[0].answer.root_id},
                )
            )[0][0]
        )
        for k in range(60):
            ok, _ = await probe(
                c.world, c.sid, c.callee, 7000 + k, {c.caller, c.callee}, probe_store
            )
            if ok:
                f2 = await inject_fault(
                    c.world,
                    session_id=c.sid,
                    fault_class=FAULT_CLASS,
                    rng_seed=7000 + k,
                    target_service=c.callee,
                    store=store,
                )
                c.faults.append(f2)
                c.rows.append(await sess.run(f2))
                break
        assert len(c.rows) == 2, "no second fault routes through the coupling"
        yield c
    finally:
        await sess.deps.runs.wait_all(limit_s=60)
        for sid in made:
            await drop_session(sid)
        client.reset_client()
