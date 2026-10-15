import pytest

from ic.graph import naming as n

SID = "a" * 32
OTHER = "b" * 32


def test_names():
    assert n.seed_world() == "seed:world"
    assert n.session_world(SID) == f"s:{SID}:world"
    assert n.ledger(SID) == f"s:{SID}:ledger"
    assert n.asof(SID, "inc-1", "true") == f"s:{SID}:asof:inc-1:true"
    assert n.asof(SID, "inc-1", "known").endswith(":known")
    assert n.verify(SID, "i", "true") == f"s:{SID}:verify:i:true"
    assert n.rehearse(SID, "i") == f"s:{SID}:rehearse:i"
    assert n.storm(SID, "st_1") == f"s:{SID}:storm:st_1"
    assert n.pr_world(SID, "i") == f"pr:{SID}:i:world"
    assert n.pr_ledger(SID, "i") == f"pr:{SID}:i:ledger"
    assert n.bench("r1", "c1", "world") == "b:r1:c1:world"
    assert len(n.new_session_id()) == 32
    assert n.new_session_id() != n.new_session_id()


@pytest.mark.parametrize("bad", ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 31 + ":", "*"])
def test_bad_sid(bad):
    with pytest.raises(ValueError):
        n.session_world(bad)


@pytest.mark.parametrize("bad", ["", "a:b", "a*", "x" * 65, "a b", "a\n"])
def test_bad_ids(bad):
    for f in (
        lambda: n.asof(SID, bad, "true"),
        lambda: n.verify(SID, bad, "known"),
        lambda: n.rehearse(SID, bad),
        lambda: n.storm(SID, bad),
        lambda: n.pr_world(SID, bad),
        lambda: n.pr_ledger(SID, bad),
        lambda: n.pr_world(bad, "i"),
        lambda: n.bench(bad, "c", "k"),
        lambda: n.bench("r", bad, "k"),
        lambda: n.bench("r", "c", bad),
    ):
        with pytest.raises(ValueError):
            f()


def test_bad_view():
    with pytest.raises(ValueError):
        n.asof(SID, "i", "other")


def test_max_length_ok():
    assert n.pr_world(SID, "x" * 64) == f"pr:{SID}:{'x' * 64}:world"


def test_scope_allows_matrix():
    own = n.session_world(SID)
    assert n.scope_allows(own, sid=SID)
    assert n.scope_allows(n.asof(SID, "i", "true"), sid=SID)
    assert not n.scope_allows(n.session_world(OTHER), sid=SID)
    assert not n.scope_allows(own, sid=OTHER)
    assert not n.scope_allows(own)  # no scope given
    assert not n.scope_allows("seed:world", sid=SID)
    assert n.scope_allows("seed:world", sid=SID, allow_seed_ro=True)
    assert not n.scope_allows("seed:world:building", sid=SID, allow_seed_ro=True)
    assert n.scope_allows(n.pr_world(SID, "i1"), sid=SID, iid="i1")
    assert not n.scope_allows(n.pr_world(SID, "i10"), sid=SID, iid="i1")
    assert not n.scope_allows(n.pr_world(SID, "i1"), sid=SID)  # needs the incident too
    assert not n.scope_allows(n.pr_world(SID, "i1"), iid="i1")  # and the session
    assert not n.scope_allows(n.pr_world(OTHER, "i1"), sid=SID, iid="i1")  # same iid, other sid
    assert not n.scope_allows("pr:i1:world", sid=SID, iid="i1")  # the old shape
    assert n.scope_allows("b:r1:c:w", run="r1")
    assert not n.scope_allows("b:r10:c:w", run="r1")
    assert not n.scope_allows("b:r1:c:w", iid="r1")
