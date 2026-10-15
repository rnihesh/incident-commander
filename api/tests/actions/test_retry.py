import pytest

from ic.actions import ActionError
from ic.actions.retry import retry_action


async def test_backoff_grows_and_jitters():
    sleeps = []

    async def sl(s):
        sleeps.append(s)

    async def fn():
        raise ActionError("x", retryable=True)

    with pytest.raises(ActionError):
        await retry_action(fn, sleep=sl, base=1.0, rng=lambda: 1.0)
    assert len(sleeps) == 2 and sleeps[0] == pytest.approx(1.0) and sleeps[1] == pytest.approx(2.0)


async def nosleep(s):
    pass


async def test_non_retryable_raises_immediately():
    calls = []

    async def fn():
        calls.append(1)
        raise ActionError("x", retryable=False)

    with pytest.raises(ActionError):
        await retry_action(fn, sleep=nosleep)
    assert calls == [1]
