import pytest

from ic.config import get_settings


@pytest.fixture(autouse=True)
def _no_live_llm(request, monkeypatch):
    """A developer's api/.env may hold a real provider key; only `live` tests may use it.
    An empty IC_LLM_API_KEY in the environment outranks the .env file."""
    if request.node.get_closest_marker("live"):
        yield
        return
    for name in ("IC_LLM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("IC_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
