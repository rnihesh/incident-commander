"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ic.api import (
    routes,
    routes_ask,
    routes_challenge,
    routes_judge,
    routes_proof,
    routes_replay,
    routes_storm,
)
from ic.api.maintenance import cleanup_loop
from ic.api.state import AUTO, app_state, build_state
from ic.config import Settings, get_settings
from ic.graph.client import ping
from ic.mcp_server import mcp_lifespan, mount_mcp
from ic.store import Store

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    llm: Any = AUTO,
    store: Store | None = None,
    recorded_path: Path | None = None,
) -> FastAPI:
    """``llm``: AUTO (a client when an API key is configured, else None), None (always the
    deterministic fallback), or an ``LLMClient``. State (SQLite store, limits, run
    registry) is built on startup or on the first request, never at import."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = app_state(app)  # Store.init() and the rest
        if not await ping():
            log.warning("FalkorDB is not reachable at %s", settings.falkordb_url)
        task = asyncio.create_task(cleanup_loop(state), name="ic-cleanup")
        try:
            async with mcp_lifespan(mcp_server):
                yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await state.runs.wait_all(limit_s=5)

    app = FastAPI(title="Incident Commander", lifespan=lifespan)
    app.state.ic_factory = lambda: build_state(
        settings, llm=llm, store=store, recorded_path=recorded_path
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    app.include_router(routes.router)
    app.include_router(routes_judge.router)
    app.include_router(routes_challenge.router)
    app.include_router(routes_replay.router)
    app.include_router(routes_storm.router)
    app.include_router(routes_proof.router)
    app.include_router(routes_ask.router)
    mcp_server = mount_mcp(app)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "falkordb": await ping()}

    return app


app = create_app()
