import asyncio

import pytest

from ic.api.events import EventType
from ic.runs.registry import RunRegistry
from ic.store import Store

pytestmark = pytest.mark.asyncio


async def test_store_uses_wal_and_busy_timeout(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    with store.conn() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


async def test_concurrent_emits_on_many_runs_and_one_run(tmp_path):
    reg = RunRegistry(Store(tmp_path / "r.sqlite3"))
    runs = [reg.create(f"sid{i}", f"inc-{i}") for i in range(6)]
    await asyncio.gather(
        *(reg.emit(r, EventType.NARRATION, {"n": n}) for r in runs for n in range(10))
    )
    for r in runs:
        assert [e.id for e in r.events] == list(range(10))
        assert [e.data["n"] for e in r.events] == list(range(10))
        reloaded = RunRegistry(reg.store).get(r.id)
        assert [e.id for e in reloaded.events[:10]] == list(range(10))
