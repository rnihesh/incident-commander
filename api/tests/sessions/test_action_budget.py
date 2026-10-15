from ic.sessions.action_budget import ActionBudget
from ic.store import Store


def test_budget_exhausts_per_kind_and_refills(tmp_path):
    b = ActionBudget(Store(tmp_path / "a.sqlite3"), per_hour=2)
    assert b.allow("pr", now=1000) and b.allow("pr", now=1001)
    assert not b.allow("pr", now=1002)  # caller goes to mock mode, labelled simulated
    assert b.allow("page", now=1002)  # separate budget per kind
    assert b.allow("pr", now=1000 + 3700)


def test_status_rerun_debounced(tmp_path):
    b = ActionBudget(Store(tmp_path / "a.sqlite3"), per_hour=2)
    assert b.allow_status_rerun("inc-1", now=1000)
    assert not b.allow_status_rerun("inc-1", now=1030)
    assert b.allow_status_rerun("inc-2", now=1030)
    assert b.allow_status_rerun("inc-1", now=1061)
