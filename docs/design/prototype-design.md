# PeerPort Prototype Design Specification

> Status: v1.0 (2026-07-18) — produced by the `peerport-designer` skill.
> Grounded in `requirements.md` (which always wins on conflict) and
> `docs/design/decisions.md` (D-001…D-017). Scope: everything the Keeper
> sees in the MVP (M1–M6), specified to prototype fidelity.

## 1. Design summary

A bright pixel-art harbor on a teal data-sea, watched over by a lighthouse
whose sweeping beam is the game's single signature visual. The world map is
the hero on the left; the Bridge — a calm hybrid panel (pixel-flavored
shell, modern readable body) — is the instrument on the right. System states
speak in the world's voice: outages arrive as fog, budget saving as
"running dark". Nothing about the UI should feel like a dashboard.

## 2. Art direction

### 2.1 Design tokens (UI layer, CSS custom properties)

Code copies these names; do not invent ad-hoc colors.

| Token | Hex | Role |
|---|---|---|
| `--harbor-night` | `#101D26` | App/page and Bridge panel background (near-black teal; never pure black) |
| `--tide-deep` | `#16323E` | Raised surfaces: cards, input fields, bubbles in Bridge |
| `--tide-line` | `#24485A` | Borders, frames, dividers, inactive tab outline |
| `--foam` | `#E8F1F2` | Primary text |
| `--mist` | `#9FB8BE` | Secondary text, timestamps, placeholders |
| `--signal-cyan` | `#3FD2C7` | Interactive: links, focus rings, active states, streaming caret |
| `--beacon-amber` | `#FFB454` | Signature warm: active tab underline, beam, highlights, "new" markers |
| `--ember` | `#E5735A` | Destructive/hard-stop only (delete note, hard budget cap) |

Contrast: `--foam` and `--mist` on `--harbor-night`/`--tide-deep` meet WCAG
AA at body size (verify at implementation with real values; adjust `--mist`
lightness before shipping if it misses 4.5:1 on `--tide-deep`).

### 2.2 World palette (map layer)

One master palette of ≤32 colors for all world pixel art (D-012), anchored
teal-sea × amber-light (D-017). The definitive `.gpl`/`.hex` palette file is
authored with the first real asset batch and lives in `assets/palette/`;
until then, placeholder art uses the 8 UI tokens above plus 4 mid-teal
ramps. Rules for palette authors:

- Cool range (sea, sky, stone, shadow): desaturated teals/blues.
- Warm range (light, wood, skin, signage): ambers/corals — the *only*
  source of warmth, so light always reads as light.
- Neon (pure `--signal-cyan`-family + one magenta) appears **only** on:
  Signal Tower lamps, shop signage, lighthouse lens, Echo's rim-light.
- Every sprite must work under all four time tints (see 2.5).

### 2.3 Pixel scale & rendering

- Tiles 16×16; character sprites 16×24 (1 tile wide, 1.5 tall) (D-011).
- Integer zoom only (2× or 3× chosen by viewport height); nearest-neighbor
  scaling (`SCALE_MODES.NEAREST`); no sub-pixel sprite positions at rest
  (interpolated movement may be fractional mid-walk).
- Map 40×30 tiles → 640×480 native → 1280×960 at 2×. This exceeds the map
  viewport at the 1280×800 baseline (~900×800 after the Bridge), so the map
  renders through a camera: idle-centered on Dock Square, drag-to-pan for
  inspection (panning is observation, not interference — operator view
  holds), edges clamped to the map. On viewports ≥1400px tall/wide enough,
  the full map fits and panning disables itself.

### 2.4 Typography

| Role | Font | Usage |
|---|---|---|
| Display | Ja-capable OSS pixel font — vet **PixelMplus12** first (M+ license), fallback candidates 美咲/JF-Dot family | Tab labels, panel/section headers, HUD, popup titles, onboarding titles |
| Body | System UI stack: `system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif` | Chat, mail, notes, BBS, logbook, settings — anything read for minutes |

Body baseline 15px / line-height 1.7 (ja-comfortable); display font never
below 12px-equivalent native size and always at integer multiples. Record
the chosen pixel font + license in `decisions.md` when vetted (D-015).

### 2.5 Day/night tint (MVP)

Single full-screen overlay on the map container only — the Bridge panel
never tints (D-016). World day = 2h real (D-005).

| Band | Overlay (multiply) | Feel |
|---|---|---|
| Morning | `#FFD9A8` @ 12% | soft gold |
| Day | none | neutral bright pop |
| Dusk | `#FF9868` @ 18% | amber hour — the palette's showcase |
| Night | `#1B3A66` @ 35% | deep blue; neon and beam carry the scene |

Transitions crossfade over ~30 world-minutes. Emissive layers (windows,
lamps) are a post-MVP additive extension, not required here.

### 2.6 Signature element — the lighthouse beam (D-014)

- A translucent additive cone rotating from the lighthouse lens, full sweep
  ~24s, `--beacon-amber` fading to transparent along its length.
- Day/morning: barely visible (≈6% opacity). Dusk: noticeable. Night: the
  defining image (≈22% peak, soft-edged, briefly glinting on water tiles it
  crosses — glint optional post-MVP).
- Reduced motion: static soft glow on the lens, no sweep.
- Boldness budget rule: nothing else on the map may animate at this level
  of prominence. Everything else stays at idle-loop scale.

## 3. Layout & main screen

Baseline 1280×800. Map hero left, Bridge fixed 380px right (D-003).

```
┌──────────────────────────────────────────────┬────────────────────┐
│ [1] Map viewport (flex, PixiJS)              │ [4] Bridge (380px) │
│                                              │ ┌────────────────┐ │
│   lighthouse w/ beam · Dock Square           │ │[4a] tab rail   │ │
│   Signal Tower · Berth ×3 · piers            │ │ Mate Mail Sig  │ │
│   peers walking · speech bubbles             │ │ Log Note Set   │ │
│                                              │ ├────────────────┤ │
│                                              │ │[4b] tab body   │ │
│ [2] world HUD (overlay, bottom-left)         │ │  (scrolls)     │ │
│  ◔ Day 12 · Dusk   ⏸ 1x|2x                  │ │                │ │
│ [3] spend chip (overlay, bottom-right of map)│ ├────────────────┤ │
│  $0.14 today                                 │ │[4c] input row  │ │
└──────────────────────────────────────────────┴────────────────────┘
```

- [1] Map canvas letterboxes with `--harbor-night` when aspect leaves
  margins; the world never stretches.
- [2] HUD: world clock (day count + band icon), pause toggle, speed 1x/2x.
  Pixel display font, semi-transparent `--tide-deep` chip. Always visible
  (D: requirements §4.4 world controls).
- [3] Spend chip: today's API cost, click → Settings tab (monthly detail).
  Turns `--beacon-amber` in low-power mode, `--ember` at hard stop.
- [4a] Tab rail: 6 tabs, pixel font + 16px pixel icons; active tab gets
  `--beacon-amber` underline; unread markers (Mail/Logbook) are small amber
  dots — no numeric badges (world-and-cast: no scale-thinking).
- [4c] Input row exists only on tabs that accept input (Mate, Signal Tower
  post, Mail reply, Notes edit).

**Narrow (<1024px)**: vertical stack — map on top (16:9 crop, HUD intact),
Bridge below at full width; same tab rail. No layout other than stacking
changes; this is a fallback, not a designed-for target.

### 3.1 Map interactions (operator view, D-009)

| Action | Result |
|---|---|
| Click/tap a peer | Peer profile popup (see §5) |
| Click Signal Tower | Bridge switches to Signal Tower tab |
| Click lighthouse | Bridge switches to Mate tab (the lighthouse *is* the Keeper's seat) |
| Hover peer (desktop) | Name label in pixel font above sprite |
| Esc | Close popup |

Drag = camera pan only (§2.3); double-click empty ground re-centers on
Dock Square. No other camera control in MVP.

## 4. Bridge tabs

Shared shell: pixel-frame border (`--tide-line` 9-slice), body on
`--harbor-night`, cards on `--tide-deep`, 12px spacing grid (4/8/12/20/32).

### 4.1 Mate (chat) — default tab

```
┌──────────────────────────────┐
│ ⌂ BEACON        ● out walking│  header: Mate name (pixel font),
├──────────────────────────────┤  presence line (world position, flavor)
│  ┌────────────────────────┐  │
│  │ Beacon: Welcome back.  │  │  Mate bubbles: left, --tide-deep,
│  │ While you were away…   │  │  amber 2px left edge
│  └────────────────────────┘  │
│          ┌────────────────┐  │  Keeper bubbles: right, outlined,
│          │ You: any news? │  │  no fill (keeps panel calm)
│          └────────────────┘  │
│  Beacon is typing ▌          │  streaming: token-by-token, cyan caret
├──────────────────────────────┤
│ [ Message Beacon…        ] ↵ │  input: 1-line grows to 4; Enter sends,
└──────────────────────────────┘  Shift+Enter newline
```

- Presence line mirrors the map ("● at Dock Square", "● talking with Tug")
  — pure flavor; Mate always answers (requirements §4.4).
- Research flow: Mate acknowledges → sprite visibly runs to Signal Tower →
  results stream in; long reports auto-file to Notes and the chat shows a
  digest + `Filed to Notes → "…"` link line (requirements §4.5).
- Day-one/empty: `en` "The line to your Mate is open. Say hello." /
  `ja`「Mateとの回線は開いている。まずは挨拶から。」

### 4.2 Mail

List of letters (sender, subject, world-date, unread dot) → letter view →
reply composer under the letter. Letters are cards with a subtle
sender-colored edge (Kai warm coral, Mia soft green — from the world
palette, giving off-map friends a visual identity, world-and-cast cast
sheet). Reply is plain text with a single Send action; sent replies render
in-thread.
- Empty: `en` "No letters yet. News from beyond the harbor takes time." /
  `ja`「まだ手紙はない。港の外からの便りには時間がかかる。」

### 4.3 Signal Tower (BBS)

Chronological board (newest first): each post = author chip (peer sprite
face 16×16 + name, or "Keeper"), body, world-date. Keeper composer pinned
at top: placeholder `en` "Pin a notice to the board…" / `ja`「掲示板に貼り
紙をする…」. Posting is fire-and-forget; peers react on their own schedule
(requirements §4.4) — no reply threading in MVP.
- Empty: `en` "The board is clear. Post something — the whole port reads
  it." / `ja`「掲示板はまっさら。何か貼れば、港じゅうが読みにいく。」

### 4.4 Logbook

Two sections: **While you were away** (latest absence digest as a dated
entry list) and **Chronicle** (world-event timeline, collapsed by world
day). Entries are read-only prose lines with world-date stamps in the
display font. On launch after ≥30min absence, the app lands on Mate tab
where Beacon narrates the digest (requirements §4.7); Logbook holds the
full record with an amber dot until first viewed.
- Day-one: `en` "The logbook opens with today. The harbor's story starts
  here." / `ja`「ログブックは今日から始まる。この港の物語の1ページ目。」

### 4.5 Notes

List (title, updated date, 1-line summary) → Markdown view/edit. Keeper can
edit and delete; deletion confirms inline with an `--ember` text button
(requirements §4.5: delete is Keeper-only). Notes authored by the Mate are
marked "filed by Beacon".
- Empty: `en` "The shelf is empty. Ask {mate} to file your first note." /
  `ja`「棚はまだ空っぽ。最初のノートは{mate}に頼んでみよう。」

### 4.6 Settings (the engine room)

Plainly technical on purpose (world-and-cast: Settings may drop the world
voice). Groups: Locale (en/ja) · Models (mate/background, free text) ·
Budget (soft/hard caps, current month table from `usage_log`) · World
(day length, speed default, reduced motion) · Data (backup folder path,
`--fresh` explanation). Standard form controls, no pixel styling in the
body.

## 5. Peer profile popup

Opens over the map (not the Bridge), Esc/click-out closes.

```
┌─────────────────────────────┐
│ [sprite 3×] TUG      · Peer │  name pixel font; kind: Peer/Mate/Drifter
│ mood: cheerful              │  mood word from last action's `mood`
├─────────────────────────────┤
│ Ties                        │  relationship rows: peer name + label
│  Bell — "old bickering pals"│  (LLM one-liner) + trend arrow ↗/→/↘
│  Beacon — "training buddy"  │  (from score delta; number never shown)
├─────────────────────────────┤
│ Lately                      │  2–3 recent memory/event lines
│  · posted on the Signal     │
│    Tower about the tide     │
└─────────────────────────────┘
```

Relationship scores stay internal — labels and arrows only (small-and-
knowable world, not a stats screen).

## 6. Onboarding (first run)

Full-screen over the map (map visible but dimmed behind — the world exists
before you're introduced to it):

```
1. API key check      → if unset: instructions card (plainly technical)
2. Locale             → en / ja  (moved before first conversation — see §9)
3. Keeper name        → "The port needs a keeper. What's your name?"
4. Meet your Mate     → scripted-then-live chat in Bridge style; Mate
                        naming happens inside this conversation
                        (default: Beacon), then the overlay lifts and the
                        beam does one full sweep as the "world open" beat
```

Steps 3–4 are in-world copy (en/ja per chosen locale); step 1 is not.

## 7. Key user flows

```
Research request
1. Keeper → Mate tab: "look into X for me"
2. Beacon replies intent → sprite runs to Signal Tower (flavor, async)
3. web_search runs → summary streams into chat (sources listed)
   └─ API failure → fog rolls in + world-voice line, auto-retry note
4. Long result → auto-filed to Notes; chat shows digest + filed-link

Returning Keeper (≥30 min away)
1. Launch → world resumes → Logbook generates
2. Mate tab active: "Welcome back. While you were away…" digest
3. Logbook tab carries amber dot → full chronicle on visit

Mail reply
1. Mail tab: unread dot → open letter → reply inline → Send
2. Friend's next letter (later, capped 3/session) references the reply
```

## 8. Cross-cutting specs

### 8.1 Errors & degraded states (diegetic, D-006)

| Event | Map | Bridge | Plain detail |
|---|---|---|---|
| LLM/API outage | Fog layer slides over the harbor (soft gray-teal, 40%); peers keep walking | Inline line in active tab: `en` "Fog's rolled in — the port can't reach the outside right now." / `ja`「霧が出てきた。いま港は外と連絡が取れない。」 | expandable one-liner: HTTP status + auto-retry/backoff note |
| WS disconnect | world runs server-side | quiet chip in tab rail: `en` "signal lost… reconnecting" / `ja`「信号ロスト…再接続中」; snapshot resync on return | — |
| Soft budget cap | — | spend chip turns amber + banner: `en` "Running dark to save power — the port slows down a little." / `ja`「節電航行中。港の時間が少しゆっくりになる。」 | intervals doubled, cap value |
| Hard budget cap | world pauses, night-still frame | `--ember` banner: `en` "The port has dropped anchor for today — spending hit the hard limit. Raise it in Settings to set sail again." / `ja`「今日はここで錨を下ろした。利用額が上限に達したよ。再開するならSettingsで上限を上げて。」 | cap value, spend link |

Never a blocking modal; the harbor stays visible through everything.

### 8.2 Loading & feedback

- Chat streams; world bubbles appear whole with a 2-frame pop.
- Async work: world flavor first (sprite behavior), spinner never on the
  map; Bridge may show a small cyan pulse dot next to the pending item.
- Confirmations are single quiet lines ("Note filed.", "Notice posted.");
  no toast stacks, no success modals.

### 8.3 Accessibility

- Full keyboard path: Tab cycles Bridge controls, Ctrl/Cmd+1–6 switches
  tabs, Esc closes popups, chat input auto-focused on Mate tab.
- Focus ring: 2px `--signal-cyan` outside-offset on every focusable.
- AA contrast per §2.1; reduced-motion setting: beam→glow, bubbles/idle
  anims→static, tint crossfades→instant.
- Map information is never exclusive: everything observable on the map
  (positions, conversations, events) also exists as text in Logbook/tabs.

### 8.4 i18n

- All copy authored as en/ja pairs at spec time (see tab sections); ja is a
  voice-matched translation, not literal.
- Labels sized for the longer locale (usually en in the tab rail, ja in
  body); chat wraps with `overflow-wrap: anywhere` off — use `line-break:
  strict` ja defaults, no mid-word breaks in en.
- Dates: world-dates ("Day 12, Dusk") localized as 「12日目・夕」;
  real-time timestamps per locale convention.

## 9. Asset specifications (contributor-facing)

- **Tile sheet**: 16×16, PNG, master palette only; one sheet per biome
  layer (ground/water/props). Collision and location zones live in map
  data, not pixels.
- **Character sprites**: 16×24, PNG sheet per peer: walk down/up/side
  (side mirrored) × 2 frames, plus a 2-frame down-facing idle loop and one
  "emote base" frame. Echo additionally gets a 1-color rim-light variant.
- **UI**: pixel frame as 9-slice PNG (16px corners); tab icons 16×16,
  1-color, `--foam` tinted at runtime.
- **Licensing**: every asset ships CC0 or CC-BY (recorded in
  `assets/CREDITS.md`); fonts OFL/M+-licensed only. Assets not meeting
  this are rejected regardless of quality (D: visual-direction, OSS rule).
- Placeholder MVP set: flat-silhouette peers (correct size/palette/
  silhouette per cast sheet), plain tiles — deliberately minimal, on-grid,
  on-palette ("placeholder-honest").

## 10. Open items & flags

1. ~~Flag (requirements conflict)~~ **Resolved 2026-07-18**: locale moved
   to onboarding step 2 (before the first Mate conversation);
   `requirements.md` §4.10 amended to match (D-018).
2. Pixel font final selection + license record (PixelMplus12 first
   candidate) — at implementation, then append to `decisions.md`.
3. Master palette `.gpl` authoring with first real asset batch (§2.2).
4. Beam water-glint and dusk window emissives — post-MVP polish candidates
   (do not start before M6).
5. `--mist` contrast verification on `--tide-deep` at 15px (§2.1).
