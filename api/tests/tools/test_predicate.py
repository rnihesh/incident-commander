"""The Cypher KTD23 predicate: one fixture per clause, then agreement with the oracle."""

from collections import Counter
from dataclasses import dataclass, replace
from types import SimpleNamespace

import fixture_world as fx
import networkx as nx
import pytest
from fixture_world import T0, write_impacts_only

from ic.graph import naming
from ic.graph.snapshots import build_snapshots, delete_snapshots
from ic.tools import predicate as P
from ic.tools.result import QueryLog, ToolError
from ic.world.causality import CLAUSES, qualifies
from ic.world.embed import HashingEmbedder
from ic.world.generator import generate_world
from ic.world.loader import load_world
from ic.world.model import World, impact_graph


@pytest.fixture(scope="module")
async def graph(module_sid):
    g = naming.asof(module_sid, "fx", "true")
    await write_impacts_only(g)
    return g


async def verdict(graph: str, incident_id: str, entity_id: str) -> P.Verdict:
    log = QueryLog(graph)
    facts = await P.incident_facts(log, incident_id)
    label = await P.entity_label(log, entity_id, P.ENTITY_LABELS)
    return await P.explain(log, facts, entity_id, label)


async def test_bad_deploy_two_hops_upstream_passes(graph):
    v = await verdict(graph, "inc-main", "c-bad")
    assert (v.ok, v.clause) == (True, None)
    assert v.hops == {"db": 1, "api": 2, "web": 3}  # web is two hops above the change's service


async def test_decoy_after_onset(graph):
    v = await verdict(graph, "inc-main", "c-late")
    assert (v.ok, v.clause) == (False, "after_onset")
    assert str(T0 + 150) in v.detail


async def test_decoy_on_healthy_dependency_fails_origin(graph):
    v = await verdict(graph, "inc-main", "c-disk")
    assert (v.ok, v.clause) == (False, "origin")
    assert "disk" in v.detail
    assert v.hops == {"db": 2, "api": 3, "web": 4}  # it does reach everything


async def test_decoy_missing_a_symptomatic_service_fails_coverage(graph):
    v = await verdict(graph, "inc-cov", "c-bad")
    assert (v.ok, v.clause) == (False, "coverage")
    assert "billing" in v.detail


async def test_reversed_onset_order_fails_order(graph):
    v = await verdict(graph, "inc-order", "c-bad")
    assert (v.ok, v.clause) == (False, "order")
    assert "api" in v.detail and "web" in v.detail


async def test_equal_onsets_at_different_hops_are_allowed(graph):
    v = await verdict(graph, "inc-tie", "c-bad")
    assert (v.ok, v.clause) == (True, None)


async def test_config_change_against_tls_error_fails_signature(graph):
    v = await verdict(graph, "inc-exp", "c-cfg")
    assert (v.ok, v.clause) == (False, "signature")
    assert "tls_error" in v.detail


async def test_expiry_uses_valid_to_and_qualifies(graph):
    v = await verdict(graph, "inc-exp", "x-cert")
    assert (v.ok, v.entity, v.event_time) == (True, "Expiry", T0 - 300)


async def test_window_and_returned_evidence(graph):
    out = await P.run_predicate(graph, "inc-main")
    got = {v["id"]: v["clause"] for v in out["verdicts"]}
    assert got == {
        "c-bad": None,
        "c-late": "after_onset",
        "c-disk": "origin",
        "c-cfg": "signature",
        "c-api": "origin",
        "c-unk": "origin",
        "x-cert": "signature",
    }  # c-old is before the 120-minute window and is not a candidate
    assert out["window"]["from"] == T0 + 100 - 120 * 60
    assert len(out["cypher"]) == len(out["params"]) == 3 + len(got)
    assert all(
        {"id", "kind", "service_id", "ok", "clause", "detail"} <= v.keys() for v in out["verdicts"]
    )
    narrow = await P.run_predicate(graph, "inc-main", window_minutes=5)
    assert {v["id"] for v in narrow["verdicts"]} == {"c-cfg", "c-api", "c-unk", "c-late"}


async def test_missing_incident_is_an_error(graph):
    with pytest.raises(ToolError, match="not in this snapshot"):
        await P.run_predicate(graph, "inc-nope")


# --- oracle agreement ------------------------------------------------------------------


@dataclass
class _Entity:  # what causality.qualifies needs
    id: str
    service_id: str
    event_time: int
    sig_kind: str


async def test_agrees_with_oracle_on_fixture(graph):
    """Every (incident, entity) pair of the hand-built graph, including coverage/order."""
    g = nx.DiGraph()
    for a, b in await QueryLog(graph).rows("MATCH (a)-[:IMPACTS]->(b) RETURN a.id, b.id"):
        g.add_edge(a, b)
    entities = [_Entity(c[0], c[2], c[3], c[1]) for c in fx.CHANGES]
    entities += [_Entity(e[0], e[2], e[4], e[1]) for e in fx.EXPIRIES]
    clauses = Counter()
    for iid, syms in fx.INCIDENTS.items():
        symptoms = [SimpleNamespace(service_id=s, onset=o, signature=sig) for s, o, sig in syms]
        for e in entities:
            v = await verdict(graph, iid, e.id)
            assert (v.ok, v.clause) == qualifies(e, g, symptoms)[:2], (iid, e.id)
            clauses[v.clause] += 1
    assert set(clauses) == {None, *CLAUSES}


def add_symptom_variants(world: World) -> None:
    """For each incident add two variants sharing its alert time ``t``, so the generated
    worlds also exercise the order and coverage clauses on real topology:
      <id>-rev  all its symptoms, re-timed to be visible at t, nearer hops failing later
      <id>-cov  its visible symptoms plus one on a service the root cannot reach"""
    for inc in list(world.incidents):
        t = inc.opened_at
        g = impact_graph(world, t, as_known=False)
        syms = sorted(world.symptoms_of(inc.id), key=lambda s: s.onset)
        root, rest = syms[0], syms[1:]
        hop = nx.single_source_shortest_path_length(g, root.service_id)
        rest.sort(key=lambda s: (hop.get(s.service_id, 99), s.service_id))
        visible = [s for s in syms if s.onset <= t]
        far = next(s.id for s in world.services if s.id not in hop)
        variants = {
            "rev": [root, *(replace(s, onset=t - i) for i, s in enumerate(rest))],
            "cov": [*visible, replace(root, service_id=far, onset=root.onset + 1)],
        }
        for suffix, syms in variants.items():
            vid = f"{inc.id}-{suffix}"
            world.incidents.append(replace(inc, id=vid))
            world.symptoms += [
                replace(s, id=f"{vid}-{n}", incident_id=vid) for n, s in enumerate(syms)
            ]


AGREEMENT: Counter = Counter()


@pytest.mark.parametrize("seed", [3, 7, 11])
async def test_agrees_with_oracle_on_generated_worlds(sid, seed):
    """Every windowed candidate of every past incident (and its variants), in both views,
    gets the same (ok, clause) from Cypher as from causality.qualifies on model.impact_graph."""
    world = generate_world(seed, n_services=40)
    originals = list(world.incidents)
    add_symptom_variants(world)
    src = naming.session_world(sid)
    await load_world(world, src, HashingEmbedder(64))
    window = P.DEFAULT_WINDOW_MINUTES * 60
    compared = Counter()
    for inc in originals:
        t = inc.opened_at
        h = await build_snapshots(src, sid, inc.id, t)
        for view, name in (("true", h.true_graph), ("known", h.known_graph)):
            known = view == "known"
            g = impact_graph(world, t, as_known=known)
            for iid in (inc.id, f"{inc.id}-rev", f"{inc.id}-cov"):
                symptoms = [s for s in world.symptoms_of(iid) if s.onset <= t]
                lo = min(s.onset for s in symptoms) - window
                out = await P.run_predicate(name, iid)
                got = {v["id"]: (v["ok"], v["clause"]) for v in out["verdicts"]}
                expected_ids = {
                    c.id
                    for c in world.changes
                    if lo <= c.valid_from <= t and (not known or c.recorded_at <= t)
                } | {e.id for e in world.expiries if lo <= e.valid_to <= t}
                assert set(got) == expected_ids, (seed, iid, view)
                for eid, ok_clause in got.items():
                    oracle = qualifies(world.entity(eid), g, symptoms)[:2]
                    assert ok_clause == oracle, (seed, iid, view, eid, ok_clause, oracle)
                    compared[ok_clause[1] or "qualifies"] += 1
                if iid == inc.id and not known:  # the recorded cause explains its incident
                    assert got[inc.cause_id] == (True, None), (seed, iid)
        await delete_snapshots(h)
    AGREEMENT.update(compared)
    print(f"\nseed {seed}: {sum(compared.values())} verdicts agree {dict(compared)}")
    assert compared["qualifies"] >= len(originals)
    assert set(CLAUSES) <= set(compared)
