"""Keeper↔Mate streaming chat (#18).

`POST /api/chat` lands here; the Mate reply streams back over WebSocket
as `chat_delta` frames followed by `chat_done`. Each completed exchange
is summarized by the background role into a `conversation` memory with
the +2 Keeper importance bias (requirements §4.4). The Mate always
answers regardless of its in-world state — the Bridge line is always
open by design, so no busy-gating exists anywhere in this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from peerport.llm.client import PromptParts, call_stream
from peerport.memory.recall import retrieve

if TYPE_CHECKING:
    from collections.abc import Callable

    from peerport.llm.client import LLMClient
    from peerport.memory.stream import MemoryStream
    from peerport.server.state import Broadcaster

KEEPER_BIAS = 2

SUMMARY_INSTRUCTIONS = (
    "Summarize the exchange below between you and your Keeper in 1-2 "
    "sentences, from your own point of view, for your memory."
)


@dataclass(slots=True)
class MateChat:
    """The Mate chat pipeline: retrieve, stream, summarize, remember."""

    llm: LLMClient
    memory: MemoryStream
    broadcaster: Broadcaster
    mate_id: str
    fixed_prefix: str
    now_world: Callable[[], int]

    async def handle(self, text: str) -> None:
        """Process one Keeper message end to end."""
        memories = await retrieve(
            self.memory, peer_id=self.mate_id, query=text, now_world=self.now_world()
        )
        recalled = "\n".join(f"- {m.text}" for m in memories)
        variable = (
            f"Memories that came to mind:\n{recalled}\n\n"
            f"Your Keeper says: {text}\n"
            "Reply in character, directly to your Keeper."
        )
        result = await call_stream(
            self.llm,
            role="mate",
            prompt=PromptParts(self.fixed_prefix, variable),
            on_delta=self._send_delta,
            purpose="chat",
        )
        reply_text = result.text or ""
        await self.broadcaster.publish({"t": "chat_done", "text": reply_text})
        if reply_text:
            await self._summarize(text, reply_text)

    async def _send_delta(self, delta: str) -> None:
        await self.broadcaster.publish({"t": "chat_delta", "text": delta})

    async def _summarize(self, keeper_text: str, mate_text: str) -> None:
        summary = await self.llm.call(
            role="background",
            prompt=PromptParts(
                self.fixed_prefix,
                f"{SUMMARY_INSTRUCTIONS}\n\nKeeper: {keeper_text}\nYou: {mate_text}",
            ),
            purpose="summarize",
        )
        if not summary.text:
            return
        await self.memory.write(
            peer_id=self.mate_id,
            ts_world=self.now_world(),
            kind="conversation",
            text=summary.text,
        )
        # Keeper-involved memories carry the +2 importance bias (§4.4).
        await self.memory.score_pending_importance(
            self.llm, self.mate_id, bias=KEEPER_BIAS
        )
