# Screen Design — Inventory, Wireframes, Cross-Cutting Specs

Conventions for designing PeerPort screens. This is a **desktop-browser
app** (localhost, single player) — mobile thumb-zone rules do not apply;
below-1024px behavior is a stacked fallback, not the primary target.

## Screen & state inventory

Design work should always name which of these it touches.

**Surfaces**
1. **Main screen** = Map (left) + Bridge panel (right). The only screen.
2. **Bridge tabs**: Mate (chat) / Mail / Signal Tower / Logbook / Notes /
   Settings.
3. **Map popups**: Peer profile (click a peer → profile, relationships,
   recent doings), location hint (Signal Tower click → jumps to BBS tab).
4. **Onboarding** (first run): API key guidance → Keeper name → Mate naming
   (in-conversation) → locale.
5. **Narrow layout** (<1024px): map/Bridge vertical stacking + tab switch.

**Global states** (every surface design must say how it reacts)
- Fog (LLM outage; movement continues), paused, low-power mode (soft budget
  cap), hard-stop (hard budget cap), WS reconnecting, `--fresh` first day.

## ASCII wireframe conventions

- One fenced block per surface/tab; baseline canvas 1280×800. Add a narrow
  variant only when layout actually changes.
- Box-drawing characters; annotate regions with `[n]` markers and a legend
  below each frame. Note fixed vs. flexible dimensions.
- Wireframes show *structure and hierarchy*, not art. Pixel-art content is
  described in words inside the region.

Example skeleton:

```
┌────────────────────────────────────────────┬──────────────────┐
│ [1] Map canvas (flex)                      │ [2] Bridge (fixed│
│     PixiJS world: tiles, peers, bubbles    │     ~380px)      │
│                                            │  [2a] tab rail   │
│                                            │  [2b] tab body   │
│ [3] world HUD: clock / pause / 1x2x / $    │  [2c] input row  │
└────────────────────────────────────────────┴──────────────────┘
```

## User-flow notation

Numbered steps + arrows, with error/wait branches inline:

```
Keeper asks Mate to research
1. Keeper types request in Mate tab
2. Mate replies intent → (map) Mate sprite runs to Signal Tower
3. web_search runs async → streaming summary in chat
   └─ on API failure → fog notice + retry offer
4. Long report? → auto-saved to Notes, chat shows digest + link
```

## Cross-cutting sections (REQUIRED in every screen spec)

Adapted for this project — a screen spec without these four is incomplete.

### 1. Error & degraded states (diegetic mapping)
| Event | World expression | Bridge expression |
|---|---|---|
| LLM/API outage | Fog rolls over the map | One-line world-voice notice + plain detail line; auto-recover note |
| WS disconnect | World keeps running server-side | Quiet "signal lost… reconnecting" indicator; snapshot resync on return |
| Soft budget cap | Lights dim subtly (optional) | "Running dark" banner; intervals doubled note |
| Hard budget cap | World pauses | Clear paused state + how to resume |
Never a blocking modal for any of these; the harbor stays visible.

### 2. Loading & feedback
- Mate chat streams token-by-token; world speech bubbles appear whole.
- Async work gets *world flavor first* (Mate runs to Signal Tower), spinner
  second.
- State changes (note saved, mail sent, world paused) confirm with one quiet
  line, not toast stacks.

### 3. Empty states (worldbuilding moments)
Each tab's day-one/empty state gets one in-world line + one actionable hint.
Specify the copy (en + ja) in the screen spec. No bare "No items".

### 4. Accessibility & i18n
- Keyboard reachability for every Bridge action; Escape closes popups.
- AA contrast; reduced-motion behavior stated per animation.
- en/ja both checked: ja line-height/wrapping (no `word-break` disasters in
  chat), label width where en is longer, dates/times per locale.

## Interaction inventory (fixed by requirements)

- Keeper never controls peers directly — operator view only. All input goes
  through the Bridge (chat, BBS posts, mail replies, settings).
- Map interactions are read-only inspections: click peer → popup; click
  Signal Tower → BBS tab.
- World controls: pause/resume, speed 1x/2x, today's API spend always
  visible.
- Mate is always reachable in chat even while walking (may answer "in a
  moment — talking with Tug" as flavor, never actually blocked).
