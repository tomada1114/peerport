"""Web search flow + report filing (requirements.md §4.5, #24).

Mate's chat turns already offer the hosted `web_search` tool
unconditionally (`llm/client.py::MATE_TOOLS`), letting the model judge
search necessity itself (REQ-001/002) — OpenAI resolves the search
server-side and the model's own prose is all this codebase ever sees,
so raw fetched page content never touches memory or Notes (REQ-009) by
construction. This module owns what happens once that reply text comes
back: auto-filing long write-ups to Notes past a word-count threshold
(REQ-005/006) and guessing a short title for the filed note.
"""

from __future__ import annotations

import re

WORD_THRESHOLD = 300
DIGEST_SENTENCE_COUNT = 2
TITLE_MAX_LENGTH = 80
DEFAULT_TITLE = "Research Notes"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def word_count(text: str) -> int:
    """Word count per REQ-005/006's stated measure: `len(text.split())`."""
    return len(text.split())


def exceeds_threshold(text: str) -> bool:
    """Whether *text* exceeds the 300-word inline-vs-file threshold."""
    return word_count(text) > WORD_THRESHOLD


def digest_of(text: str) -> str:
    """A short chat digest: the write-up's first two sentences."""
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    return " ".join(sentences[:DIGEST_SENTENCE_COUNT]).strip()


def title_from_request(keeper_text: str) -> str:
    """A short note title guessed from the Keeper's own request text."""
    cleaned = keeper_text.strip().rstrip("?.!")
    if not cleaned:
        return DEFAULT_TITLE
    title = cleaned[0].upper() + cleaned[1:]
    return title[:TITLE_MAX_LENGTH]
