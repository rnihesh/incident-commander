"""Daily dollar breaker from logged token usage (KTD10)."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

from ic.config import get_settings
from ic.store import Store


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


class Breaker:
    def __init__(
        self,
        store: Store,
        *,
        daily_usd: float | None = None,
        prices: dict[str, list[float]] | None = None,
    ):
        s = get_settings()
        self.store = store
        self.daily_usd = s.daily_usd_breaker if daily_usd is None else daily_usd
        self.prices = s.llm_prices if prices is None else prices

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p_in, p_out = self.prices.get(model) or self.prices.get("default") or [5.0, 20.0]
        return (prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000

    def record_usage(
        self, model: str, prompt_tokens: int, completion_tokens: int, *, now: float | None = None
    ) -> float:
        now = time.time() if now is None else now
        usd = self.cost(model, prompt_tokens, completion_tokens)
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO llm_usage VALUES (?, ?, ?, ?, ?, ?)",
                (now, _day(now), model, prompt_tokens, completion_tokens, usd),
            )
        return usd

    def spend(self, today: date | str | None = None) -> float:
        day = today.isoformat() if isinstance(today, date) else (today or _day(time.time()))
        with self.store.conn() as c:
            return c.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM llm_usage WHERE day = ?", (day,)
            ).fetchone()[0]

    def tripped(self, today: date | str | None = None) -> bool:
        return self.spend(today) >= self.daily_usd
