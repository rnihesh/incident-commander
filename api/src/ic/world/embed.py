"""Text embedders (KTD22): a deterministic local hasher and a cached OpenAI client."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Protocol

from ic.config import Settings, get_settings

CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "embeddings"


class Embedder(Protocol):
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Feature hashing of unigrams and bigrams, L2-normalised. Offline and deterministic."""

    def __init__(self, dims: int = 512):
        self.dims = dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9_]+", text.lower())
        vec = [0.0] * self.dims
        for token in [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))]:
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[h % self.dims] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAIEmbedder:
    """OpenAI embeddings with an on-disk cache so re-seeding is offline after the first run."""

    def __init__(self, settings: Settings, cache_dir: Path = CACHE_DIR):
        from openai import OpenAI

        self.model, self.dims = settings.embedding_model, settings.embedding_dims
        self.cache_dir = cache_dir
        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    def _path(self, text: str) -> Path:
        key = hashlib.sha256(f"{self.model}|{self.dims}|{text}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out: dict[int, list[float]] = {}
        missing = []
        for i, t in enumerate(texts):
            p = self._path(t)
            if p.exists():
                out[i] = json.loads(p.read_text())
            else:
                missing.append(i)
        for start in range(0, len(missing), 64):
            batch = missing[start : start + 64]
            resp = self._client.embeddings.create(
                model=self.model, input=[texts[i] for i in batch], dimensions=self.dims
            )
            for i, item in zip(batch, resp.data, strict=True):
                out[i] = item.embedding
                self._path(texts[i]).write_text(json.dumps(item.embedding))
        return [out[i] for i in range(len(texts))]


def select_embedder(settings: Settings) -> Embedder:
    """``embedding_provider``: ``hashing``, ``openai``, or ``auto`` (OpenAI embeddings only when
    the LLM key is an OpenAI key; other providers' keys never reach OpenAI's endpoint). Query
    and index must use the same embedder, so re-run ``ic seed`` after changing it."""
    mode = settings.embedding_provider.lower()
    if mode == "openai" or (
        mode == "auto" and settings.llm_api_key and settings.llm_provider.lower() == "openai"
    ):
        return OpenAIEmbedder(settings)
    return HashingEmbedder(settings.embedding_dims)


def get_embedder() -> Embedder:
    return select_embedder(get_settings())
