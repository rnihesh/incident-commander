"""Bounded retry for outbound actions: ``ActionError(retryable=True)`` only."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from ic.actions.errors import ActionError

ATTEMPTS = 3
BASE_DELAY_S = 0.5
MAX_DELAY_S = 10.0


async def retry_action[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = ATTEMPTS,
    base: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call ``fn`` up to ``attempts`` times. Waits ``base * 2**i`` with jitter (50-100%) between
    tries, or the server's ``retry_after`` when the error carries one (capped)."""
    base = BASE_DELAY_S if base is None else base
    for i in range(attempts):
        try:
            return await fn()
        except ActionError as e:
            if not e.retryable or i == attempts - 1:
                raise
            if e.retry_after is not None:
                delay = e.retry_after
            else:
                delay = base * 2**i * (0.5 + rng() / 2)
            await (sleep or asyncio.sleep)(min(delay, MAX_DELAY_S))
    raise AssertionError("unreachable")
