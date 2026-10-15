import os
import subprocess
import sys
import time
from collections import Counter

import networkx as nx
import pytest

from ic.graph.schema import HISTORY_DAYS, WORLD_NOW
from ic.world.causality import qualifies
from ic.world.generator import generate_world, verify_incidents
from ic.world.model import call_graph, impact_graph, world_content_hash


@pytest.fixture(scope="module")
def world():
    return generate_world(42, 400)


def test_same_seed_same_world_different_seed_different_world():
    a, b, c = generate_world(5, 60), generate_world(5, 60), generate_world(6, 60)
    assert a.node_counts() == b.node_counts() and a.edge_counts() == b.edge_counts()
    assert world_content_hash(a) == world_content_hash(b)
    assert world_content_hash(a) != world_content_hash(c)


def test_hash_independent_of_python_hash_randomisation():
    code = (
        "from ic.world.generator import generate_world as g;"
        "from ic.world.model import world_content_hash as h;print(h(g(3, 40)))"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": hs},
        ).stdout
        for hs in ("1", "2")
    }
    assert len(outs) == 1


def test_scale(world):
    assert 380 <= len({s.name for s in world.services}) <= 420
    assert len(world.services) > 400  # service-region nodes
    assert len(world.incidents) >= 30
    assert len(world.adrs) == 20
    assert Counter(s.region for s in world.services).keys() == {"us-east", "eu-west", "ap-south"}


def test_inbound_degree_is_heavy_tailed(world):
    indeg = Counter(d.callee_id for d in world.dependencies)
    counts = sorted((indeg.get(s.id, 0) for s in world.services), reverse=True)
    top = counts[: max(1, len(counts) // 20)]
    assert sum(top) >= 0.30 * sum(counts)


def test_no_service_is_upstream_of_every_other(world):
    g = call_graph(world)
    n = g.number_of_nodes()
    assert max(len(nx.descendants(g, s)) for s in g) < n - 1


def test_every_service_has_owner_and_oncall(world):
    owners = {o.service_id: o.team_id for o in world.ownerships}
    assert set(owners) == {s.id for s in world.services}
    for t in (
        WORLD_NOW - HISTORY_DAYS * 86400 + 5,
        WORLD_NOW - 40 * 86400,
        WORLD_NOW,
        WORLD_NOW + 86400,
    ):
        covered = {o.team_id for o in world.oncall if o.valid_from <= t < o.valid_to}
        assert set(owners.values()) <= covered


def test_change_times_are_consistent(world):
    for c in world.changes:
        assert c.recorded_at >= c.valid_from
        assert c.valid_to > c.valid_from
    ids = [c.id for c in world.changes]
    assert len(ids) == len(set(ids))


def test_each_flag_gated_by_config_of_same_service(world):
    cfg = {c.id: c.service_id for c in world.configs}
    assert all(cfg[f.config_id] == f.service_id for f in world.flags)


def test_future_expiries_exist(world):
    assert any(e.valid_to > WORLD_NOW for e in world.expiries)


def test_undeclared_couplings_present(world):
    declared = {(d.caller_id, d.callee_id) for d in world.dependencies}
    assert sum((o.caller_id, o.callee_id) not in declared for o in world.observed_calls) > 0
    assert any(i.coupling for i in world.incidents)


def test_incident_causes_qualify_and_decoys_fail(world):
    verify_incidents(world)
    assert sum(len(i.decoy_ids) for i in world.incidents) >= 2 * len(world.incidents)
    for inc in world.incidents:
        g = impact_graph(world, inc.resolved_at, as_known=False)
        failing = {
            qualifies(world.entity(d), g, world.symptoms_of(inc.id))[1] for d in inc.decoy_ids
        }
        assert None not in failing


def test_cause_kinds_cover_expiry_and_coupling(world):
    assert {i.cause_kind for i in world.incidents} == {"change", "expiry"}


def test_open_recommendation_targets_service_on_causal_path(world):
    found = False
    for rec in world.recommendations:
        pm = next(p for p in world.postmortems if p.id == rec.postmortem_id)
        path = {s.service_id for s in world.symptoms_of(pm.incident_id)}
        if rec.status == "open" and set(rec.target_ids) <= path | {
            world.entity(
                next(i.cause_id for i in world.incidents if i.id == pm.incident_id)
            ).service_id
        }:
            found = True
    assert found


def test_generation_of_400_services_is_fast():
    start = time.perf_counter()
    generate_world(43, 400)
    assert time.perf_counter() - start < 30
