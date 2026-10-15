"""Recorded runs: the fallback served when limits or the spend breaker refuse a live run
(KTD10). ``record_hero`` runs one investigation in a throwaway session with mocked actions
and saves its event list plus the incident graph the console draws for it (the same payload
as ``GET /api/incidents/{id}/graph``); ``load_hero`` and ``load_hero_graph`` serve them.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ic.actions import pr_store
from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.config import Settings
from ic.graph import client, naming
from ic.graph.snapshots import create_session_world
from ic.runs.orchestrator import RunDeps, default_deps, execute_run
from ic.runs.registry import RunRegistry
from ic.store import Store
from ic.world.faults import inject_fault

RECORDED_DIR = Path(__file__).parent / "recorded"
HERO_PATH = RECORDED_DIR / "hero.json"
HERO_FAULT, HERO_SEED = "bad_deploy", 3
REDACTED_SID = "0" * 32


def hero_graph_path(events_path: Path | None = None) -> Path:
    """The graph file saved next to a recording: ``hero.json`` -> ``hero_graph.json``."""
    p = events_path or HERO_PATH
    return p.with_name(f"{p.stem}_graph.json")


def load_hero(path: Path | None = None) -> list[dict] | None:
    p = path or HERO_PATH
    if not p.is_file():
        return None
    data = json.loads(p.read_text())
    return data if isinstance(data, list) else None


def load_hero_graph(path: Path | None = None) -> dict | None:
    p = path or hero_graph_path()
    if not p.is_file():
        return None
    data = json.loads(p.read_text())
    return data if isinstance(data, dict) else None


async def _drop(prefix: str) -> None:
    """Delete every graph under ``prefix``; one failed delete does not stop the rest (the
    first error is raised at the end)."""
    db = client.get_db()
    first: Exception | None = None
    for name in await db.list_graphs():
        if name.startswith(prefix):
            try:
                await db.select_graph(name).delete()
            except Exception as e:
                first = first or e
    if first is not None:
        raise first


async def record_hero(
    path: Path | None = None,
    *,
    settings: Settings | None = None,
    fault_class: str = HERO_FAULT,
    seed: int = HERO_SEED,
    llm: Any = None,
) -> list[dict]:
    """Run a fallback investigation in a throwaway session and save its events and its
    incident graph (``hero_graph_path(path)``). ``llm=None`` records the deterministic
    agent; pass a client to record a live LLM run."""
    from ic.api.graph_view import _graph_payload  # ic.api imports this module

    settings = settings or Settings()
    out_path = path or HERO_PATH
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "record.sqlite3")
        sid = naming.new_session_id()
        try:
            world = await create_session_world(sid)  # inside: a half-built world is dropped
            f = await inject_fault(
                world, session_id=sid, fault_class=fault_class, rng_seed=seed, store=store
            )
            deps: RunDeps = default_deps(
                store,
                settings,
                llm,
                runs=RunRegistry(store),
                github=GitHubClient(None, None, mock=True),
                pager=DiscordPager(None, mock=True),
            )
            run = deps.runs.create(sid, f.incident_id)
            await execute_run(
                run,
                f.incident_id,
                f.alert_time,
                f.alert_service_id,
                deps=deps,
                scenario="hero",
                recorded=True,
            )
            events = [e.model_dump(mode="json") for e in run.events]
            # The run's as-known snapshot is still there: draw it before the drop below.
            graph = await _graph_payload(naming.asof(sid, f.incident_id, "known"), f.incident_id)
        finally:
            try:
                await _drop(f"s:{sid}:")
            finally:
                for name in pr_store.tracked_pr_graphs(store):
                    await client.delete_graph(name)
    text = json.dumps(events, sort_keys=True).replace(sid, REDACTED_SID)
    graph_text = json.dumps(graph, sort_keys=True).replace(sid, REDACTED_SID)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    hero_graph_path(out_path).write_text(graph_text)
    return json.loads(text)
