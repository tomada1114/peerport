"""Parse and validate `personas/*.md` into structured Persona records.

Per requirements.md §3.2, persona files are Markdown with YAML-flavored
frontmatter, officially user-editable. Rather than depend on PyYAML (not
a runtime dependency — see architecture.md §8's dependency policy), this
module hand-rolls a minimal frontmatter parser sufficient for the small,
flat `key: value  # comment` shape these files use.

Validation runs at boot: any missing/invalid field aborts startup with
the offending filename and reason (REQ-007), so the rest of the app never
sees a partially-valid persona registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from peerport.errors import PersonaValidationError

if TYPE_CHECKING:
    from pathlib import Path

REQUIRED_FIELDS = ("id", "name", "kind", "pair", "sprite", "activity_interval")
VALID_KINDS = frozenset({"mate", "peer", "friend", "drifter"})
MAP_KINDS = frozenset({"mate", "peer", "drifter"})
SEED_MEMORIES_HEADING = "## Seed memories"

_FRONTMATTER_COMMENT_RE = re.compile(r"\s+#")
_MIN_QUOTED_LENGTH = 2


@dataclass(frozen=True, slots=True)
class Persona:
    """A fully parsed and validated persona definition."""

    id: str
    name: str
    kind: str
    pair: str | None
    sprite: str | None
    activity_interval: int | None
    body: str
    seed_memories: tuple[str, ...]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split raw Markdown into (frontmatter fields, text after frontmatter).

    Args:
        text: Full file content, expected to start with a `---` block.

    Returns:
        A tuple of the parsed frontmatter fields and the remaining text.

    Raises:
        PersonaValidationError: If the opening or closing `---` delimiter
            is missing.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        msg = "missing frontmatter delimiter '---' at start of file"
        raise PersonaValidationError(msg)

    closing_index = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if closing_index is None:
        msg = "missing closing frontmatter delimiter '---'"
        raise PersonaValidationError(msg)

    fields = _parse_frontmatter_fields(lines[1:closing_index])
    rest = "\n".join(lines[closing_index + 1 :])
    return fields, rest


def _parse_frontmatter_fields(lines: list[str]) -> dict[str, Any]:
    """Parse `key: value  # comment` lines into a typed dict."""
    fields: dict[str, Any] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = _FRONTMATTER_COMMENT_RE.split(value, maxsplit=1)[0].strip()
        fields[key.strip()] = _coerce_scalar(value)
    return fields


def _coerce_scalar(value: str) -> Any:
    """Coerce a bare frontmatter token into `None`, `int`, or `str`."""
    if value == "" or value.lower() == "null":
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    if len(value) >= _MIN_QUOTED_LENGTH and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _extract_body(rest: str) -> str:
    """Return the persona-core body, excluding the seed memories section."""
    return rest.split(SEED_MEMORIES_HEADING, 1)[0].strip("\n")


def _extract_seed_memories(rest: str) -> list[str]:
    """Extract bulleted seed memories, joining word-wrapped continuation lines."""
    marker_index = rest.find(SEED_MEMORIES_HEADING)
    if marker_index == -1:
        return []

    memories: list[str] = []
    current: list[str] = []
    for raw_line in rest[marker_index + len(SEED_MEMORIES_HEADING) :].splitlines():
        line = raw_line.strip()
        if line.startswith("<!--"):
            continue
        if line.startswith("- "):
            if current:
                memories.append(" ".join(current))
            current = [line[2:].strip()]
        elif line:
            current.append(line)
    if current:
        memories.append(" ".join(current))
    return memories


def _validate_fields(fields: dict[str, Any]) -> None:
    """Validate required-field presence and kind-conditional constraints.

    Raises:
        PersonaValidationError: On the first validation failure found.
    """
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        msg = f"missing required field(s): {', '.join(missing)}"
        raise PersonaValidationError(msg)

    if not isinstance(fields["id"], str) or not fields["id"]:
        msg = "'id' must be a non-empty string"
        raise PersonaValidationError(msg)
    if not isinstance(fields["name"], str) or not fields["name"]:
        msg = "'name' must be a non-empty string"
        raise PersonaValidationError(msg)

    kind = fields["kind"]
    if kind not in VALID_KINDS:
        msg = f"invalid kind: {kind}"
        raise PersonaValidationError(msg)

    sprite = fields["sprite"]
    activity_interval = fields["activity_interval"]

    if kind == "friend":
        if sprite is not None or activity_interval is not None:
            msg = "friend personas require sprite: null and activity_interval: null"
            raise PersonaValidationError(msg)
    else:
        if sprite is None:
            msg = f"{kind} personas require a non-null sprite"
            raise PersonaValidationError(msg)
        if not isinstance(activity_interval, int):
            msg = f"{kind} personas require a numeric activity_interval"
            raise PersonaValidationError(msg)


def parse_persona_file(path: Path) -> Persona:
    """Parse and validate a single `personas/*.md` file.

    Args:
        path: Path to the persona Markdown file.

    Returns:
        The fully validated `Persona`.

    Raises:
        PersonaValidationError: On any validation failure. The message is
            prefixed with the filename so boot-abort output is actionable.
    """
    text = path.read_text(encoding="utf-8")
    try:
        fields, rest = parse_frontmatter(text)
        _validate_fields(fields)
    except PersonaValidationError as exc:
        msg = f"{path.name}: {exc}"
        raise PersonaValidationError(msg) from exc

    return Persona(
        id=fields["id"],
        name=fields["name"],
        kind=fields["kind"],
        pair=fields["pair"],
        sprite=fields["sprite"],
        activity_interval=fields["activity_interval"],
        body=_extract_body(rest),
        seed_memories=tuple(_extract_seed_memories(rest)),
    )


def load_personas(directory: Path) -> dict[str, Persona]:
    """Parse and validate every `*.md` file in *directory* into a registry.

    Args:
        directory: Path to the `personas/` directory.

    Returns:
        Mapping of persona id -> `Persona`, one entry per `*.md` file.

    Raises:
        SystemExit: If any file fails validation (REQ-007); the message
            names the filename and the specific reason.
    """
    registry: dict[str, Persona] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            persona = parse_persona_file(path)
        except PersonaValidationError as exc:
            raise SystemExit(str(exc)) from exc
        registry[persona.id] = persona
    return registry
