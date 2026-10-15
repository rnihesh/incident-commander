"""Fixtures for the HTTP tests: one app per module on a temp SQLite store, real FalkorDB,
no LLM key (deterministic fallback), mocked GitHub/Discord (no tokens configured)."""

from __future__ import annotations

import pytest
from api_helpers import SECRET, cleanup_store_graphs, test_settings

from ic.api import routes
from ic.api.app import create_app
from ic.graph import client
from ic.store import Store


@pytest.fixture(scope="module")
async def ic_app(tmp_path_factory):
    client.reset_client()
    tmp = tmp_path_factory.mktemp("api")
    store = Store(tmp / "ic.sqlite3")
    app = create_app(test_settings(tmp), llm=None, store=store)
    state = routes.app_state(app)
    yield app
    await state.runs.wait_all(limit_s=60)
    await cleanup_store_graphs(store)
    client.reset_client()


@pytest.fixture(scope="module")
def ic_state(ic_app) -> routes.AppState:
    return routes.app_state(ic_app)


@pytest.fixture
def secret() -> str:
    return SECRET
