"""Per-key asyncio locks that do not outlive their users.

A plain ``dict.setdefault(key, asyncio.Lock())`` keeps one lock per key forever: keys carry
session and incident ids, so the dict grows for the life of the process. ``KeyedLocks``
counts the tasks holding or waiting on each key's lock and drops the entry when the last
one leaves, so only keys in use take memory. Mutual exclusion per key is unchanged: an entry
is never dropped while anyone holds or waits on it, so every contender shares one lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # holders + waiters


class KeyedLocks[K: Hashable]:
    """``async with locks(key):`` serializes the tasks using the same key."""

    def __init__(self) -> None:
        self._entries: dict[K, _Entry] = {}

    @asynccontextmanager
    async def __call__(self, key: K) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry()
        entry.users += 1  # no await since the lookup: no other task can drop it meanwhile
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(key) is entry:
                del self._entries[key]

    def locked(self, key: K) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry.lock.locked()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries
