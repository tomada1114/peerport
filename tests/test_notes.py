"""Tests for `peerport.mate.notes` (plain-Markdown storage, #25)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from peerport.errors import NoteNotFoundError, NoteOperationRejectedError
from peerport.mate.notes import (
    NOTE_OPERATIONS,
    NOTE_TOOL_SCHEMAS,
    NotesStore,
    dispatch_note_call,
)

if TYPE_CHECKING:
    from pathlib import Path


def make_store(tmp_path: Path, *, now: datetime | None = None) -> NotesStore:
    fixed_now = now or datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
    return NotesStore(tmp_path / "notes", now=lambda: fixed_now)


class TestCreate:
    def test_writes_md_file_and_returns_note_id(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)

        note_id = store.create("Tide Patterns", "The tides run high this week.")

        assert (tmp_path / "notes" / f"{note_id}.md").exists()
        assert note_id == "tide-patterns"

    def test_duplicate_title_gets_numeric_suffix(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)

        first = store.create("Tide Patterns", "First.")
        second = store.create("Tide Patterns", "Second.")

        assert first != second
        assert second == "tide-patterns-2"
        assert store.read(first) != store.read(second)


class TestAppend:
    def test_appends_content_and_advances_updated_timestamp(
        self, tmp_path: Path
    ) -> None:
        before = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
        after = datetime(2026, 7, 27, 9, 0, 0, tzinfo=UTC)
        store = make_store(tmp_path, now=before)
        note_id = store.create("Tide Patterns", "Original text.")
        first_summary = store.list_notes()[0]

        store.now = lambda: after
        store.append(note_id, "Update: spring tides confirmed weekly.")

        content = store.read(note_id)
        assert "Original text." in content
        assert "Update: spring tides confirmed weekly." in content
        second_summary = store.list_notes()[0]
        assert second_summary.updated_date != first_summary.updated_date
        assert second_summary.note_id == note_id

    def test_append_to_missing_note_raises_not_found(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)

        with pytest.raises(NoteNotFoundError):
            store.append("does-not-exist", "text")


class TestRead:
    def test_read_matches_created_content(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        note_id = store.create("Tide Patterns", "The tides run high this week.")

        content = store.read(note_id)

        assert "The tides run high this week." in content
        assert "<!--" not in content

    def test_read_missing_note_raises_not_found(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)

        with pytest.raises(NoteNotFoundError):
            store.read("does-not-exist")


class TestList:
    def test_returns_title_date_summary_per_note(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create("Tide Patterns", "The tides run high this week.\nMore detail.")

        summaries = store.list_notes()

        assert len(summaries) == 1
        assert summaries[0].title == "Tide Patterns"
        assert summaries[0].summary == "The tides run high this week."
        assert summaries[0].updated_date

    def test_filter_matches_title_substring(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create("Tide Patterns", "body")
        store.create("Fishing Spots", "body")

        summaries = store.list_notes(filter_text="tide")

        assert [s.title for s in summaries] == ["Tide Patterns"]

    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        assert store.list_notes() == []


class TestSearch:
    def test_matches_content_case_insensitively(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create("Tide Patterns", "Spring tides peak on Tuesdays.")

        hits = store.search("TUESDAYS")

        assert len(hits) == 1
        assert hits[0].title == "Tide Patterns"
        assert "Tuesdays" in hits[0].snippet

    def test_zero_matches_returns_empty_list(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create("Tide Patterns", "Spring tides peak on Tuesdays.")

        assert store.search("shipwrecks") == []


class TestDelete:
    def test_removes_the_note_file(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        note_id = store.create("Tide Patterns", "body")

        store.delete(note_id)

        assert store.list_notes() == []

    def test_delete_missing_note_is_a_noop(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.delete("does-not-exist")  # must not raise


class TestNoteToolSchemas:
    def test_exactly_five_operations_no_delete(self) -> None:
        names = {schema["name"] for schema in NOTE_TOOL_SCHEMAS}
        assert names == set(NOTE_OPERATIONS)
        assert len(NOTE_TOOL_SCHEMAS) == 5
        assert "delete" not in names


class TestDispatchNoteCall:
    def test_create_dispatches_to_store(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)

        result = dispatch_note_call(
            store, "create", {"title": "Tide Patterns", "content_markdown": "body"}
        )

        note_id = result["note_id"]
        assert isinstance(note_id, str)
        assert store.read(note_id) is not None

    def test_list_dispatches_to_store(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create("Tide Patterns", "body")

        result = dispatch_note_call(store, "list", {})

        notes = result["notes"]
        assert isinstance(notes, list)
        assert len(notes) == 1

    @pytest.mark.parametrize("name", ["delete", "remove_note", "delete_note"])
    def test_delete_like_calls_are_rejected(self, tmp_path: Path, name: str) -> None:
        store = make_store(tmp_path)
        note_id = store.create("Tide Patterns", "body")

        with pytest.raises(NoteOperationRejectedError):
            dispatch_note_call(store, name, {"note_id": note_id})

        assert store.read(note_id) is not None

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("create", {}),
            ("create", {"title": "Tide Patterns"}),  # missing content_markdown
            ("append", {}),
            ("read", {}),
            ("search", {}),
        ],
    )
    def test_missing_required_argument_is_rejected_not_a_key_error(
        self, tmp_path: Path, name: str, arguments: dict[str, object]
    ) -> None:
        """A malformed/empty tool-call payload must degrade gracefully.

        `_extract_tool_calls` defaults unparsable tool-call JSON to `{}`
        (llm/client.py); dispatching that used to raise a bare KeyError
        that crashed the whole Mate chat turn instead of being reported
        like every other rejected operation.
        """
        store = make_store(tmp_path)

        with pytest.raises(NoteOperationRejectedError):
            dispatch_note_call(store, name, arguments)
