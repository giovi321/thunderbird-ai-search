"""Embedding provider for Thunderbird AI Search."""

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Strip ASCII control chars except \t \n \r. These can crash Ollama's tokenizer.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Defensive cap well below nomic-embed-text's 2048-token context.
# Token-density varies (URLs, code, non-English text), so 3000 chars (~750-1000 tokens)
# is the safe ceiling. Caller's `truncate=true` is a backup.
_MAX_INPUT_CHARS = 3000
# Bisect recursion depth cap. 2^7 = 128, comfortably above default batch size 50.
_MAX_BISECT_DEPTH = 7


def _sanitize_text(t: str) -> str:
    """Make a string safe to send to Ollama /api/embed.

    Removes control bytes, drops lone Unicode surrogates, caps length, and
    replaces empty strings with a placeholder (Ollama rejects empty input).
    """
    if not isinstance(t, str):
        t = str(t) if t is not None else ""
    # Drop lone surrogates / invalid UTF-8 by round-tripping through bytes
    t = t.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    t = _CONTROL_CHAR_RE.sub("", t)
    t = t.strip()[:_MAX_INPUT_CHARS]
    return t if t else "(empty)"


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed a list of texts.

        Returns one entry per input. Entries are vectors for successfully
        embedded inputs and None for inputs the provider rejected (e.g. an
        Ollama 400 isolated to that single input). Callers must handle None.
        """
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensionality of the embedding vectors."""
        ...


class OllamaEmbedding(EmbeddingProvider):
    """Embedding provider using a local Ollama instance."""

    # Known dimensions per model. Fallback: query Ollama on first call.
    _KNOWN_DIMS = {
        "nomic-embed-text": 768,
        "mxbai-embed-large": 1024,
        "all-minilm": 384,
    }

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "nomic-embed-text"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dimensions: int | None = self._KNOWN_DIMS.get(model)
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise RuntimeError(
                "Dimensions unknown. Call embed() once first, or use a known model."
            )
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed texts via Ollama's /api/embed endpoint.

        Resilient to single-input 400s: bisects the batch on failure to isolate
        and skip the offending input(s), returning None for those slots while
        still embedding the rest. Non-400 errors propagate.
        """
        sanitized = [_sanitize_text(t) for t in texts]
        results = await self._embed_or_bisect(sanitized, depth=0)

        if self._dimensions is None:
            for vec in results:
                if vec is not None:
                    self._dimensions = len(vec)
                    logger.info("Detected embedding dimensions: %d", self._dimensions)
                    break

        return results

    async def _embed_or_bisect(
        self,
        texts: list[str],
        depth: int,
    ) -> list[Optional[list[float]]]:
        """POST a batch to Ollama; on 400, bisect to isolate bad inputs."""
        if not texts:
            return []

        resp = await self._client.post(
            "/api/embed",
            json={"model": self.model, "input": texts, "truncate": True},
        )

        if resp.status_code == 200:
            data = resp.json()
            return list(data["embeddings"])

        if resp.status_code == 400:
            body = resp.text[:500]
            if len(texts) == 1:
                # Leaf of the bisect tree. Log full diagnostic and skip this one.
                only = texts[0]
                digest = hashlib.sha1(only.encode("utf-8", errors="ignore")).hexdigest()[:8]
                logger.error(
                    "Ollama rejected single input (len=%d, hash=%s): %r. body: %s",
                    len(only), digest, only[:200], body,
                )
                return [None]
            if depth >= _MAX_BISECT_DEPTH:
                # Stop bisecting; mark everything in this sub-batch as failed.
                logger.error(
                    "Ollama 400 bisect depth cap (%d) reached, skipping %d inputs. body: %s",
                    _MAX_BISECT_DEPTH, len(texts), body,
                )
                return [None] * len(texts)
            mid = len(texts) // 2
            logger.warning(
                "Ollama 400 on batch of %d (depth=%d), bisecting. body: %s",
                len(texts), depth, body,
            )
            left = await self._embed_or_bisect(texts[:mid], depth + 1)
            right = await self._embed_or_bisect(texts[mid:], depth + 1)
            return left + right

        # Non-400 (5xx, 401, network, etc.). Propagate so the indexer can abort the cycle.
        body = resp.text[:500]
        logger.error(
            "Ollama embed failed (%d): %s. input lengths: %s",
            resp.status_code, body, [len(t) for t in texts],
        )
        resp.raise_for_status()
        return []  # unreachable; raise_for_status raises

    async def close(self):
        await self._client.aclose()
