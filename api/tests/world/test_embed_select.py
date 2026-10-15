from ic.config import Settings
from ic.world.embed import HashingEmbedder, OpenAIEmbedder, select_embedder


def _s(**kw) -> Settings:
    base = {"llm_provider": "openai", "llm_api_key": None, "embedding_provider": "auto"}
    return Settings(**{**base, **kw})


def test_auto_uses_openai_only_for_an_openai_key():
    assert isinstance(select_embedder(_s(llm_api_key="k")), OpenAIEmbedder)
    assert isinstance(select_embedder(_s()), HashingEmbedder)


def test_auto_never_sends_a_non_openai_key_to_openai_embeddings():
    for provider in ("anthropic", "gemini", "ollama"):
        assert isinstance(
            select_embedder(_s(llm_provider=provider, llm_api_key="k")), HashingEmbedder
        )


def test_explicit_hashing_wins_over_a_key():
    assert isinstance(
        select_embedder(_s(llm_api_key="k", embedding_provider="hashing")), HashingEmbedder
    )
