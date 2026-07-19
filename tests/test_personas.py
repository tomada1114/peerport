"""Tests for peerport.peers.personas (personas/*.md parsing and validation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from peerport.errors import PersonaValidationError
from peerport.peers.personas import load_personas, parse_frontmatter, parse_persona_file

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_DIR = REPO_ROOT / "personas"

MAP_KINDS = ("mate", "peer", "drifter")


def _write_persona(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


VALID_MATE_MD = """---
id: beacon
name: Beacon
kind: mate
pair: null
sprite: beacon
activity_interval: 75
---

# Persona core

Body text here.

## Seed memories
<!-- Written once to the memory stream at world creation; not injected. -->
- First seed memory.
- Second seed memory.
"""


class TestParseFrontmatter:
    def test_separates_frontmatter_from_body(self) -> None:
        fields, rest = parse_frontmatter(VALID_MATE_MD)

        assert fields["id"] == "beacon"
        assert fields["kind"] == "mate"
        assert "# Persona core" in rest

    def test_missing_opening_delimiter_raises(self) -> None:
        with pytest.raises(PersonaValidationError):
            parse_frontmatter("id: beacon\n---\nbody")

    def test_missing_closing_delimiter_raises(self) -> None:
        with pytest.raises(PersonaValidationError):
            parse_frontmatter("---\nid: beacon\nbody with no closing delimiter")


class TestParsePersonaFileRealFixtures:
    """These exercise the actual repo personas/*.md files directly."""

    def test_beacon_frontmatter_fields(self) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "beacon.md")

        assert persona.id == "beacon"
        assert persona.name == "Beacon"
        assert persona.kind == "mate"
        assert persona.pair is None
        assert persona.sprite == "beacon"
        assert persona.activity_interval == 75

    def test_beacon_body_starts_with_persona_core_and_excludes_seed_memories(
        self,
    ) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "beacon.md")

        assert persona.body.startswith("# Persona core")
        assert "## Seed memories" not in persona.body

    def test_beacon_seed_memories_extracted_exactly(self) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "beacon.md")

        assert len(persona.seed_memories) == 2
        assert persona.seed_memories[0].startswith(
            "I docked in at this port the day the lighthouse first lit."
        )
        assert persona.seed_memories[1] == (
            "I promised myself the Keeper would never come back to a dark harbor."
        )

    def test_tug_frontmatter_fields(self) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "tug.md")

        assert persona.id == "tug"
        assert persona.kind == "peer"
        assert persona.pair == "kai"
        assert persona.sprite == "tug"
        assert persona.activity_interval == 90

    def test_bell_frontmatter_fields(self) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "bell.md")

        assert persona.id == "bell"
        assert persona.kind == "peer"
        assert persona.pair == "mia"
        assert persona.activity_interval == 100

    def test_echo_frontmatter_fields(self) -> None:
        persona = parse_persona_file(PERSONAS_DIR / "echo.md")

        assert persona.id == "echo"
        assert persona.kind == "drifter"
        assert persona.pair is None
        assert persona.activity_interval == 120

    @pytest.mark.parametrize("filename", ["kai.md", "mia.md"])
    def test_friend_personas_parse_with_null_sprite_and_interval(
        self, filename: str
    ) -> None:
        persona = parse_persona_file(PERSONAS_DIR / filename)

        assert persona.kind == "friend"
        assert persona.sprite is None
        assert persona.activity_interval is None

    @pytest.mark.parametrize(
        "filename", ["beacon.md", "tug.md", "bell.md", "echo.md", "kai.md", "mia.md"]
    )
    def test_all_six_real_files_parse_with_two_seed_memories(
        self, filename: str
    ) -> None:
        persona = parse_persona_file(PERSONAS_DIR / filename)

        assert len(persona.seed_memories) == 2


class TestLoadPersonasRealDirectory:
    def test_registry_has_exactly_six_entries(self) -> None:
        registry = load_personas(PERSONAS_DIR)

        assert set(registry) == {"beacon", "tug", "bell", "echo", "kai", "mia"}

    def test_beacon_registry_entry_matches_expected_values(self) -> None:
        registry = load_personas(PERSONAS_DIR)

        assert registry["beacon"].activity_interval == 75
        assert registry["beacon"].sprite == "beacon"
        assert len(registry["beacon"].seed_memories) == 2


class TestValidationFailures:
    def test_invalid_kind_enum_aborts_with_filename_and_reason(
        self, tmp_path: Path
    ) -> None:
        content = VALID_MATE_MD.replace("kind: mate", "kind: villager")
        path = _write_persona(tmp_path, "echo.md", content)

        with pytest.raises(PersonaValidationError, match=r"echo\.md"):
            parse_persona_file(path)

        with pytest.raises(PersonaValidationError, match="villager"):
            parse_persona_file(path)

    @pytest.mark.parametrize("kind", MAP_KINDS)
    def test_null_sprite_fails_for_map_kinds(self, tmp_path: Path, kind: str) -> None:
        content = VALID_MATE_MD.replace("kind: mate", f"kind: {kind}").replace(
            "sprite: beacon", "sprite: null"
        )
        path = _write_persona(tmp_path, "test.md", content)

        with pytest.raises(PersonaValidationError):
            parse_persona_file(path)

    def test_null_activity_interval_fails_for_peer(self, tmp_path: Path) -> None:
        content = VALID_MATE_MD.replace("kind: mate", "kind: peer").replace(
            "activity_interval: 75", "activity_interval: null"
        )
        path = _write_persona(tmp_path, "tug.md", content)

        with pytest.raises(PersonaValidationError, match=r"tug\.md"):
            parse_persona_file(path)

    def test_friend_with_non_null_sprite_is_rejected(self, tmp_path: Path) -> None:
        content = VALID_MATE_MD.replace("kind: mate", "kind: friend").replace(
            "activity_interval: 75", "activity_interval: null"
        )
        path = _write_persona(tmp_path, "kai.md", content)

        with pytest.raises(PersonaValidationError):
            parse_persona_file(path)

    def test_missing_required_field_aborts(self, tmp_path: Path) -> None:
        content = VALID_MATE_MD.replace("id: beacon\n", "")
        path = _write_persona(tmp_path, "broken.md", content)

        with pytest.raises(PersonaValidationError, match="id"):
            parse_persona_file(path)

    def test_load_personas_raises_system_exit_on_invalid_file(
        self, tmp_path: Path
    ) -> None:
        _write_persona(
            tmp_path, "echo.md", VALID_MATE_MD.replace("kind: mate", "kind: villager")
        )

        with pytest.raises(SystemExit) as exc_info:
            load_personas(tmp_path)

        assert "echo.md" in str(exc_info.value)
        assert "villager" in str(exc_info.value)
