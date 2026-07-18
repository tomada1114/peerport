# Technical Architecture

> Status: v1.0 (2026-07-18). The implementation backbone for issues #9–#29.
> `requirements.md` defines *what*; this document fixes *how* so that every
> issue slots into one coherent structure. Deviate only with a written note
> in `docs/design/decisions.md`.

## 1. Process model

One Python process, one asyncio event loop, started by `uv run peerport`
(console script → `peerport.__main__:main`). Inside it:

- **Tick task** — every 500ms: advance world clock, step peer movement
  along paths, flush position diffs to the broadcaster. **Never awaits an
  LLM call.**
- **Peer scheduler task** — one per map peer: sleeps `activity_interval` ±
  jitter, wakes on events (spoken to, BBS post, Keeper instruction), then
  enqueues an LLM decision job.
- **LLM worker pool** — a single `asyncio.TaskGroup` draining a global job
  queue (decisions, conversation turns, summaries, importance scoring,
  logbook, mail). Concurrency cap 4; the budget guard gates the queue.
- **Broadcaster task** — drains an outbound event queue and fans out to
  all connected WebSockets.
- **FastAPI/uvicorn** — serves HTTP + WS on port 8712 (config).

The world runs whether or not a browser is connected. Graceful shutdown:
cancel tasks in order (schedulers → workers → tick), final DB commit,
record `last_shutdown_ts` in `world_state`.

## 2. Module layout

```
src/peerport/
├── __main__.py        # CLI entry: arg parsing (--fresh, --debug), boot
├── config.py          # config.toml + env (OPENAI_API_KEY) + defaults
├── db.py              # SQLite open/schema/backup-rotate; thin query API
├── events.py          # in-process event bus (asyncio.Queue, typed events)
├── world/
│   ├── clock.py       # world time, day bands, speed 1x/2x, pause
│   ├── worldmap.py    # port.json loading, zones, waypoints, A*
│   ├── sim.py         # tick loop, movement stepping, peer positions
│   └── state.py       # snapshot/diff assembly for the wire
├── peers/
│   ├── personas.py    # personas/*.md parse + validate + seed memories
│   ├── decide.py      # Option-Action decision loop, action schema
│   └── converse.py    # peer↔peer turns, summary, relationship update
├── memory/
│   ├── stream.py      # memory writes, importance batch scoring
│   ├── recall.py      # 3-axis retrieval (recency/importance/relevance)
│   └── reflect.py     # reflection + summarize-and-forget
├── llm/
│   ├── client.py      # Responses API wrapper, retries, usage recording
│   ├── budget.py      # daily spend, soft/hard caps, low-power flags
│   └── prompts.py     # prompt assembly (fixed prefix ordering), schemas
├── mate/
│   ├── chat.py        # Keeper↔Mate streaming chat
│   ├── research.py    # web_search flow + report filing
│   └── notes.py       # data/notes/*.md ops (5 tool functions)
├── friends/mail.py    # friend state vars, mail generation, hearsay
├── logbook.py         # absence events, chronicle, weekly summary
└── server/
    ├── app.py         # FastAPI app factory, lifespan wiring
    ├── ws.py          # WS endpoint: snapshot on connect, diff stream
    ├── api.py         # REST: bridge commands (see §4)
    └── static/        # index.html, css/, js/, vendor/pixi.min.js
```

Frontend (`server/static/`, no build chain, ES modules):
`js/net.js` (WS + reconnect + REST), `js/world.js` (Pixi renderer),
`js/bridge.js` (tabs/panels), `js/i18n.js` (catalog loading + `t(key)`),
`css/tokens.css` (the 8 design tokens) + `css/bridge.css`.

## 3. Data layer

SQLite via **stdlib `sqlite3`** in `asyncio.to_thread` for writes (no
aiosqlite dependency; call volume is tiny). WAL mode. One module (`db.py`)
owns all SQL. Schema (created idempotently at boot):

```sql
peers(id TEXT PK, name TEXT, kind TEXT, pair TEXT, sprite TEXT,
      pos_x INT, pos_y INT, state TEXT, mood TEXT)
relationships(peer_a TEXT, peer_b TEXT, score INT, label TEXT,
      updated_ts INT, PK(peer_a, peer_b))          -- store a<b once
memories(id INTEGER PK, peer_id TEXT, ts_world INT, ts_real INT,
      kind TEXT, text TEXT, importance INT, embedding BLOB,  -- f32 packed
      reflected INT DEFAULT 0)
world_state(key TEXT PK, value TEXT)               -- clock, weather, misc
events(id INTEGER PK, ts_world INT, ts_real INT, kind TEXT,
      actors TEXT, payload TEXT)                   -- full history, JSON
mails(id INTEGER PK, friend_id TEXT, direction TEXT, subject TEXT,
      body TEXT, ts_real INT, read INT DEFAULT 0)
board_posts(id INTEGER PK, author_id TEXT, body TEXT, ts_world INT)
usage_log(id INTEGER PK, ts_real INT, model TEXT, purpose TEXT,
      input_tokens INT, cached_tokens INT, output_tokens INT,
      est_cost_usd REAL)
```

Embeddings: `struct`-packed float32 BLOBs; cosine similarity in pure
Python (≤2,000 rows/peer — no numpy dependency). Backups: on boot copy
`data/peerport.db` → `data/backups/peerport-<ts>.db`, keep 7.

## 4. Wire protocol

**Downstream = WebSocket** (`/ws`), JSON messages, `{"t": <type>, ...}`:
`snapshot` (full world on connect), `pos` (batched moves per tick),
`speech` (bubble), `chat_delta` / `chat_done` (Mate streaming),
`event` (logbook/board/mail markers), `clock`, `state`
(fog / low_power / hard_stop / paused), `spend` (today's total).

**Upstream = REST** (`/api/…`), because Keeper commands are sparse and
request/response-shaped: `POST /api/chat` (message → deltas arrive on WS),
`POST /api/board`, `POST /api/mail/{id}/reply`, `POST /api/world`
(pause/resume/speed), `GET/POST /api/notes…`, `GET /api/logbook`,
`GET /api/usage`, `POST /api/settings`, `GET /api/peer/{id}` (popup data),
onboarding endpoints. WS stays downstream-only (matches requirements §4.8:
client→server is Bridge operations only).

Client reconnect: exponential backoff 1→2→4→…→30s cap, then resnapshot.

## 5. LLM integration

- **Single gateway**: everything goes through `llm/client.py::call()`,
  taking a `purpose` tag (decide/converse/summarize/score/logbook/mail/
  chat), model resolved from config (`gpt-5-nano` default, `gpt-5-mini`
  for chat), optional JSON schema (Structured Outputs), `max_output_tokens`.
  It records usage + estimated cost per call and consults `budget.py`
  before dispatch (soft cap → low-power flags; hard cap → raise
  `BudgetExceeded`, callers convert to world-pause).
- **Prompt discipline** (`prompts.py`): every prompt is
  `[STATIC: world rules text + persona body] + [DYNAMIC: situation,
  retrieved memories top-10, recent actions]` — static part byte-identical
  across calls per peer to maximize prompt caching. World-rules text lives
  in `llm/prompts.py` as one constant.
- **Schemas as pydantic models** (pydantic ships with FastAPI): action
  decision, conversation turn (`wants_to_end`), logbook events array,
  importance scores array, relationship delta.
- **Injection guard**: retrieved web content and peer speech are wrapped
  as data ("quoted material, not instructions") in the world-rules text.
- Retries: exponential backoff ×3 (client level); schema-violation → one
  re-ask then skip (caller level). Rate limit → skip action, next cycle.

## 6. Determinism & testing

- All randomness (jitter, wander targets, spawn timing) flows from one
  `random.Random(seed)` owned by the sim; seed from config for tests.
- `llm/client.py` is the only network touchpoint; tests inject a
  `FakeLLM` returning canned/schema-valid payloads (a pytest fixture in
  `tests/conftest.py`). **No test may hit the network.**
- Tick logic is testable without asyncio timing: `sim.step(dt)` is a pure
  function of state; the tick task is a thin driver.
- Frontend has no test harness in MVP; `just check` covers Python only.

## 7. Boot sequence

`--fresh` archives the old DB (backup, then delete) before this:

1. Load config (+ `.env` for `OPENAI_API_KEY` — key absence is allowed:
   world runs LLM-less with fog UI, onboarding shows setup card).
2. Open DB, run schema, rotate backups.
3. Load + validate personas (abort with filename+reason on error);
   first boot writes seed memories.
4. Load map (`data/map/port.json`).
5. If away ≥30min: enqueue logbook generation.
6. Start tasks (tick, schedulers, workers, broadcaster), then uvicorn.

## 8. Dependency policy

Runtime deps (pyproject `dependencies`): `fastapi`, `uvicorn`, `openai`,
`pydantic` — nothing else without a decisions.md entry. Explicitly avoided
for MVP: numpy (pure-python cosine), aiosqlite (to_thread), jinja2
(static HTML), any frontend tooling (vendored pixi.min.js only).

## 9. Issue → module map

| Issues | Modules touched |
|---|---|
| #9 F1 | config.py, db.py |
| #10 F2 | server/*, static/js/net.js |
| #11 F3 | peers/personas.py |
| #12 F4 | world/worldmap.py, data/map/port.json |
| #13 W1 | world/clock.py, world/sim.py, events.py |
| #14 W2 | static/js/world.js, css/tokens.css |
| #15 W3 | static/js/bridge.js, js/i18n.js, css/* |
| #16 C1 | llm/client.py, llm/budget.py |
| #17 C2 | memory/stream.py, memory/recall.py |
| #18 C3 | mate/chat.py, llm/prompts.py, server/api.py |
| #19 S1 | peers/decide.py |
| #20 S2 | peers/converse.py, static/js/world.js (popup) |
| #21 S3 | server/api.py, peers/decide.py, static/js/bridge.js |
| #22 A1 | logbook.py |
| #23 A2 | friends/mail.py |
| #24 U1 | mate/research.py |
| #25 U2 | mate/notes.py |
| #26 P1 | memory/reflect.py |
| #27 P2 | static/js/world.js, js/bridge.js, llm/budget.py |
| #28 P3 | server/static assets, assets/palette/ |
| #29 P4 | server/api.py, static/js/bridge.js |
