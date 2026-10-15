"""Fault injection (U5): oracle agreement on real snapshots, isolation, no answer in the
graph, and the KTD12 leakage test."""

from __future__ import annotations

import pytest

from ic.graph import client, naming
from ic.graph.schema import WORLD_NOW
from ic.graph.snapshots import build_snapshots, create_session_world, delete_snapshots
from ic.store import Store
from ic.world.causality import necessity, qualifies
from ic.world.faults import TRAINING_CLASSES, inject_fault
from ic.world.impact_reader import read_impact_graph, read_symptoms, read_window_entities

CLASSES = sorted(TRAINING_CLASSES)


async def rows(g, q, params=None):
    return (await client.query(g, q, params, read_only=True)).result_set


@pytest.fixture
async def sid():
    client.reset_client()
    s = naming.new_session_id()
    yield s
    db = client.get_db()
    for n in await db.list_graphs():
        if n.startswith(f"s:{s}:"):
            await db.select_graph(n).delete()
    client.reset_client()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ic.sqlite3")


async def counts(g) -> tuple[int, int]:
    n = (await rows(g, "MATCH (n) RETURN count(n)"))[0][0]
    e = (await rows(g, "MATCH ()-[e]->() RETURN count(e)"))[0][0]
    return n, e


@pytest.mark.parametrize("fault_class", CLASSES)
async def test_root_qualifies_and_every_other_window_entity_fails(sid, store, fault_class):
    world = await create_session_world(sid)
    for seed in (1, 2, 3):  # default alert times chain forward, so windows never overlap
        f = await inject_fault(
            world, session_id=sid, fault_class=fault_class, rng_seed=seed, store=store
        )
        assert f.alert_time > WORLD_NOW
        root_id = f.answer.root_id
        handles = await build_snapshots(world, sid, f.incident_id, f.alert_time)
        try:
            for view, snap in (("true", handles.true_graph), ("known", handles.known_graph)):
                g = await read_impact_graph(snap)
                symptoms = await read_symptoms(snap, f.incident_id)
                assert 3 <= len(symptoms) <= 6
                assert all(s.onset <= f.alert_time for s in symptoms)
                earliest = min(s.onset for s in symptoms)
                first = min(symptoms, key=lambda s: s.onset)
                last = max(symptoms, key=lambda s: (s.onset, s.service_id))
                # The alert fires on the last (customer-facing) service to fail, not the root.
                assert last.service_id == f.alert_service_id
                assert first.service_id != f.alert_service_id
                ents = await read_window_entities(snap, earliest)
                by_id = {e.id: e for e in ents}
                assert root_id in by_id, view
                ok, clause, detail = qualifies(by_id[root_id], g, symptoms)
                assert ok, f"{view}: root fails {clause}: {detail}"
                others = [e for e in ents if e.id != root_id]
                if view == "true":
                    assert len(others) >= 3  # decoys are present
                for e in others:
                    ok, clause, _ = qualifies(e, g, symptoms)
                    assert not ok, f"{view}: {e.id} qualifies ({e.service_id}, {e.sig_kind})"
                    assert clause is not None
                assert necessity(by_id[root_id], ents, g, symptoms)[0]
            rec = await rows(world, "MATCH (c:Change {id: $id}) RETURN c.kind", {"id": root_id})
            assert rec == [[TRAINING_CLASSES[fault_class].value]]
        finally:
            await delete_snapshots(handles)


async def test_decoys_cover_several_failure_clauses(sid, store):
    world = await create_session_world(sid)
    seen: set[str] = set()
    for seed in range(6):
        f = await inject_fault(
            world, session_id=sid, fault_class=CLASSES[seed % 3], rng_seed=seed, store=store
        )
        h = await build_snapshots(world, sid, f.incident_id, f.alert_time)
        try:
            g = await read_impact_graph(h.true_graph)
            syms = await read_symptoms(h.true_graph, f.incident_id)
            ents = await read_window_entities(h.true_graph, min(s.onset for s in syms))
            seen |= {qualifies(e, g, syms)[1] for e in ents if e.id != f.answer.root_id}
        finally:
            await delete_snapshots(h)
    assert {"origin", "signature"} <= seen, seen


async def test_injection_leaves_seed_and_other_sessions_untouched(sid, store):
    other_sid = naming.new_session_id()
    try:
        other = await create_session_world(other_sid)
        world = await create_session_world(sid)
        before = (await counts(naming.seed_world()), await counts(other), await counts(world))
        await inject_fault(world, session_id=sid, fault_class="bad_deploy", rng_seed=7, store=store)
        after = (await counts(naming.seed_world()), await counts(other), await counts(world))
        assert after[0] == before[0]
        assert after[1] == before[1]
        assert after[2] != before[2]
    finally:
        await client.delete_graph(naming.session_world(other_sid))


async def test_answer_is_only_in_the_store(sid, store):
    world = await create_session_world(sid)
    f = await inject_fault(world, session_id=sid, fault_class="flag_flip", rng_seed=4, store=store)
    nodes = await rows(world, "MATCH (n) RETURN properties(n)")
    edges = await rows(world, "MATCH ()-[e]->() RETURN properties(e)")
    blob = repr(nodes) + repr(edges)
    assert f.answer.nonce not in blob
    assert f.answer.fault_id not in blob
    caused = await rows(
        world, "MATCH (:Incident {id: $i})-[e:CAUSED_BY]->() RETURN count(e)", {"i": f.incident_id}
    )
    assert caused[0][0] == 0
    status = await rows(world, "MATCH (i:Incident {id: $i}) RETURN i.status", {"i": f.incident_id})
    assert status == [["open"]]
    row = store.get_answer(f.incident_id, sid)
    assert row["root_id"] == f.answer.root_id
    assert row["nonce"] == f.answer.nonce
    assert row["fault_class"] == "flag_flip"
    assert f.answer.nonce not in repr(f)


async def test_same_seed_is_deterministic(sid, store):
    outs = []
    for _ in range(2):
        s = naming.new_session_id()
        try:
            w = await create_session_world(s)
            f = await inject_fault(
                w, session_id=s, fault_class="config_change", rng_seed=11, store=store
            )
            changes = await rows(
                w,
                "MATCH (c:Change) WHERE c.valid_from > $t RETURN c.id, c.summary, c.valid_from "
                "ORDER BY c.id",
                {"t": WORLD_NOW},
            )
            outs.append((f.incident_id, f.alert_service_id, f.alert_time, changes))
        finally:
            await client.delete_graph(naming.session_world(s))
    assert outs[0] == outs[1]


# --------------------------------------------------------------------------- leakage (KTD12)

N_INJECTIONS = 200
MARGIN = 0.15


async def _candidates(world: str, alert: int):
    """Every Change in the window up to the alert, with the features the probes use."""
    return await rows(
        world,
        "MATCH (p:Person)-[:AUTHORED]->(c:Change)-[:CHANGES]->(s:Service) "
        "WHERE c.valid_from >= $lo AND c.valid_from <= $hi "
        "OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)-[:OWNS]->(s) "
        "RETURN c.id, c.recorded_at - c.valid_from, size(c.summary), id(c), c.valid_from, "
        "c.kind, t IS NOT NULL, s.id",
        {"lo": alert - 150 * 60, "hi": alert},
    )


async def test_leakage_single_feature_probes_stay_near_chance(sid, store):
    """Each probe picks the candidate with the max (or min) of one feature that does not
    need the symptoms; ties count fractionally (expected accuracy). Probes:

      delay_*       recorded_at - valid_from
      summary_*     length of the summary text
      id_*          change id rank (ids follow valid_from order)
      insertion_*   FalkorDB internal node id, i.e. write order
      valid_from_*  event time rank
      author_owner  author belongs to the team owning the changed service
      kind_matches  candidate has the same kind as the fault class

    Not asserted: "candidate sits on the alert service". KTD23's origin clause forces the
    root onto the first symptomatic service, so that is structural rather than noise.
    """
    world = await create_session_world(sid)
    alert = WORLD_NOW
    cases = []
    for i in range(N_INJECTIONS):
        cls = CLASSES[i % 3]
        alert += 4 * 3600  # 4h apart: windows (< ~2.7h) never overlap
        f = await inject_fault(
            world,
            session_id=sid,
            fault_class=cls,
            rng_seed=1000 + i,
            alert_time=alert,
            store=store,
        )
        cands = await _candidates(world, alert)
        assert f.answer.root_id in {c[0] for c in cands}
        cases.append((cls, f.answer.root_id, cands))

    cols = {"delay": 1, "summary": 2, "insertion": 3, "valid_from": 4}

    def feature(name, cls, c):
        if name == "id":
            return int(c[0].split("-")[1])
        if name == "author_owner":
            return int(c[6])
        if name == "kind_matches":
            return int(c[5] == TRAINING_CLASSES[cls].value)
        return c[cols[name]]

    probes = [
        (n, fn) for n in ("delay", "summary", "id", "insertion", "valid_from") for fn in (max, min)
    ]
    probes += [("author_owner", max), ("kind_matches", max)]

    chance = sum(1 / len(c) for _, _, c in cases) / len(cases)
    results = {}
    for name, pick in probes:
        total = 0.0
        for cls, root, cands in cases:
            vals = [feature(name, cls, c) for c in cands]
            best = pick(vals)
            tied = [c[0] for c, v in zip(cands, vals, strict=True) if v == best]
            total += (root in tied) / len(tied)
        results[f"{name}_{pick.__name__}"] = total / len(cases)
    print(f"\nleakage chance={chance:.3f} " + " ".join(f"{k}={v:.3f}" for k, v in results.items()))
    for k, acc in results.items():
        assert acc <= chance + MARGIN, f"probe {k}: {acc:.3f} vs chance {chance:.3f}"


@pytest.mark.parametrize("fault_class", ["bad_deploy", "config_change", "flag_flip"])
async def test_incident_title_names_the_alerting_service_not_the_root(fault_class, tmp_path):
    """A pager title describes what fired. Naming the root would hand the answer over."""
    from ic.graph import client, naming
    from ic.graph.snapshots import create_session_world
    from ic.store import Store

    sid = naming.new_session_id()
    world = await create_session_world(sid)
    try:
        store = Store(tmp_path / "s.sqlite3")
        f = await inject_fault(
            world, session_id=sid, fault_class=fault_class, rng_seed=5, store=store
        )
        rows = (
            await client.query(
                world,
                "MATCH (i:Incident {id: $iid}) "
                "MATCH (a:Service) WHERE a.id = i.alert_service_id "
                "MATCH (c:Change {id: $root})-[:CHANGES]->(r:Service) "
                "RETURN i.title, a.name, r.name",
                {"iid": f.incident_id, "root": f.answer.root_id},
            )
        ).result_set
        title, alert_name, root_name = rows[0]
        assert alert_name in title
        if root_name != alert_name:
            assert root_name not in title
    finally:
        await client.delete_graph(world)


async def test_max_id_is_a_one_row_aggregate(sid):
    from ic.world.faults import _max_id

    g = await create_session_world(sid)
    expected = max(int(r[0][4:]) for r in await rows(g, "MATCH (c:Change) RETURN c.id"))
    assert await _max_id(g, "Change", "chg-") == expected
    assert await _max_id(g, "Storm", "storm-") == 0  # label with no nodes
