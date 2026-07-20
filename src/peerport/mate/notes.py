"""Notes storage: plain `data/notes/*.md` files (requirements §4.5, #25).

Intentionally no DB table — notes are files, full stop. `create`,
`append`, `read`, `list`, and `search` are the only 5 operations Mate's
function-calling schema exposes (`NOTE_TOOL_SCHEMAS`); deleting a note is
a separate Keeper-only path (`NotesStore.delete`) never offered to Mate,
so `dispatch_note_call` structurally cannot route a delete-style call.

Each note file's first line is an internal `<!-- updated: ISO8601 -->`
comment updated by `create`/`append` via an injectable clock (mirroring
`logbook.py`/`friends/mail.py`'s `now_world`/`now_real` pattern) rather
than filesystem mtime, whose coarse resolution would make "did the
timestamp change" flaky in fast tests.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from peerport.errors import NoteNotFoundError, NoteOperationRejectedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SNIPPET_RADIUS = 60
NOTE_OPERATIONS = ("create", "append", "read", "list", "search")
_UPDATED_COMMENT_RE = re.compile(r"^<!-- updated: (.+?) -->$")


@dataclass(frozen=True, slots=True)
class NoteSummary:
    """One row for the Notes tab list view / Mate's `list` operation.

    `filed_by_mate` is always `True`: every note originates from Mate's
    `create` operation (there is no Keeper-facing "new note" flow), so
    the "filed by {mate}" marker (REQ-012) applies to every note.
    """

    note_id: str
    title: str
    updated_date: str
    summary: str
    filed_by_mate: bool = True


@dataclass(frozen=True, slots=True)
class NoteSearchHit:
    """One row for Mate's `search` operation."""

    note_id: str
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class NoteDetail:
    """Full note record for the Notes tab detail/edit view."""

    note_id: str
    title: str
    content_markdown: str
    updated_date: str
    filed_by_mate: bool = True


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "note"


def _strip_updated_comment(text: str) -> tuple[str, str]:
    """Split off the leading `<!-- updated: ... -->` line, if present."""
    lines = text.splitlines()
    if lines and _UPDATED_COMMENT_RE.match(lines[0].strip()):
        match = _UPDATED_COMMENT_RE.match(lines[0].strip())
        timestamp = match.group(1) if match else ""
        return timestamp, "\n".join(lines[1:]).lstrip("\n")
    return "", text


def _title_of(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return "Untitled"


def _summary_of(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _snippet_of(body: str, needle: str) -> str:
    lower = body.lower()
    idx = lower.find(needle.lower())
    if idx == -1:
        return ""
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(body), idx + len(needle) + SNIPPET_RADIUS)
    return body[start:end].strip()


class NotesStore:
    """Owns all reads/writes to `data/notes/*.md`."""

    def __init__(
        self, notes_dir: Path, now: Callable[[], datetime] | None = None
    ) -> None:
        """Bind the store to a directory, creating it if absent."""
        self.notes_dir = notes_dir
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))

    def create(self, title: str, content_markdown: str) -> str:
        """Write a new note; a duplicate title gets a numeric suffix."""
        slug = _slugify(title)
        note_id = slug
        suffix = 2
        while self._path(note_id).exists():
            note_id = f"{slug}-{suffix}"
            suffix += 1
        body = f"# {title}\n\n{content_markdown}\n"
        self._write(note_id, body)
        return note_id

    def append(self, note_id: str, content_markdown: str) -> None:
        """Append Markdown content to an existing note.

        Raises:
            NoteNotFoundError: If *note_id* does not exist.
        """
        path = self._path(note_id)
        if not path.exists():
            message = f"note not found: {note_id!r}"
            raise NoteNotFoundError(message)
        _, body = _strip_updated_comment(path.read_text(encoding="utf-8"))
        self._write(note_id, f"{body}\n{content_markdown}\n")

    def read(self, note_id: str) -> str:
        """Full Markdown content of one note (metadata comment stripped).

        Raises:
            NoteNotFoundError: If *note_id* does not exist.
        """
        path = self._path(note_id)
        if not path.exists():
            message = f"note not found: {note_id!r}"
            raise NoteNotFoundError(message)
        _, body = _strip_updated_comment(path.read_text(encoding="utf-8"))
        return body

    def list_notes(self, filter_text: str | None = None) -> list[NoteSummary]:
        """All notes, newest-updated first."""
        summaries = [self._summarize(path) for path in self.notes_dir.glob("*.md")]
        if filter_text:
            needle = filter_text.lower()
            summaries = [s for s in summaries if needle in s.title.lower()]
        return sorted(summaries, key=lambda s: s.updated_date, reverse=True)

    def search(self, query: str) -> list[NoteSearchHit]:
        """Notes whose title or content contains *query* (case-insensitive)."""
        hits = []
        for path in sorted(self.notes_dir.glob("*.md")):
            _, body = _strip_updated_comment(path.read_text(encoding="utf-8"))
            if query.lower() in body.lower():
                hits.append(
                    NoteSearchHit(
                        note_id=path.stem,
                        title=_title_of(body),
                        snippet=_snippet_of(body, query),
                    )
                )
        return hits

    def delete(self, note_id: str) -> None:
        """Remove one note file. Keeper-only; never exposed to Mate."""
        self._path(note_id).unlink(missing_ok=True)

    def read_detail(self, note_id: str) -> NoteDetail | None:
        """Full note record for the Notes tab detail view, or `None`."""
        path = self._path(note_id)
        if not path.exists():
            return None
        timestamp, body = _strip_updated_comment(path.read_text(encoding="utf-8"))
        return NoteDetail(
            note_id=note_id,
            title=_title_of(body),
            content_markdown=body,
            updated_date=timestamp,
        )

    def _path(self, note_id: str) -> Path:
        return self.notes_dir / f"{note_id}.md"

    def _write(self, note_id: str, body: str) -> None:
        timestamp = self.now().isoformat()
        self._path(note_id).write_text(
            f"<!-- updated: {timestamp} -->\n{body}", encoding="utf-8"
        )

    def _summarize(self, path: Path) -> NoteSummary:
        timestamp, body = _strip_updated_comment(path.read_text(encoding="utf-8"))
        return NoteSummary(
            note_id=path.stem,
            title=_title_of(body),
            updated_date=timestamp,
            summary=_summary_of(body),
        )


NOTE_TOOL_SCHEMAS: list[dict[str, object]] = [
    {
        "type": "function",
        "name": "create",
        "description": "Create a new note with a title and Markdown content.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content_markdown": {"type": "string"},
            },
            "required": ["title", "content_markdown"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "append",
        "description": "Append Markdown content to an existing note.",
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {"type": "string"},
                "content_markdown": {"type": "string"},
            },
            "required": ["note_id", "content_markdown"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read",
        "description": "Read the full Markdown content of one note.",
        "parameters": {
            "type": "object",
            "properties": {"note_id": {"type": "string"}},
            "required": ["note_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list",
        "description": (
            "List notes (title, updated date, summary), optionally "
            "filtered by a title substring."
        ),
        "parameters": {
            "type": "object",
            "properties": {"filter": {"type": ["string", "null"]}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search",
        "description": "Search note titles and content for a query string.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def dispatch_note_call(
    store: NotesStore, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Execute one Mate-invoked note operation.

    Raises:
        NoteOperationRejectedError: If *name* is not one of the 5
            operations in `NOTE_OPERATIONS` (in particular, any
            delete-like call), or if *arguments* is missing a key one
            of those operations requires (e.g. malformed tool-call JSON
            that `_extract_tool_calls` already had to default to `{}`).
    """
    try:
        return _dispatch_known_call(store, name, arguments)
    except KeyError as error:
        message = f"note operation {name!r} missing required argument: {error}"
        raise NoteOperationRejectedError(message) from error


def _dispatch_known_call(
    store: NotesStore, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    if name == "create":
        note_id = store.create(
            str(arguments["title"]), str(arguments["content_markdown"])
        )
        return {"note_id": note_id}
    if name == "append":
        store.append(str(arguments["note_id"]), str(arguments["content_markdown"]))
        return {"ok": True}
    if name == "read":
        return {"content_markdown": store.read(str(arguments["note_id"]))}
    if name == "list":
        filter_text = arguments.get("filter")
        notes = store.list_notes(str(filter_text) if filter_text else None)
        return {"notes": [asdict(note) for note in notes]}
    if name == "search":
        hits = store.search(str(arguments["query"]))
        return {"results": [asdict(hit) for hit in hits]}
    message = f"unsupported note operation: {name!r}"
    raise NoteOperationRejectedError(message)
