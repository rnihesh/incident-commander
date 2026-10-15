from datetime import UTC, datetime

from ic.sessions.breaker import Breaker
from ic.store import Store

PRICES = {"m": [1.0, 2.0], "default": [10.0, 10.0]}
D1 = datetime(2026, 10, 9, 12, tzinfo=UTC).timestamp()
D2 = datetime(2026, 10, 10, 1, tzinfo=UTC).timestamp()


def test_cost_and_default_price(tmp_path):
    b = Breaker(Store(tmp_path / "b.sqlite3"), daily_usd=1.0, prices=PRICES)
    assert b.cost("m", 1_000_000, 500_000) == 2.0
    assert b.cost("unknown", 100_000, 0) == 1.0


def test_trips_at_amount_and_resets_next_day(tmp_path):
    b = Breaker(Store(tmp_path / "b.sqlite3"), daily_usd=3.0, prices=PRICES)
    b.record_usage("m", 1_000_000, 0, now=D1)  # $1
    assert not b.tripped("2026-10-09")
    b.record_usage("m", 0, 1_000_000, now=D1)  # +$2 = $3 exactly
    assert b.tripped("2026-10-09")
    assert not b.tripped("2026-10-10")
    b.record_usage("m", 1000, 1000, now=D2)
    assert not b.tripped("2026-10-10")


def test_persists_across_instances(tmp_path):
    p = tmp_path / "b.sqlite3"
    Breaker(Store(p), daily_usd=1.0, prices=PRICES).record_usage("m", 2_000_000, 0, now=D1)
    assert Breaker(Store(p), daily_usd=1.0, prices=PRICES).tripped("2026-10-09")
