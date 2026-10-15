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


def test_long_prompt_tier(tmp_path):
    """A request whose prompt exceeds the model's threshold is priced at the long-prompt rate."""
    b = Breaker(
        Store(tmp_path / "b.sqlite3"),
        daily_usd=1.0,
        prices=PRICES,
        long_prompt_prices={"m": (100_000, [5.0, 10.0])},
    )
    assert b.cost("m", 100_000, 0) == 0.1  # at the threshold: base rate
    assert b.cost("m", 200_000, 100_000) == 2.0  # 0.2M * 5 + 0.1M * 10
    assert b.cost("unknown", 200_000, 0) == 2.0  # no tier: default rate


def test_shipped_prices_match_the_rate_card():
    from ic.config import Settings

    s = Settings()
    assert s.llm_prices["claude-haiku-5-5"] == [0.10, 0.50]
    assert s.llm_prices["claude-sonnet-5-5"] == [2.0, 10.0]
    assert s.llm_long_prompt_prices["claude-haiku-5-5"] == (100_000, [0.50, 2.50])
