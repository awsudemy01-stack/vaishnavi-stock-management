"""Embedding helpers — pure maths and packing for the ``embedding`` table (stdlib only).

Vectors are stored as little-endian ``float32`` blobs; similarity is cosine in plain Python (a few
hundred chunks — no vector database needed). ``keyword_score`` is the fallback ranking when the
provider has no embeddings (``provider = none``): token overlap with a small bonus for title hits.
"""

from __future__ import annotations

import math
import re
from array import array
from collections.abc import Sequence

_TOKEN = re.compile(r"[\w\u0900-\u097F]+")
_STOP = frozenset("the a an of to in on for and or is are do i how what where kaise kya hai ka ki ke me my we you it this that with from by as at be can".split())


def pack(vector: Sequence[float]) -> bytes:
    """float list → float32 blob."""
    return array("f", [float(x) for x in vector]).tobytes()


def unpack(blob: bytes) -> list[float]:
    """float32 blob → float list."""
    a = array("f")
    a.frombytes(blob)
    return [float(x) for x in a]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity (0 when either vector is empty or zero)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def tokens(text: str) -> set[str]:
    """Lower-cased word set without stop words."""
    return {t for t in (m.group(0).lower() for m in _TOKEN.finditer(text)) if t not in _STOP and len(t) > 1}


def keyword_score(query: str, title: str, content: str) -> float:
    """Overlap ratio of query tokens with the chunk (title hits count double)."""
    q = tokens(query)
    if not q:
        return 0.0
    t, c = tokens(title), tokens(content)
    hits = sum((2 if w in t else 1) if (w in t or w in c) else 0 for w in q)
    return hits / (2 * len(q))
