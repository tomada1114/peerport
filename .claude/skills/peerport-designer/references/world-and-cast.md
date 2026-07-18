# World & Cast — Design Bible

Design-relevant distillation of `requirements.md` §2–§3. When this file and
`requirements.md` disagree, `requirements.md` wins — fix this file.

## The world in one paragraph

PeerPort is a small, closed **cyber port town** facing a sea of data. AI
personas ("Peers") live there autonomously; the user ("Keeper") watches over
the harbor from a lighthouse, talking with their partner Peer ("Mate")
through a terminal called the **Bridge**. Inspired by Mega Man Battle
Network's *feel* (persistent little world, partner-on-the-other-side-of-the-
screen warmth, mail culture) — never its IP. No combat, ever. The experience
is daily life, small talk, and relationships accumulating over time.

## Tone words (use these to steer any visual or copy decision)

- **Near-future cyber-pop**: bright digital city, pixel art, neon signage —
  *not* dystopian cyberpunk. If a choice reads gritty, rainy-noir, or
  corporate-evil, it is off-tone.
- **Cozy futurism**: a harbor at dusk; signal lights; someone is always home.
- **Alive while you're away**: the world quietly persists. Design should
  reward returning (Logbook, "welcome back" moments).
- **Small and knowable**: one map, six personalities. Never design for scale
  (feeds, infinite lists, notification badges competing for attention).

## Canonical terminology

English is the default locale; Japanese is the paired translation. UI copy,
code identifiers, and docs must use these exact terms — never generic
synonyms (write "Dock In", not "connect"; "Berth", not "home").

| EN | JA | Meaning |
|---|---|---|
| PeerPort | ピアポート | The world / product name |
| Peer | ピア | Any AI persona |
| Mate | メイト | The Keeper's partner Peer |
| Keeper | キーパー | The user (lighthouse keeper) |
| Bridge | ブリッジ | The Keeper's terminal UI panel |
| Dock In / Dock Out | ドックイン/ドックアウト | Connect/disconnect to a place or device |
| Dock Square | ドックスクエア | Central plaza, social hub |
| Signal Tower | シグナルタワー | The BBS (requests, notices, chatter) |
| Berth | バース | A peer's home / mooring spot |
| Drifter | ドリフター | A keeper-less, self-owned Peer |
| Logbook | ログブック | Record of events while the Keeper was away |
| Mail | メール | Async letters from real-side friends |

## Cast sheet (visual & voice identity)

Six personas; four appear on the map. Names are defaults and renameable —
design sprites/motifs around the *role*, not the name string.

| Persona | On map | Motif & silhouette | Personality → visual/voice cues |
|---|---|---|---|
| **Beacon** (Mate) | Yes | Lighthouse: lantern glow, warm light | Reliable, warm, lightly playful. The voice the Keeper hears most — copy must be pleasant over hundreds of sessions. Runs to Signal Tower when researching (visible diligence). |
| **Tug** (Peer of Kai) | Yes | Tugboat: stout, wide, sturdy | Strong, simple, caring. Big readable silhouette, heavy idle bounce. Blunt short sentences. |
| **Bell** (Peer of Mia) | Yes | Harbor bell: rounded, tidy | Gentle, attentive. Small precise movements. Polite, considerate copy. |
| **Echo** (Drifter) | Yes | Mist/sonar: outline-y, slightly desaturated | Mysterious wanderer, information broker. Appears from the map edge, sometimes vanishes. Visually reads as "not from here". |
| **Kai** (friend) | No | Exists via Mail + hearsay | Hot-blooded, hasty childhood-friend energy. Identity carried by mail styling and Tug's "my Keeper said…" hearsay. |
| **Mia** (friend) | No | Exists via Mail + hearsay | Dependable, caring. Same: mail styling + Bell's hearsay. |

**Mirroring principle**: a Peer's design echoes its friend's personality
(Tug↔Kai, Bell↔Mia). Future pairs may instead use *complementary* pairing
(clumsy owner ↔ butler-type Peer). Keep sprite/palette kinship visible
within a pair.

## How the world speaks (copy voice)

- **Diegetic first**: system states wear in-world clothes. API outage = fog
  rolls over the harbor; budget low-power mode = "running dark to save
  power"; reconnect = "signal restored". A plain technical detail line may
  accompany, but the world-voice leads.
- **The Bridge addresses the Keeper as a colleague**, not a user. No
  exclamation-mark marketing tone, no "Oops!".
- **Peers never break character** in UI copy, but the Settings tab and error
  detail text may be plainly technical — Settings is the engine room.
- **Empty states are worldbuilding**: an empty Notes tab, day-one Logbook,
  or no-mail state each get one line of in-world flavor, not "No items yet".
- Localized copy (en/ja) must carry the same personality, not literal
  translation.

## Never

- Combat, weapons, battle UI, HP bars, dystopian decay, corporate-evil
  branding.
- Generic terms where canonical ones exist.
- New named characters or places in design docs — the cast and map locations
  are fixed by `requirements.md` (§3, §4.1); propose additions there first.
