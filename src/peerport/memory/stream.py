"""Memory stream writes and batched importance scoring (requirements §4.3).

Every write embeds its text via `text-embedding-3-small` (embedding
failure degrades to a row without a vector rather than blocking), and
importance is scored 1-10 by the background role in one batched call per
peer. Secondhand information goes through the hearsay path, framed
"I heard ..." so it is never conflated with firsthand observation.
"""

from __future__ import annotations

import logging
import struct
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from peerport.errors import InvalidMemoryKindError
from peerport.llm.client import PromptParts
from peerport.llm.prompts import WORLD_RULES, ImportanceScores

if TYPE_CHECKING:
    import sqlite3

    from peerport.llm.client import LLMClient

logger = logging.getLogger(__name__)

MEMORY_KINDS = frozenset(
    {"observation", "conversation", "reflection", "logbook", "keeper_note"}
)
MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 10
HEARSAY_PREFIX = "I heard that"

SCORING_INSTRUCTIONS = (
    "Rate the importance of each numbered memory below for the peer who "
    "holds it, from 1 (mundane) to 10 (life-changing). Return a JSON "
    "object with a `scores` array of integers, one per memory, in order."
)


class Embedder(Protocol):
    """The embedding network boundary; tests inject a fake."""

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for *text*."""
        ...


def pack_embedding(vector: list[float]) -> bytes:
    """Pack a float vector into a compact f32 BLOB (architecture.md §3)."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack an f32 BLOB back into a float list."""
    count = len(blob) // 4
    return [round(value, 6) for value in struct.unpack(f"<{count}f", blob)]


def clamp_importance(raw: int, bias: int = 0) -> int:
    """Clamp a raw importance score (plus optional bias) into 1-10."""
    return max(MIN_IMPORTANCE, min(MAX_IMPORTANCE, raw + bias))


class MemoryStream:
    """Owns all writes into the `memories` table for every peer."""

    def __init__(self, conn: sqlite3.Connection, embedder: Embedder) -> None:
        """Bind the stream to a database and an embedding boundary."""
        self.conn = conn
        self.embedder = embedder

    async def write(
        self,
        *,
        peer_id: str,
        ts_world: int,
        kind: str,
        text: str,
        importance: int | None = None,
    ) -> int:
        """Write one memory row, embedding its text.

        Args:
            peer_id: Owning peer.
            ts_world: World-clock timestamp of the event.
            kind: One of `MEMORY_KINDS`.
            text: The memory text.
            importance: Pre-scored importance; `None` leaves the row
                pending for the next `score_pending_importance()` batch.

        Returns:
            The new row id.

        Raises:
            InvalidMemoryKindError: If *kind* is not an allowed value.
        """
        if kind not in MEMORY_KINDS:
            message = f"invalid memory kind: {kind!r} (expected one of {sorted(MEMORY_KINDS)})"
            raise InvalidMemoryKindError(message)
        embedding: bytes | None = None
        try:
            embedding = pack_embedding(await self.embedder.embed(text))
        except Exception:
            # Broad by design (degrade-to-no-embedding is a sanctioned
            # boundary, see module docstring) -- but logger.exception
            # keeps the traceback so a real bug isn't indistinguishable
            # from a transient embedding-API outage in the logs.
            logger.exception("embedding failed; storing memory without vector")
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO memories (peer_id, ts_world, ts_real, kind, text,"
                " importance, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    peer_id,
                    ts_world,
                    int(datetime.now(UTC).timestamp()),
                    kind,
                    text,
                    importance,
                    embedding,
                ),
            )
        return int(cursor.lastrowid or 0)

    async def write_hearsay(self, *, peer_id: str, ts_world: int, fact: str) -> int:
        """Write secondhand information framed as hearsay (requirements §4.3)."""
        return await self.write(
            peer_id=peer_id,
            ts_world=ts_world,
            kind="conversation",
            text=f"{HEARSAY_PREFIX} {fact}.",
        )

    async def score_pending_importance(
        self, llm: LLMClient, peer_id: str, bias: int = 0
    ) -> int:
        """Score all pending memories for a peer in one batched call.

        Returns:
            The number of memories scored (0 when none were pending or
            the call was skipped).
        """
        pending = self.conn.execute(
            "SELECT id, text FROM memories"
            " WHERE peer_id = ? AND importance IS NULL ORDER BY id",
            (peer_id,),
        ).fetchall()
        if not pending:
            return 0
        listing = "\n".join(f"{i + 1}. {text}" for i, (_, text) in enumerate(pending))
        result = await llm.call(
            role="background",
            prompt=PromptParts(WORLD_RULES, f"{SCORING_INSTRUCTIONS}\n\n{listing}"),
            schema=ImportanceScores,
            purpose="score",
        )
        if result.skipped or not isinstance(result.parsed, ImportanceScores):
            return 0
        scores = result.parsed.scores
        with self.conn:
            for (memory_id, _), raw in zip(pending, scores, strict=False):
                self.conn.execute(
                    "UPDATE memories SET importance = ? WHERE id = ?",
                    (clamp_importance(raw, bias), memory_id),
                )
        return min(len(pending), len(scores))


EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIEmbedder:  # pragma: no cover - the real network boundary
    """`text-embedding-3-small` embedder over the OpenAI SDK."""

    def __init__(self) -> None:
        """Create the SDK client (reads OPENAI_API_KEY from the env)."""
        from openai import AsyncOpenAI  # noqa: PLC0415 - heavy import kept lazy

        self._client = AsyncOpenAI()

    async def embed(self, text: str) -> list[float]:
        """Embed one text; SDK errors propagate to the degrade path."""
        response = await self._client.embeddings.create(
            model=EMBEDDING_MODEL, input=text
        )
        return list(response.data[0].embedding)
