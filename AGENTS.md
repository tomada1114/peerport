# Project Guide

## Overview

This is a Python application built with [uv](https://docs.astral.sh/uv/) and
[hatchling](https://hatch.pypa.io/). It uses a strict `src/` layout with
comprehensive type checking and linting.

## Quick Reference

```bash
just install   # Install dependencies and git hooks when .git/ is present
just fmt       # Format code (ruff check --fix + ruff format)
just lint      # Lint (ruff) + spell check (typos) + type check (mypy)
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
├── __main__.py   # boot: config → db → personas → map → sim → app wiring
├── config.py     # Config (models / budget / world / server sub-configs)
├── db.py         # ALL SQL lives here; migrations via _SCHEMA_UPGRADES
├── errors.py     # PeerPortError hierarchy
├── logbook.py    # absence reports and weekly digests
├── world/        # clock, tick simulation, map + waypoint graph
├── llm/          # client.py (gateway, the ONLY network touchpoint),
│                 #   budget guard, prompt schemas
├── memory/       # memory stream, recall, reflection + forgetting
├── mate/         # Keeper-facing Mate chat and Notes tools
├── peers/        # personas, decision engine, peer conversations
├── friends/      # off-map friends and letter mail
└── server/       # FastAPI app, REST api, WS, broadcaster state, static/
```

Invariants (violating any of these is a bug):

- All SQL lives in `db.py`; schema changes go through the idempotent
  `_SCHEMA_UPGRADES` ALTER pattern
- Every LLM call goes through `LLMClient.call` / `call_stream` with
  `PromptParts` and a pydantic schema (strict Structured Outputs);
  usage is recorded and `BudgetGuard` consulted
- WebSocket wire frames are `{"t": <type>, ...}`, published only via
  `Broadcaster.publish` (`server/state.py`)
- Memory kinds are exactly the requirements §4.3 enum:
  observation / conversation / reflection / logbook / keeper_note
- The app must boot and run without `OPENAI_API_KEY` (degraded mode);
  every LLM-dependent service is wired conditionally in `__main__.py`
- Services that generate user-visible text carry a `locale` field threaded
  from config; locale catalogs keep en/ja key parity (enforced by tests)
- Separate concerns: one module per logical unit

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
