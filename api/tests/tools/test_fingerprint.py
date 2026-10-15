"""Pure fingerprint functions: no database."""

from ic.tools.fingerprint import (
    HopInfo,
    cause_fingerprint,
    cause_similarity,
    count_bucket,
    edit_distance,
    matches,
    seq_similarity,
    symptom_fingerprint,
    symptom_similarity,
)


def syms(*pairs: tuple[str, str]) -> list[dict]:
    return [{"service_id": s, "signature": sig} for s, sig in pairs]


CHAIN = {"a": HopInfo(0, (1,)), "b": HopInfo(1, (1, 2)), "c": HopInfo(2, (1, 2, 3))}


def test_count_bucket():
    assert [count_bucket(n) for n in (1, 2, 3, 4, 5, 9)] == [0, 1, 2, 2, 3, 3]


def test_symptom_fingerprint_fields():
    fp = symptom_fingerprint(syms(("a", "latency"), ("b", "timeout"), ("c", "latency")), CHAIN)
    assert fp.signatures == ("latency", "timeout")
    assert (fp.count_bucket, fp.max_hop, fp.tier_seq) == (2, 2, (1, 2, 3))


def test_same_shape_on_other_services_is_equal():
    other = {"x": HopInfo(0, (1,)), "y": HopInfo(1, (1, 2)), "z": HopInfo(2, (1, 2, 3))}
    a = symptom_fingerprint(syms(("a", "latency"), ("b", "latency"), ("c", "latency")), CHAIN)
    b = symptom_fingerprint(syms(("x", "latency"), ("y", "latency"), ("z", "latency")), other)
    assert a == b and symptom_similarity(a, b) == 1.0 and matches(1.0)


def test_equal_depth_different_signatures_do_not_match():
    a = symptom_fingerprint(syms(("a", "latency"), ("c", "latency")), CHAIN)
    b = symptom_fingerprint(syms(("a", "error_5xx"), ("c", "error_5xx")), CHAIN)
    assert a.max_hop == b.max_hop
    assert symptom_similarity(a, b) == 0.0
    superset = symptom_fingerprint(syms(("a", "latency"), ("c", "timeout")), CHAIN)
    assert symptom_similarity(a, superset) == 0.0


def test_cause_kind_gates_cause_similarity():
    s = syms(("a", "latency"), ("b", "latency"))
    path = ["Change", "Service", "Service"]
    deploy = cause_fingerprint("deploy", s, path)
    assert cause_similarity(deploy, cause_fingerprint("deploy", s, path)) == 1.0
    # same hop count and signatures, different kind: no match
    assert cause_similarity(deploy, cause_fingerprint("config", s, path)) == 0.0
    # same kind and hop count, different signatures: no match
    other = cause_fingerprint("deploy", syms(("a", "timeout"), ("b", "timeout")), path)
    assert cause_similarity(deploy, other) == 0.0


def test_cause_label_sequence_discriminates():
    s = syms(("a", "tls_error"))
    cert = cause_fingerprint("cert", s, ["Expiry", "Service", "Service"])
    assert cert.hops == 2
    deeper = cause_fingerprint("cert", s, ["Expiry", "Service", "Service", "Service", "Service"])
    sim = cause_similarity(cert, deeper)
    assert 0 < sim < 1 and not matches(sim)


def test_depth_and_tiers_lower_similarity_smoothly():
    base = symptom_fingerprint(syms(("a", "latency"), ("c", "latency")), CHAIN)
    shallow = symptom_fingerprint(syms(("a", "latency"), ("b", "latency")), CHAIN)
    s1 = symptom_similarity(base, shallow)
    assert 0.5 < s1 < 1.0


def test_unreachable_services_ignored_and_empty_input():
    fp = symptom_fingerprint(syms(("a", "latency"), ("lost", "latency")), CHAIN)
    assert fp.max_hop == 0
    empty = symptom_fingerprint([], {})
    assert empty.signatures == () and empty.max_hop == 0


def test_edit_distance_and_similarity():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance([], [1, 2]) == 2
    assert seq_similarity([], []) == 1.0
    assert seq_similarity([1, 2, 3], [1, 2, 3]) == 1.0
    assert seq_similarity([1, 2], [3, 4]) == 0.0
