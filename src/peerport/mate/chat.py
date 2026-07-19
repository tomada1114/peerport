"""Keeper↔Mate streaming chat (#18, extended by #24/#25).

`POST /api/chat` lands here; the Mate reply streams back over WebSocket
as `chat_delta` frames followed by `chat_done`. Each completed exchange
is summarized by the background role into a `conversation` memory with
the +2 Keeper importance bias (requirements §4.4). The Mate always
answers regardless of its in-world state — the Bridge line is always
open by design, so no busy-gating exists anywhere in this path.

Before the visible streamed reply, one bounded, non-streaming
`call_with_tools` round offers Mate's 5 Notes operations
(`NOTE_TOOL_SCHEMAS`); any tool call the model makes is dispatched and
folded into the reply prompt as context. This is a deliberate departure
from OpenAI's native multi-turn function-calling loop (structured
conversation items threaded back and forth) — this codebase's prompts
are always plain strings (`PromptParts`), so a tool result is instead
appended as prompt text for the one follow-up call, avoiding a second,
larger protocol change across the whole `llm/client.py` module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from peerport.errors import NoteNotFoundError, NoteOperationRejectedError
from peerport.llm.client import PromptParts, call_stream
from peerport.mate.notes import NOTE_TOOL_SCHEMAS, dispatch_note_call
from peerport.mate.research import digest_of, exceeds_threshold, title_from_request
from peerport.memory.recall import retrieve

if TYPE_CHECKING:
    from collections.abc import Callable

    from peerport.llm.client import LLMClient, ToolCall
    from peerport.mate.notes import NotesStore
    from peerport.memory.stream import MemoryStream
    from peerport.server.state import Broadcaster

logger = logging.getLogger(__name__)

KEEPER_BIAS = 2
NOTE_ACTION_KINDS = ("create", "append")

SUMMARY_INSTRUCTIONS = (
    "Summarize the exchange below between you and your Keeper in 1-2 "
    "sentences, from your own point of view, for your memory."
)

NOTE_TOOL_INSTRUCTIONS = (
    "You may optionally use one of your note tools if the Keeper's "
    "message calls for it (e.g. filing new research, appending an "
    "update, reading back a note, listing what's on file, or searching "
    "past notes). If no note action is needed, don't call any tool."
)


@dataclass(slots=True)
class MateChat:
    """The Mate chat pipeline: retrieve, stream, summarize, remember."""

    llm: LLMClient
    memory: MemoryStream
    broadcaster: Broadcaster
    notes: NotesStore
    mate_id: str
    fixed_prefix: str
    now_world: Callable[[], int]

    async def handle(self, text: str) -> None:
        """Process one Keeper message end to end."""
        memories = await retrieve(
            self.memory, peer_id=self.mate_id, query=text, now_world=self.now_world()
        )
        recalled = "\n".join(f"- {m.text}" for m in memories)
        tool_context = await self._maybe_use_note_tools(text, recalled)
        variable = (
            f"Memories that came to mind:\n{recalled}\n\n"
            f"Your Keeper says: {text}\n"
            f"{tool_context}"
            "Reply in character, directly to your Keeper."
        )
        # "Mate is searching" flavor signal (REQ-003): fired before the
        # call since search necessity is the model's own judgment call,
        # resolved server-side inside this same streaming request; the
        # frontend shows the flavor line until the first chat_delta.
        await self.broadcaster.publish({"t": "event", "kind": "search"})
        result = await call_stream(
            self.llm,
            role="mate",
            prompt=PromptParts(self.fixed_prefix, variable),
            on_delta=self._send_delta,
            purpose="chat",
        )
        reply_text = result.text or ""
        chat_text, filed_title = await self._maybe_file_report(text, reply_text)
        await self.broadcaster.publish(
            {"t": "chat_done", "text": chat_text, "filed_note_title": filed_title}
        )
        if reply_text:
            await self._summarize(text, reply_text)

    async def _maybe_file_report(
        self, keeper_text: str, reply_text: str
    ) -> tuple[str, str | None]:
        """Auto-file long write-ups to Notes (REQ-005/006/007)."""
        if not exceeds_threshold(reply_text):
            return reply_text, None
        title = title_from_request(keeper_text)
        note_id = self.notes.create(title, reply_text)
        await self._remember_note_action(note_id)
        return digest_of(reply_text), title

    async def _send_delta(self, delta: str) -> None:
        await self.broadcaster.publish({"t": "chat_delta", "text": delta})

    async def _maybe_use_note_tools(self, text: str, recalled: str) -> str:
        variable = (
            f"{NOTE_TOOL_INSTRUCTIONS}\n\nMemories that came to mind:\n{recalled}\n\n"
            f"Your Keeper says: {text}"
        )
        result = await self.llm.call_with_tools(
            role="mate",
            prompt=PromptParts(self.fixed_prefix, variable),
            tools=NOTE_TOOL_SCHEMAS,
            purpose="notes_tool",
        )
        if not result.tool_calls:
            return ""
        lines = [await self._dispatch_one(call) for call in result.tool_calls]
        return f"{chr(10).join(lines)}\n\n"

    async def _dispatch_one(self, call: ToolCall) -> str:
        try:
            output = dispatch_note_call(self.notes, call.name, call.arguments)
        except NoteOperationRejectedError:
            logger.warning("Mate attempted a rejected note operation: %s", call.name)
            return f"[note tool {call.name} rejected: not a supported operation]"
        except NoteNotFoundError as error:
            return f"[note tool {call.name} error: {error}]"
        if call.name in NOTE_ACTION_KINDS:
            note_id = output.get("note_id") or call.arguments.get("note_id")
            if isinstance(note_id, str):
                await self._remember_note_action(note_id)
        return f"[note tool {call.name} result: {output}]"

    async def _remember_note_action(self, note_id: str) -> None:
        match = next((s for s in self.notes.list_notes() if s.note_id == note_id), None)
        if match is None:
            return
        await self.memory.write(
            peer_id=self.mate_id,
            ts_world=self.now_world(),
            kind="keeper_note",
            text=f'Filed a note: "{match.title}" — {match.summary}',
        )

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
