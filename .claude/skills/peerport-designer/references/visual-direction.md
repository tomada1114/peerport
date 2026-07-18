# Visual Direction — Constraints, Open Axes, Taste

How to make PeerPort look intentional. Read before any art-direction or
styling work. Decisions already made live in `docs/design/decisions.md` —
never re-litigate them; this file explains the *space* of choices.

## Fixed constraints (from requirements.md — not up for debate)

- **Stack**: PixiJS 2D map + vanilla JS, no Node build chain; `pixi.min.js`
  vendored. CSS is hand-written; no Tailwind/frameworks.
- **Canvas**: one tile map (~40×30 tiles). Locations: Dock Square, Signal
  Tower, 3 Berths, lighthouse (Bridge's world-side), piers/alleys.
- **Layout**: map left (flexible, the star) + Bridge panel right (fixed
  width). Below 1024px viewport width: stacked with tab switching.
- **Day/night**: independent world clock; morning/day/dusk/night lighting
  cycle (1 world day = 2 real hours by default).
- **Assets**: MVP ships placeholder pixel art; directory layout must allow
  drop-in replacement — pixel art is a welcome-contribution area.
- **Diegetic error states**: API outage = fog over the map (see
  world-and-cast.md "How the world speaks").
- **i18n**: en/ja. Every font/typography choice must cover Japanese.
- **OSS**: every font and asset must be redistributable (OFL fonts, CC0 or
  self-made CC-licensed art). Record license per asset.

## Open decision axes

Each axis below is a decision to make *with the Keeper* (AskUserQuestion,
≤4 options, tradeoffs stated, recommendation marked). Record outcomes in
`docs/design/decisions.md`. Suggested order: 1 → 2 → 4 → 3 → 5 → 6.

### 1. Pixel scale
- **16×16 tiles / ~16×24 sprites** — GBA-era authenticity (closest to the
  inspiration), cheapest for contributors to redraw, reads well zoomed 2–3×.
- **24×24** — middle ground; more facial/pose expression, still cheap.
- **32×32** — expressive, modern-indie look; higher asset cost, silhouettes
  need more skill; contributor bar rises.

### 2. Palette strategy & neon dosage
- **Unified limited palette** (one ~32-color master palette for all world
  art) — strong cohesion, easy day/night tinting, contributor-friendly
  ("use these colors"). Recommended default posture.
- **Free color + neon accents** — flexible, risks patchwork as contributors
  add assets.
- Neon dosage is a dial, not a binary: signage/Signal Tower/lighthouse beam
  glow vs. full neon-city saturation. The tone is *bright pop*, so daylight
  scenes must also look good — a world that only works at night is off-spec.

### 3. Day/night rendering
- **Full-screen tint overlay** (single multiply/overlay layer) — trivial,
  uniform, MVP-appropriate.
- **Palette swap per time-of-day** — richer (windows light up, sky bands),
  more asset work. Can be layered later; don't block MVP on it.

### 4. Bridge panel chrome
The Bridge is read for minutes at a time — readability outranks flavor.
- **Diegetic ship console** — pixel frame, scanlines, monospace everywhere.
  Maximum immersion, worst long-form readability, ja glyphs suffer.
- **Clean modern panel** — plain chat UI beside the pixel map; readable but
  breaks the world at the seam.
- **Hybrid (frame diegetic, body modern)** — pixel-art chrome, tab icons,
  world-voice copy on the shell; body text in a readable UI font with
  generous line-height. Usually the right answer; verify with the Keeper.

### 5. Typography
- Display/headers: a pixel or pixel-flavored font is on-tone. For ja, pixel
  fonts are scarce — vet coverage + license (candidates to research at
  decision time; do not hardcode assumptions).
- Body (chat, mail, notes): prioritize long-read comfort; system font stack
  is acceptable and build-chain-free. Latin/ja must harmonize in size and
  weight; test with real mixed copy, not lorem ipsum.

### 6. Motion
- World: sprite idle loops (2–4 frames), walk cycles, speech bubbles pop.
  Signature-level motion budget goes to *one* thing (see below).
- Bridge: streaming text is the main motion; keep panel chrome still.
- Always honor a reduced-motion setting; the world keeps working with
  animation dialed down.

## Taste rules (adapted from the frontend-design philosophy)

- **The map is the hero; the Bridge is the instrument.** Spend visual
  boldness on the world; keep the panel quiet and legible.
- **One signature element.** Pick a single memorable moving image — e.g. the
  lighthouse beam sweep, the fog rolling in, windows lighting at dusk — and
  polish it. Everything else stays modest. Two signatures = zero signatures.
- **Name your tokens first.** Before any CSS/canvas code: 4–8 named colors
  (e.g. `harbor-night`, `beacon-warm`, `signal-cyan`), 2 type roles, spacing
  scale. Write them in the design doc; code copies the doc.
- **Anti-cliché check**: reject the three default "AI looks" (cream/serif
  editorial; near-black+single-neon-accent dashboard; broadsheet hairlines)
  *and* the genre cliché: purple-drenched rainy cyberpunk. PeerPort's
  distinctiveness comes from its own nouns — harbor, lighthouse, signal,
  fog, tide — not from genre wallpaper.
- **Placeholder-honest**: MVP placeholders (flat-color characters, plain
  tiles) should look *deliberately minimal*, not broken — consistent scale,
  aligned palette, real silhouettes. A good placeholder invites contributors
  to redraw it; a broken one repels them.

## Quality floor (non-negotiable, applies to every screen)

- WCAG AA contrast for all Bridge text; chat text at a comfortable reading
  size (≥14px equivalent, user-zoomable).
- Works in a dim room at night — this is an evening-desk companion; avoid
  large white surfaces.
- Keyboard: chat input focus, tab switching, Escape closes popups.
- Cost display, pause/speed controls always visible without hunting (§4.4).
