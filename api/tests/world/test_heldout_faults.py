"""Held-out fault classes (U15, KTD15): expiry and undeclared coupling.

Same guarantees as the training classes, checked on real snapshots in both views: the root
qualifies under the KTD23 oracle and is necessary, every other windowed entity fails a
clause, and for the coupling class the observed edge is load-bearing."""

from __future__ import annotations

import pytest

from ic.graph import client, naming
from ic.graph.schema import COMPATIBLE_SIGNATURES, OBSERVED_CALL_THRESHOLD, ExpiryKind
from ic.graph.snapshots import build_snapshots, create_session_world, delete_snapshots
from ic.store import Store
from ic.world.causality import necessity, qualifies
from ic.world.faults import FAULT_CLASSES, HELDOUT_CLASSES, TRAINING_CLASSES, inject_fault
from ic.world.impact_reader import read_impact_graph, read_symptoms, read_window_entities

HELDOUT = sorted(HELDOUT_CLASSES)


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


def test_class_lists():
    assert set(HELDOUT_CLASSES) == {"expiry", "undeclared_coupling"}
    assert not set(HELDOUT_CLASSES) & set(TRAINING_CLASSES)
    assert set(FAULT_CLASSES) == set(TRAINING_CLASSES) | set(HELDOUT_CLASSES)


@pytest.mark.parametrize("fault_class", HELDOUT)
async def test_root_qualifies_and_every_other_window_entity_fails(sid, store, fault_class):
    world = await create_session_world(sid)
    for seed in (1, 2, 3):
        f = await inject_fault(
            world, session_id=sid, fault_class=fault_class, rng_seed=seed, store=store
        )
        root_id = f.answer.root_id
        assert store.get_answer(f.incident_id, sid)["root_id"] == root_id
        handles = await build_snapshots(world, sid, f.incident_id, f.alert_time)
        try:
            for view, snap in (("true", handles.true_graph), ("known", handles.known_graph)):
                g = await read_impact_graph(snap)
                symptoms = await read_symptoms(snap, f.incident_id)
                assert 3 <= len(symptoms) <= 6
                last = max(symptoms, key=lambda s: (s.onset, s.service_id))
                assert last.service_id == f.alert_service_id
                earliest = min(s.onset for s in symptoms)
                ents = await read_window_entities(snap, earliest)
                by_id = {e.id: e for e in ents}
                assert root_id in by_id, view
                root = by_id[root_id]
                ok, clause, detail = qualifies(root, g, symptoms)
                assert ok, f"{view}: root fails {clause}: {detail}"
                others = [e for e in ents if e.id != root_id]
                if view == "true":
                    assert len(others) >= 3
                for e in others:
                    ok, clause, _ = qualifies(e, g, symptoms)
                    assert not ok, f"{view}: {e.id} qualifies ({e.service_id}, {e.sig_kind})"
                    assert clause is not None
                assert necessity(root, ents, g, symptoms)[0]
                if fault_class == "expiry":
                    assert root.node_type == "Expiry"
                    assert root.sig_kind in {str(k) for k in ExpiryKind}
                    assert root.event_time < earliest  # valid_to just before the onset
                    allowed = {str(s) for s in COMPATIBLE_SIGNATURES[root.sig_kind]}
                    assert {s.signature for s in symptoms} <= allowed
                else:
                    assert root.node_type == "Change"
        finally:
            await delete_snapshots(handles)


OBSERVED_ONLY = (
    "MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service) "
    "WHERE o.count >= $thr AND size([(a)-[:DEPENDS_ON]->(b) | 1]) = 0 RETURN a.id, b.id"
)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
async def test_coupling_root_fails_coverage_without_observed_edges(sid, store, seed):
    world = await create_session_world(sid)
    f = await inject_fault(
        world, session_id=sid, fault_class="undeclared_coupling", rng_seed=seed, store=store
    )
    h = await build_snapshots(world, sid, f.incident_id, f.alert_time)
    try:
        for snap in (h.true_graph, h.known_graph):
            g = await read_impact_graph(snap)
            symptoms = await read_symptoms(snap, f.incident_id)
            ents = await read_window_entities(snap, min(s.onset for s in symptoms))
            root = next(e for e in ents if e.id == f.answer.root_id)
            assert qualifies(root, g, symptoms)[0]

            hidden = await rows(snap, OBSERVED_ONLY, {"thr": OBSERVED_CALL_THRESHOLD})
            assert hidden
            dg = g.copy()
            dg.remove_edges_from((b, a) for a, b in hidden)  # IMPACTS run callee -> caller
            ok, clause, _ = qualifies(root, dg, symptoms)
            assert not ok and clause == "coverage"
            # At least one symptomatic service is reachable only via an observed edge,
            # and the undeclared edge enters the root's own service.
            symptomatic = {s.service_id for s in symptoms}
            assert symptomatic - ({root.service_id} | _reach(dg, root.id))
            assert any(b == root.service_id and a in symptomatic for a, b in hidden)
    finally:
        await delete_snapshots(h)


def _reach(g, src):
    import networkx as nx

    return nx.descendants(g, src) if src in g else set()


async def test_heldout_injection_is_deterministic(store):
    outs = []
    for _ in range(2):
        s = naming.new_session_id()
        try:
            w = await create_session_world(s)
            got = []
            for cls in HELDOUT:
                f = await inject_fault(w, session_id=s, fault_class=cls, rng_seed=5, store=store)
                got.append((f.incident_id, f.alert_service_id, f.alert_time, f.answer.root_id))
            outs.append(got)
        finally:
            await client.delete_graph(naming.session_world(s))
    assert outs[0] == outs[1]


async def test_answer_stays_out_of_the_graph(sid, store):
    world = await create_session_world(sid)
    f = await inject_fault(world, session_id=sid, fault_class="expiry", rng_seed=9, store=store)
    blob = repr(await rows(world, "MATCH (n) RETURN properties(n)"))
    assert f.answer.nonce not in blob and f.answer.fault_id not in blob
    caused = await rows(
        world, "MATCH (:Incident {id: $i})-[e:CAUSED_BY]->() RETURN count(e)", {"i": f.incident_id}
    )
    assert caused[0][0] == 0
