"""3-axis memory retrieval: recency + importance + relevance (§4.3).

Score = weighted sum of exponential recency decay over world time,
normalized importance, and cosine similarity between the query embedding
and each memory's stored embedding. When the query embedding cannot be
produced, retrieval degrades to the two non-semantic axes instead of
failing (requirements §4.3 edge case).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from peerport.memory.stream import unpack_embedding

if TYPE_CHECKING:
    from peerport.memory.stream import MemoryStream

logger = logging.getLogger(__name__)

TOP_K = 10
RECENCY_TAU_WORLD_SECONDS = 7200.0  # one default world day
WEIGHT_RECENCY = 1.0
WEIGHT_IMPORTANCE = 1.0
WEIGHT_RELEVANCE = 1.0
DEFAULT_IMPORTANCE = 5


@dataclass(frozen=True, slots=True)
class RecallResult:
    """One retrieved memory with its computed score."""

    memory_id: int
    text: str
    kind: str
    importance: int
    score: float
    relevance_used: bool


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-python cosine similarity (no numpy; pools are tiny)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def retrieve(
    stream: MemoryStream,
    *,
    peer_id: str,
    query: str,
    now_world: int,
    k: int = TOP_K,
) -> list[RecallResult]:
    """Return the top-*k* memories for a peer ranked by the 3-axis score."""
    query_vector: list[float] | None = None
    try:
        query_vector = await stream.embedder.embed(query)
    except Exception:
        logger.warning("query embedding failed; falling back to 2-axis retrieval")

    rows = stream.conn.execute(
        "SELECT id, ts_world, kind, text, importance, embedding"
        " FROM memories WHERE peer_id = ?",
        (peer_id,),
    ).fetchall()

    results = []
    for memory_id, ts_world, kind, text, importance, blob in rows:
        age = max(now_world - (ts_world or 0), 0)
        recency = math.exp(-age / RECENCY_TAU_WORLD_SECONDS)
        weight = (importance or DEFAULT_IMPORTANCE) / 10
        relevance = 0.0
        relevance_used = False
        if query_vector is not None and blob is not None:
            relevance = cosine_similarity(query_vector, unpack_embedding(blob))
            relevance_used = True
        score = (
            WEIGHT_RECENCY * recency
            + WEIGHT_IMPORTANCE * weight
            + WEIGHT_RELEVANCE * relevance
        )
        results.append(
            RecallResult(
                memory_id=memory_id,
                text=text,
                kind=kind,
                importance=importance or DEFAULT_IMPORTANCE,
                score=score,
                relevance_used=relevance_used,
            )
        )
    results.sort(key=lambda result: result.score, reverse=True)
    return results[:k]
