"""Application state (U9): ``AppState`` with the store, sessions, limits, breaker, budget,
run registry and per-key locks, built once per app and fetched by every route.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from ic.agent.llm import LLMClient, LLMConfigError, make_llm
from ic.config import Settings
from ic.locks import KeyedLocks
from ic.runs.orchestrator import RunDeps, default_deps
from ic.runs.recorded import HERO_PATH
from ic.runs.registry import RunRegistry
from ic.sessions.action_budget import ActionBudget
from ic.sessions.breaker import Breaker
from ic.sessions.limits import Limiter
from ic.sessions.manager import SessionManager
from ic.store import Store

AUTO: Any = object()  # create_app(llm=AUTO): a client if a key is configured, else None
BENCH_DIR = Path(__file__).resolve().parents[3] / "bench_results"
LEARNING_CURVE_PATH = BENCH_DIR / "learning_curve.json"
BENCHMARK_PATH = BENCH_DIR / "report.json"


@dataclass
class AppState:
    settings: Settings
    store: Store
    sessions: SessionManager
    limiter: Limiter
    breaker: Breaker
    budget: ActionBudget
    runs: RunRegistry
    llm: LLMClient | None
    deps: RunDeps
    recorded_path: Path = HERO_PATH
    learning_curve_path: Path = LEARNING_CURVE_PATH
    benchmark_path: Path = BENCHMARK_PATH
    create_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # GitHub client for the PR auto-close pass and proof status re-runs; None builds one
    # from settings per use (mock when no token/repo). Tests inject a fake here.
    github: Any = None
    last_pr_close: float = 0.0  # time.time() of the last auto-close pass
    # Public proof page re-runs: the last report per proof ref, served while debounced.
    proof_reports: dict[str, dict] = field(default_factory=dict)
    # Query X-ray responses per (sid, incident, step): a finished run's snapshot does not
    # change, so a repeated toggle is served without another PROFILE. A re-derive (which
    # rebuilds the incident) forgets the incident's entries.
    xray_cache: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    _locks: KeyedLocks[str] = field(default_factory=KeyedLocks)

    def lock_for(self, key: str) -> AbstractAsyncContextManager[None]:
        """``async with s.lock_for(key):`` serializes on ``key``; idle keys are dropped."""
        return self._locks(key)

    def forget_xray(self, sid: str, iid: str) -> None:
        for key in [k for k in self.xray_cache if k[:2] == (sid, iid)]:
            del self.xray_cache[key]


def auto_llm(settings: Settings) -> LLMClient | None:
    """``make_llm`` when a provider key is configured; None selects the deterministic
    fallback (verdicts are tagged degraded)."""
    try:
        return make_llm(settings)
    except LLMConfigError:
        return None


def build_state(
    settings: Settings,
    *,
    llm: Any = AUTO,
    store: Store | None = None,
    recorded_path: Path | None = None,
) -> AppState:
    store = store or Store(settings.store_path)
    store.init()
    # Runs live in this process: a counter left by a dead process would block its session.
    with store.conn() as c:
        c.execute("UPDATE sessions SET active_runs = 0 WHERE active_runs > 0")
    llm = auto_llm(settings) if llm is AUTO else llm
    sessions = SessionManager(
        store, max_sessions=settings.max_sessions, idle_ttl_s=settings.session_idle_ttl_s
    )
    limiter = Limiter(
        store,
        max_sessions=settings.max_sessions,
        global_runs_per_hour=settings.global_runs_per_hour,
        idle_ttl_s=settings.session_idle_ttl_s,
        proof_verifies_per_ip_hour=settings.proof_verifies_per_ip_hour,
    )
    breaker = Breaker(store, daily_usd=settings.daily_usd_breaker, prices=settings.llm_prices)
    budget = ActionBudget(store, per_hour=settings.actions_per_hour)
    runs = RunRegistry(store)
    deps = default_deps(
        store, settings, llm, runs=runs, sessions=sessions, breaker=breaker, budget=budget
    )
    return AppState(
        settings=settings,
        store=store,
        sessions=sessions,
        limiter=limiter,
        breaker=breaker,
        budget=budget,
        runs=runs,
        llm=llm,
        deps=deps,
        recorded_path=recorded_path or HERO_PATH,
    )


def app_state(app: FastAPI) -> AppState:
    s = getattr(app.state, "ic", None)
    if s is None:
        factory = app.state.ic_factory
        s = app.state.ic = factory()
    return s


def get_state(request: Request) -> AppState:
    return app_state(request.app)
