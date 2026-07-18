# Project Guide

## Overview

This is a Python library built with [uv](https://docs.astral.sh/uv/) and
[hatchling](https://hatch.pypa.io/). It uses a strict `src/` layout with
comprehensive type checking and linting.

## Quick Reference

```bash
just install   # Install dependencies and git hooks when .git/ is present
just fmt       # Format code (ruff check --fix + ruff format)
just lint      # Lint (ruff check) + type check (mypy)
just test      # Run tests with coverage
just smoke     # Build and verify the wheel in a temp virtual environment
just check     # Run all checks: fmt → lint → test
just docs      # Serve docs locally
just build     # Build distribution packages
```

Without Just: replace `just <cmd>` with the corresponding `uv run` commands
in the `justfile`. Run a single test with
`uv run pytest tests/test_<module>.py::test_<name>`.

## What PeerPort Is

A self-hosted life-sim: AI personas ("Peers") live autonomously in a small
pixel-art cyber port town; the user ("Keeper") watches and chats through a
browser UI. FastAPI + WebSocket backend, PixiJS frontend (no Node build
chain), SQLite persistence, OpenAI Responses API.

**Read before designing or implementing anything:**

- `requirements.md` — the product spec; it outranks every other document
- `docs/design/decisions.md` — append-only design decision log (D-NNN)
- `docs/design/prototype-design.md` — UI/visual spec (tokens, wireframes)
- `docs/design/map-layout.md` — the 40×30 world map and waypoint graph
- `personas/*.md` — persona definitions (schema in requirements §3.2)
- `locales/en.json` / `locales/ja.json` — all UI copy; never hardcode
  user-facing strings

Design/UI/worldview/copy work must go through the `peerport-designer`
skill (`.claude/skills/peerport-designer/`), which enforces terminology,
tone, and the decision log.

## Architecture

```
src/peerport/
├── __init__.py   # Public API — export everything users need here
├── py.typed      # PEP 561 marker for typed package
└── core.py       # Placeholder module — replace and re-export via __init__.py
```

- Keep the public API surface small — export via `__init__.py.__all__`
- Internal modules can use a leading underscore (`_internal.py`)
- Separate concerns: one module per logical unit
- Update `docs/reference.md` and README examples whenever you change the public API

## Review Checklist

Before submitting a PR:

1. `just check` passes (format, lint, type check, tests)
2. New public APIs have type annotations and docstrings
3. Tests cover the new functionality
4. No unnecessary dependencies added

## Important Reminders

- All code, docs, commits, and PRs must be written in English
- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary
- ALWAYS prefer editing an existing file to creating a new one
- NEVER proactively create documentation files unless explicitly requested
- Dependencies should always be added to the appropriate group in pyproject.toml
