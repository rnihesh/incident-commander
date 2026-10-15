"""One test per KTD23 clause, on tiny hand-built worlds."""

import networkx as nx

from ic.graph.schema import (
    OBSERVED_CALL_THRESHOLD,
    OPEN_END,
    WORLD_NOW,
    ChangeKind,
    ExpiryKind,
    Signature,
)
from ic.world.causality import explaining_entities, necessity, qualifies, screen
from ic.world.model import (
    Change,
    Dependency,
    Expiry,
    ObservedCall,
    Service,
    Symptom,
    World,
    impact_graph,
)

T0 = WORLD_NOW - 10_000


def chg(cid: str, service: str, t: int, kind: str = ChangeKind.DEPLOY) -> Change:
    return Change(cid, kind, service, "x", t, OPEN_END, t, "p", "deploy", None, "v2", "v1")


def sym(service: str, onset: int, sig: str = Signature.ERROR_5XX) -> Symptom:
    return Symptom(f"sym-{service}", "inc", service, onset, sig)


def chain_graph() -> nx.DiGraph:
    """db -> api -> web: failure travels db to api to web; change c1 sits on db."""
    g = nx.DiGraph()
    g.add_edge("c1", "db")
    g.add_edge("db", "api")
    g.add_edge("api", "web")
    return g


def test_all_clauses_hold():
    symptoms = [sym("db", T0 + 100), sym("api", T0 + 200), sym("web", T0 + 300)]
    ok, clause, _ = qualifies(chg("c1", "db", T0), chain_graph(), symptoms)
    assert ok and clause is None


def test_after_onset_fails_when_event_is_late():
    ok, clause, _ = qualifies(chg("c1", "db", T0 + 150), chain_graph(), [sym("db", T0 + 100)])
    assert (ok, clause) == (False, "after_onset")


def test_coverage_names_missing_service():
    g = chain_graph()
    g.add_node("billing")
    ok, clause, detail = qualifies(
        chg("c1", "db", T0), g, [sym("db", T0 + 10), sym("billing", T0 + 20)]
    )
    assert (ok, clause) == (False, "coverage")
    assert "billing" in detail


def test_order_fails_when_far_service_starts_first():
    # db fails first (origin holds) but web (hop 2) fails before api (hop 1).
    symptoms = [sym("db", T0 + 100), sym("api", T0 + 300), sym("web", T0 + 200)]
    ok, clause, detail = qualifies(chg("c1", "db", T0), chain_graph(), symptoms)
    assert (ok, clause) == (False, "order")
    assert "api" in detail and "web" in detail


def test_order_allows_ties():
    symptoms = [sym("db", T0 + 100), sym("api", T0 + 100)]
    assert qualifies(chg("c1", "db", T0), chain_graph(), symptoms)[0]


def test_signature_mismatch():
    symptoms = [sym("db", T0 + 10, Signature.TLS_ERROR)]
    ok, clause, _ = qualifies(chg("c1", "db", T0), chain_graph(), symptoms)
    assert (ok, clause) == (False, "signature")


def test_expiry_uses_valid_to_as_event_time():
    exp = Expiry("e1", ExpiryKind.CERT, "db", "cert", T0 - 365 * 86400, T0 + 500, T0)
    g = chain_graph()
    g.add_edge("e1", "db")
    tls = Signature.TLS_ERROR
    # issued long ago, but it only *expires* after the symptom starts: not an explanation
    assert qualifies(exp, g, [sym("db", T0 + 100, tls)])[1] == "after_onset"
    exp.valid_to = T0 + 50
    assert qualifies(exp, g, [sym("db", T0 + 100, tls)])[0]


def _coupling_world() -> World:
    w = World(1, 3)
    w.services = [Service(f"svc-{n}@us-east", n, "us-east", 1, "d") for n in ("db", "legacy")]
    db, legacy = (s.id for s in w.services)
    w.changes = [chg("c1", db, T0)]
    # legacy calls db, but nobody declared it
    w.observed_calls = [
        ObservedCall(legacy, db, T0 - 1000, WORLD_NOW, OBSERVED_CALL_THRESHOLD + 10)
    ]
    return w


def test_undeclared_coupling_reachable_only_via_observed_call():
    w = _coupling_world()
    db, legacy = (s.id for s in w.services)
    symptoms = [sym(db, T0 + 100), sym(legacy, T0 + 200)]
    c1 = w.changes[0]
    with_obs = impact_graph(w, T0 + 1000, as_known=False)
    without = impact_graph(w, T0 + 1000, as_known=False, include_observed=False)
    assert qualifies(c1, with_obs, symptoms)[0]
    assert qualifies(c1, without, symptoms)[:2] == (False, "coverage")


def test_weak_observed_calls_do_not_create_impacts():
    w = _coupling_world()
    w.observed_calls[0].count = OBSERVED_CALL_THRESHOLD - 1
    db, legacy = (s.id for s in w.services)
    g = impact_graph(w, T0 + 1000, as_known=False)
    assert not g.has_edge(db, legacy)


def test_as_known_drops_late_recorded_dependency():
    w = _coupling_world()
    db, legacy = (s.id for s in w.services)
    w.observed_calls = []
    w.dependencies = [Dependency(legacy, db, T0 - 100, OPEN_END, T0 + 5000)]
    assert impact_graph(w, T0 + 1000, as_known=False).has_edge(db, legacy)
    assert not impact_graph(w, T0 + 1000, as_known=True).has_edge(db, legacy)


def test_necessity_and_screen():
    g = chain_graph()
    g.add_edge("c2", "db")
    symptoms = [sym("db", T0 + 100)]
    c1, c2 = chg("c1", "db", T0), chg("c2", "db", T0 + 50)
    late = chg("c3", "db", T0 + 500)
    g.add_edge("c3", "db")
    assert [e.id for e in explaining_entities([c1, c2, late], g, symptoms)] == ["c1", "c2"]
    assert necessity(c1, [c1, late], g, symptoms) == (True, [])
    ok, rivals = necessity(c1, [c1, c2], g, symptoms)
    assert not ok and rivals == [c2]
    verdicts = {v.entity_id: v.clause for v in screen([c1, late], g, symptoms)}
    assert verdicts == {"c1": None, "c3": "after_onset"}


def test_origin_fails_for_change_on_unsymptomatic_dependency():
    """A deploy on 'disk' (which db depends on) reaches every symptom but disk itself is healthy."""
    g = chain_graph()
    g.add_edge("c0", "disk")
    g.add_edge("disk", "db")
    symptoms = [sym("db", T0 + 100), sym("api", T0 + 200)]
    ok, clause, detail = qualifies(chg("c0", "disk", T0), g, symptoms)
    assert (ok, clause) == (False, "origin")
    assert "disk" in detail


def test_origin_fails_when_changed_service_is_not_earliest():
    symptoms = [sym("db", T0 + 300), sym("api", T0 + 100)]
    g = chain_graph()
    g.add_edge("c2", "api")
    ok, clause, _ = qualifies(chg("c2", "api", T0), g, [sym("db", T0 + 300), sym("api", T0 + 100)])
    assert ok is False and clause == "coverage"  # api's change cannot reach db
    ok, clause, _ = qualifies(chg("c1", "db", T0), chain_graph(), symptoms)
    assert (ok, clause) == (False, "origin")
