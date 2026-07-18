# Design Decision Log

Append-only log maintained by the `peerport-designer` skill. Format:
`D-NNN | date | decision | why | alternatives rejected`. A decision is
revised only on explicit request — add a new entry superseding the old one,
never edit history.

## Seed (derived from requirements.md, 2026-07-18)

| ID | Decision | Why / source |
|---|---|---|
| D-001 | Tone: near-future cyber-pop — bright digital port town, pixel art, neon; never dystopian, no combat imagery | requirements.md §2.1 |
| D-002 | Rendering: PixiJS tile map + vanilla JS/CSS, no Node build chain, `pixi.min.js` vendored | requirements.md §1.4 |
| D-003 | Layout: map left (flexible, hero) + Bridge panel right (fixed width); <1024px = vertical stack with tab switching | requirements.md §4.8 |
| D-004 | Bridge tabs: Mate / Mail / Signal Tower / Logbook / Notes / Settings | requirements.md §4.4 |
| D-005 | Day/night lighting cycle on the map; world day = 2 real hours (configurable) | requirements.md §4.1 |
| D-006 | Diegetic error states: LLM outage = fog over the harbor; world stays visible, no blocking modals | requirements.md §5.2 |
| D-007 | Assets: placeholder-first, drop-in replaceable directory; pixel art is a contributor-welcome area | requirements.md §4.8 |
| D-008 | i18n: English default, Japanese paired; all UI copy specified as en/ja pairs | requirements.md §2.3 |
| D-009 | Operator view: the Keeper never walks the map; all input flows through the Bridge; map is read-only inspection | requirements.md §4.8 |
| D-010 | Canonical terminology (Dock In, Berth, Signal Tower, …) used exactly in all UI copy | requirements.md §2.2 |

## Session decisions

<!-- Appended by peerport-designer sessions as D-011+ -->

| ID | Date | Decision | Why | Alternatives rejected |
|---|---|---|---|---|
| D-011 | 2026-07-18 | Pixel scale: 16×16 tiles, ~16×24 character sprites, rendered at 2–3× integer zoom | GBA-era authenticity (closest to inspiration); lowest asset cost; lowest contributor entry bar; 40×30 map fits on screen | 24×24 (few reference specs), 32×32 (asset cost too high for hobby OSS) |
| D-012 | 2026-07-18 | Color: one unified limited master palette (~32 colors) for all world art; neon reserved as accents (signage, Signal Tower, lighthouse light) | Cohesion across contributor assets; works in daylight and night scenes; simplifies day/night tinting | Full neon city (breaks "bright pop", weak daylight), free color (patchwork risk) |
| D-013 | 2026-07-18 | Bridge chrome: hybrid — pixel/terminal-flavored frame, tabs, and headers; modern readable UI font and spacing for body text | Immersion at the shell, long-form readability (ja included) where it counts | Full diegetic console (ja readability), clean modern (breaks the world at the seam) |
| D-014 | 2026-07-18 | Signature element: the lighthouse beam sweep | Embodies the Keeper/Bridge on the world side; visible from anywhere; shines at night | Dusk window lighting (secondary, may add later as plain detail), fog/tide (conflicts with error language), bubble motion (weak symbolism) |
| D-015 | 2026-07-18 | Typography: pixel font for display roles (headers, tabs, HUD; ja-capable OSS font vetted at implementation), system UI font stack for body (chat, mail, notes) | Long-read comfort + world flavor; no build chain needed | All-pixel (ja long-form readability), all-system (no flavor outside map) |
| D-016 | 2026-07-18 | Day/night: single full-screen tint overlay per time band (MVP); emissive/light layers are a future additive extension | Cheap, uniform, palette-friendly; doesn't block MVP | Per-time palette swaps (asset volume too high for MVP) |
| D-017 | 2026-07-18 | Palette anchor: teal/cyan data-sea base × warm amber light (lighthouse, windows, sunset) as the warm counterpoint | Harbor identity + warm/cool contrast that showcases the beam signature | Blue-night × magenta neon (genre cliché), bright white daylight pop (weak at night, glaring in dim rooms) |
| D-018 | 2026-07-18 | Onboarding order: API key → locale → Keeper name → Mate naming (in first conversation); `requirements.md` §4.10 amended | The first Mate conversation must run in the chosen locale, so locale must precede it | Original §4.10 order (locale last — first conversation's language undefined) |
