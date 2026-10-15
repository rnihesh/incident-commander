"""KeyedLocks: per-key mutual exclusion whose entries go away once nobody uses them."""

from __future__ import annotations

import asyncio

import pytest

from ic.locks import KeyedLocks


async def test_same_key_is_mutually_exclusive():
    locks: KeyedLocks[str] = KeyedLocks()
    inside, peak, order = 0, 0, []

    async def worker(i: int) -> None:
        nonlocal inside, peak
        async with locks("k"):
            inside += 1
            peak = max(peak, inside)
            order.append(i)
            await asyncio.sleep(0.005)
            inside -= 1

    await asyncio.gather(*(worker(i) for i in range(10)))
    assert peak == 1
    assert sorted(order) == list(range(10))
    assert len(locks) == 0


async def test_different_keys_do_not_block_each_other():
    locks: KeyedLocks[str] = KeyedLocks()
    async with locks("a"):
        assert locks.locked("a")
        async with asyncio.timeout(1):
            async with locks("b"):  # would hang if keys shared a lock
                assert locks.locked("b")
        assert "b" not in locks
    assert not locks.locked("a")
    assert len(locks) == 0


async def test_entry_is_evicted_after_release_and_on_error():
    locks: KeyedLocks[tuple[str, int]] = KeyedLocks()
    async with locks(("s", 1)):
        assert ("s", 1) in locks and len(locks) == 1
    assert ("s", 1) not in locks

    with pytest.raises(RuntimeError):
        async with locks(("s", 2)):
            raise RuntimeError("boom")
    assert len(locks) == 0


async def test_entry_stays_while_a_waiter_is_queued():
    locks: KeyedLocks[str] = KeyedLocks()
    release = asyncio.Event()
    got_it = asyncio.Event()

    async def holder() -> None:
        async with locks("k"):
            await release.wait()

    async def waiter() -> None:
        async with locks("k"):
            got_it.set()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert locks.locked("k") and not got_it.is_set()
    release.set()  # the holder leaves; the waiter must get the same, still-listed lock
    await asyncio.gather(h, w)
    assert got_it.is_set()
    assert len(locks) == 0


async def test_cancelled_waiter_does_not_leak_or_break_exclusion():
    locks: KeyedLocks[str] = KeyedLocks()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks("k"):
            await release.wait()

    async def waiter() -> None:
        async with locks("k"):
            pytest.fail("a cancelled waiter must never enter")

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert "k" in locks  # the holder still has it
    release.set()
    await h
    assert len(locks) == 0
    async with locks("k"):  # and the key is usable again
        assert locks.locked("k")


async def test_many_concurrent_waiters_across_keys():
    locks: KeyedLocks[int] = KeyedLocks()
    inside: dict[int, int] = {}
    peak: dict[int, int] = {}

    async def worker(key: int) -> None:
        async with locks(key):
            inside[key] = inside.get(key, 0) + 1
            peak[key] = max(peak.get(key, 0), inside[key])
            await asyncio.sleep(0)
            inside[key] -= 1

    await asyncio.gather(*(worker(i % 7) for i in range(200)))
    assert set(peak) == set(range(7)) and set(peak.values()) == {1}
    assert len(locks) == 0
