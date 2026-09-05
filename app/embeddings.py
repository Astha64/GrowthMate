"""
Lightweight deterministic embeddings for GrowthMate (Revision 3).

Isolated behind a tiny vector interface so the real product can move to
Postgres + pgvector without touching callers (ARCHITECTURE §7.1 / LLD §7.2):
`embed_text(text) -> list[float]` + `cosine(a, b)`. For the hackathon the
"embeddings" are hashing-trick lexical vectors over word and character-ngram
features — deterministic, dependency-free, and cheap at catalog scale.

These vectors are used for:
  - merchant semantic retrieval (phase 3)
  - semantic external-cache similarity (phase 4)

Never encode mutable price/stock into the embedding; those stay metadata.
"""

import functools
import hashlib
import re
from typing import Sequence

from app.config import EMBEDDING_HASH_DIM

_WORD_RE = re.compile(r"[a-z0-9]+")
_NGRAM_N = 3


@functools.lru_cache(maxsize=4096)
def embed_text(text: str, dim: int = EMBEDDING_HASH_DIM) -> list[float]:
    """Return a deterministic unit vector over lexical features of `text`.

    Uses the hashing trick (feature -> signed bucket) so no vocab is needed,
    then L2-normalizes. Empty input yields a zero vector (cosine = 0).
    """
    if not text:
        return [0.0] * dim
    vec = [0.0] * dim
    tokens = _tokenize(text)
    for tok in tokens:
        h = _sha(tok)
        bucket = h % dim
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[bucket] += sign
    norm = _l2(vec)
    if norm == 0.0:
        return [0.0] * dim
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors. Zero vector => 0.0."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        min_len = min(len(a), len(b))
        a = a[:min_len]
        b = b[:min_len]
    num = sum(x * y for x, y in zip(a, b))
    den = _l2(a) * _l2(b)
    if den == 0.0:
        return 0.0
    return num / den


def _tokenize(text: str) -> list[str]:
    lower = text.lower()
    tokens = _WORD_RE.findall(lower)
    grams: list[str] = list(tokens)
    for token in tokens:
        if len(token) >= _NGRAM_N:
            for i in range(len(token) - _NGRAM_N + 1):
                grams.append(token[i:i + _NGRAM_N])
    if not grams:
        grams = list(lower)
    return grams


def clear_embedding_cache() -> None:
    embed_text.cache_clear()


def _sha(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest(), 16)


def _l2(vec: Sequence[float]) -> float:
    return float(sum(v * v for v in vec) ** 0.5)