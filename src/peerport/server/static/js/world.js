// world.js — PixiJS map renderer (#14).
//
// Pixel-perfect tile map at integer zoom (2x/3x by viewport height,
// SCALE_MODES.NEAREST), server-authoritative movement interpolated
// client-side, day-band tint crossfade, the lighthouse beam sweep
// (D-014), camera pan/clamp, and interaction events. This module never
// renders popups or switches tabs itself — it emits
// "peerport:peer-selected" and "peerport:open-signal-tower" events.

export const TILE = 16;
export const SPRITE_W = 16;
export const SPRITE_H = 24;
export const MAP_COLS = 40;
export const MAP_ROWS = 30;
export const TICK_MS = 500;
export const BEAM_SWEEP_MS = 24000;
export const PAN_DISABLE_MIN_PX = 1400;

// Placeholder tile colors on the master palette until #28's sheets land.
const TILE_COLORS = {
  "#": 0x0b141b,
  ",": 0x16323e,
  ".": 0x24485a,
  "=": 0x2b5468,
  "~": 0x0e2733,
  o: 0xffb454,
  T: 0x3fd2c7,
  L: 0xe8f1f2,
  1: 0x9fb8be,
  2: 0xe5735a,
  3: 0xffb454,
  D: 0x1c3a48,
  M: 0x9fb8be,
};

const PEER_COLORS = {
  beacon: 0xffb454,
  tug: 0xe5735a,
  bell: 0x3fd2c7,
  echo: 0x9fb8be,
};

// Day-band tint overlay (map container only, never the Bridge).
export const BAND_TINTS = {
  morning: { color: 0xffd9a8, alpha: 0.12 },
  day: { color: 0xffffff, alpha: 0 },
  dusk: { color: 0xff9868, alpha: 0.18 },
  night: { color: 0x1b3a66, alpha: 0.35 },
};

// Beam peak opacity per band; #FFB454 fading to transparent.
export const BEAM_COLOR = 0xffb454;
export const BEAM_ALPHAS = { morning: 0.06, day: 0.06, dusk: 0.14, night: 0.22 };

const TINT_CROSSFADE_MS = 30 * 60 * 1000; // ~30 world-minutes (1s = 1s)
const DOCK_SQUARE = { col: 18, row: 14 };
const LIGHTHOUSE = { col: 7, row: 16 };

// LLM/API outage fog (#27, D-006): desaturated gray-teal at 40% max
// opacity. Per map-layout.md's simulation hooks, "the sea fogs before
// the town does" — the `~`/`M` tiles ramp in faster than everything
// else so the pooling is visible in the first couple of seconds.
export const FOG_COLOR = 0x5a7a82;
export const FOG_TARGET_ALPHA = 0.4;
const FOG_SEA_TILES = new Set(["~", "M"]);
const FOG_SEA_RAMP_MS = 2000;
const FOG_TOWN_RAMP_MS = 6000;

export function zoomForViewport(height) {
  return height >= 3 * MAP_ROWS * TILE ? 3 : 2;
}

export function lerp(a, b, ratio) {
  return a + (b - a) * Math.min(Math.max(ratio, 0), 1);
}

export class WorldRenderer {
  constructor(pane) {
    this.pane = pane;
    this.peers = new Map(); // id -> {sprite, label, bubble, from, to, movedAt}
    this.band = "day";
    this.tintFrom = { ...BAND_TINTS.day };
    this.tintTo = { ...BAND_TINTS.day };
    this.tintChangedAt = 0;
    // Fog overlay state (#27): target alpha the two fog layers ramp
    // toward, and the "from" values/timestamp driving that ramp.
    this.fogTarget = 0;
    this.seaFogFrom = 0;
    this.townFogFrom = 0;
    this.fogChangedAt = 0;
    // Hard-cap night-still frame (#27): forces the Night tint regardless
    // of the actual band while true; cleared on a "resumed" state frame.
    this.hardStop = false;
    this.reducedMotion =
      typeof matchMedia !== "undefined" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  async init() {
    const response = await fetch("/api/map");
    this.map = await response.json();

    PIXI.BaseTexture.defaultOptions.scaleMode = PIXI.SCALE_MODES.NEAREST;
    this.zoom = zoomForViewport(this.pane.clientHeight);
    this.app = new PIXI.Application({
      resizeTo: this.pane,
      background: 0x0e2733,
      antialias: false,
    });
    this.pane.append(this.app.view);

    this.camera = new PIXI.Container();
    this.camera.scale.set(this.zoom);
    this.app.stage.addChild(this.camera);

    this._buildTiles();
    this._buildBeam();
    this._buildTint();
    this._buildFog();
    this._bindCamera();
    this.centerOn(DOCK_SQUARE.col, DOCK_SQUARE.row);
    this.app.ticker.add(() => this._frame());
    addEventListener("resize", () => this._applyZoom());
  }

  _buildTiles() {
    const tiles = new PIXI.Graphics();
    this.map.ground.forEach((row, r) => {
      [...row].forEach((ch, c) => {
        tiles.beginFill(TILE_COLORS[ch] ?? 0x16323e);
        tiles.drawRect(c * TILE, r * TILE, TILE, TILE);
        tiles.endFill();
      });
    });
    tiles.eventMode = "static";
    tiles.on("pointertap", (event) => {
      const point = event.getLocalPosition(tiles);
      const col = Math.floor(point.x / TILE);
      const row = Math.floor(point.y / TILE);
      if (this._inZone("signal_tower", col, row)) {
        dispatchEvent(new CustomEvent("peerport:open-signal-tower"));
      }
    });
    this.camera.addChild(tiles);
    this.peerLayer = new PIXI.Container();
    this.camera.addChild(this.peerLayer);
  }

  _inZone(zone, col, row) {
    return (this.map.zones[zone] ?? []).some(([zc, zr]) => zc === col && zr === row);
  }

  _buildBeam() {
    this.beam = new PIXI.Graphics();
    const length = 14 * TILE;
    this.beam.beginFill(BEAM_COLOR, 1);
    this.beam.moveTo(0, 0);
    this.beam.lineTo(length, -2.2 * TILE);
    this.beam.lineTo(length, 2.2 * TILE);
    this.beam.closePath();
    this.beam.endFill();
    this.beam.blendMode = PIXI.BLEND_MODES.ADD;
    this.beam.position.set(
      LIGHTHOUSE.col * TILE + TILE / 2,
      LIGHTHOUSE.row * TILE + TILE / 2,
    );
    this.beam.alpha = BEAM_ALPHAS.day;
    this.camera.addChild(this.beam);

    this.lensGlow = new PIXI.Graphics();
    this.lensGlow.beginFill(BEAM_COLOR, 0.5);
    this.lensGlow.drawCircle(0, 0, TILE);
    this.lensGlow.endFill();
    this.lensGlow.blendMode = PIXI.BLEND_MODES.ADD;
    this.lensGlow.position.copyFrom(this.beam.position);
    this.lensGlow.visible = this.reducedMotion;
    this.beam.visible = !this.reducedMotion;
    this.camera.addChild(this.lensGlow);
  }

  _buildTint() {
    this.tint = new PIXI.Graphics();
    this.tint.beginFill(0xffffff);
    this.tint.drawRect(0, 0, MAP_COLS * TILE, MAP_ROWS * TILE);
    this.tint.endFill();
    this.tint.alpha = 0;
    this.camera.addChild(this.tint);
  }

  // Two fog layers so the sea (`~`/`M`) can pool ahead of the town, per
  // map-layout.md's simulation hooks (#27). Both start fully transparent;
  // `applyStateFrame`/`_frame` ramp them toward `fogTarget` on an outage.
  _buildFog() {
    const seaFog = new PIXI.Graphics();
    const townFog = new PIXI.Graphics();
    this.map.ground.forEach((row, r) => {
      [...row].forEach((ch, c) => {
        const layer = FOG_SEA_TILES.has(ch) ? seaFog : townFog;
        layer.beginFill(FOG_COLOR);
        layer.drawRect(c * TILE, r * TILE, TILE, TILE);
        layer.endFill();
      });
    });
    seaFog.alpha = 0;
    townFog.alpha = 0;
    this.camera.addChild(seaFog, townFog);
    this.seaFog = seaFog;
    this.townFog = townFog;
  }

  _bindCamera() {
    let dragging = null;
    this.app.stage.eventMode = "static";
    this.app.stage.hitArea = this.app.screen;
    this.app.stage.on("pointerdown", (event) => {
      if (this._panDisabled()) {
        return;
      }
      dragging = {
        x: event.global.x - this.camera.x,
        y: event.global.y - this.camera.y,
      };
    });
    this.app.stage.on("pointermove", (event) => {
      if (dragging) {
        this.camera.position.set(
          event.global.x - dragging.x,
          event.global.y - dragging.y,
        );
        this._clampCamera();
      }
    });
    const stop = () => {
      dragging = null;
    };
    this.app.stage.on("pointerup", stop);
    this.app.stage.on("pointerupoutside", stop);
    this.app.view.addEventListener("dblclick", () =>
      this.centerOn(DOCK_SQUARE.col, DOCK_SQUARE.row),
    );
  }

  _panDisabled() {
    return (
      innerWidth >= PAN_DISABLE_MIN_PX && innerHeight >= PAN_DISABLE_MIN_PX
    );
  }

  centerOn(col, row) {
    this.camera.position.set(
      this.app.screen.width / 2 - (col * TILE + TILE / 2) * this.zoom,
      this.app.screen.height / 2 - (row * TILE + TILE / 2) * this.zoom,
    );
    this._clampCamera();
  }

  _clampCamera() {
    const mapW = MAP_COLS * TILE * this.zoom;
    const mapH = MAP_ROWS * TILE * this.zoom;
    const minX = Math.min(0, this.app.screen.width - mapW);
    const minY = Math.min(0, this.app.screen.height - mapH);
    this.camera.x = Math.min(Math.max(this.camera.x, minX), Math.max(0, minX));
    this.camera.y = Math.min(Math.max(this.camera.y, minY), Math.max(0, minY));
    if (mapW <= this.app.screen.width) {
      this.camera.x = (this.app.screen.width - mapW) / 2;
    }
    if (mapH <= this.app.screen.height) {
      this.camera.y = (this.app.screen.height - mapH) / 2;
    }
  }

  _applyZoom() {
    this.zoom = zoomForViewport(this.pane.clientHeight);
    this.camera.scale.set(this.zoom);
    this._clampCamera();
  }

  applySnapshot(snapshot) {
    for (const { sprite } of this.peers.values()) {
      sprite.destroy({ children: true });
    }
    this.peers.clear();
    for (const [peerId, pos] of Object.entries(snapshot.peers)) {
      this._upsertPeer(peerId, pos, { snap: true });
    }
  }

  applyDiff(diff) {
    for (const [peerId, pos] of Object.entries(diff.peers ?? {})) {
      if (pos === null) {
        this._removePeer(peerId);
      } else {
        this._upsertPeer(peerId, pos, { snap: false });
      }
    }
  }

  applyClockFrame(frame) {
    if (frame.band !== this.band) {
      this.tintFrom = { ...this.tintTo };
      this.tintTo = BAND_TINTS[frame.band] ?? BAND_TINTS.day;
      this.tintChangedAt = performance.now();
      this.band = frame.band;
    }
  }

  // Degraded-state wire frames (#27): `{"t": "state", "state": ...}`.
  // Peer movement is untouched here — an outage only ever adds the fog
  // overlay; only a hard-cap trip (owned by #16, consumed here) freezes
  // the world, and it does so by the server simply no longer sending
  // diffs while `simulation.paused` — this only forces the tint lock.
  applyStateFrame(frame) {
    if (frame.state === "fog") {
      this.seaFogFrom = this.seaFog.alpha;
      this.townFogFrom = this.townFog.alpha;
      this.fogTarget = frame.active ? FOG_TARGET_ALPHA : 0;
      this.fogChangedAt = performance.now();
    } else if (frame.state === "hard_stop") {
      this.hardStop = true;
    } else if (frame.state === "resumed") {
      this.hardStop = false;
    }
  }

  // Resync on every (re)connect (finding): the snapshot is the
  // guaranteed first message on every connection (net.js), so a
  // reconnecting client picks up the current fog/hard-stop status
  // instead of only ever learning about it from a live `state` frame it
  // may have missed while disconnected. Reuses `applyStateFrame`'s own
  // logic rather than duplicating the fog-ramp/tint-lock bookkeeping.
  applyDegradedSnapshot(frame) {
    if (frame.fog) {
      this.applyStateFrame({
        state: "fog",
        active: frame.fog.active,
        status: frame.fog.status,
      });
    }
    this.applyStateFrame({ state: frame.hard_stop ? "hard_stop" : "resumed" });
  }

  showSpeech(peerId, text) {
    const entry = this.peers.get(peerId);
    if (!entry) {
      return;
    }
    entry.bubble?.destroy({ children: true });
    const bubble = new PIXI.Container();
    const label = new PIXI.Text(text, {
      fontSize: 7,
      fill: 0x101d26,
      wordWrap: true,
      wordWrapWidth: 6 * TILE,
    });
    const bg = new PIXI.Graphics();
    bg.beginFill(0xe8f1f2, 0.95);
    bg.drawRoundedRect(-2, -2, label.width + 4, label.height + 4, 2);
    bg.endFill();
    bubble.addChild(bg, label);
    bubble.position.set(0, -SPRITE_H - label.height);
    // Two-frame pop-in: whole content, no typewriter streaming (REQ-016).
    // Reduced motion (prototype-design.md §8.3): bubbles are static, so
    // skip the scale ramp and show the bubble at full size immediately.
    if (this.reducedMotion) {
      bubble.scale.set(1);
    } else {
      bubble.scale.set(0.5);
      requestAnimationFrame(() =>
        requestAnimationFrame(() => bubble.scale.set(1)),
      );
    }
    entry.sprite.addChild(bubble);
    entry.bubble = bubble;
    clearTimeout(entry.bubbleTimer);
    entry.bubbleTimer = setTimeout(() => {
      bubble.destroy({ children: true });
      entry.bubble = null;
    }, 6000);
  }

  _upsertPeer(peerId, pos, { snap }) {
    let entry = this.peers.get(peerId);
    if (!entry) {
      const sprite = new PIXI.Container();
      const body = new PIXI.Graphics();
      body.beginFill(PEER_COLORS[peerId] ?? 0xe8f1f2);
      body.drawRect(0, -(SPRITE_H - TILE), SPRITE_W, SPRITE_H);
      body.endFill();
      sprite.addChild(body);
      sprite.eventMode = "static";
      sprite.cursor = "pointer";
      sprite.on("pointertap", () =>
        dispatchEvent(
          new CustomEvent("peerport:peer-selected", { detail: { peerId } }),
        ),
      );
      sprite.on("pointerover", (event) => {
        if (event.pointerType === "mouse") {
          this._showNameLabel(entry, peerId);
        }
      });
      sprite.on("pointerout", () => this._hideNameLabel(entry));
      this.peerLayer.addChild(sprite);
      entry = { sprite, from: pos, to: pos, movedAt: 0, bubble: null };
      this.peers.set(peerId, entry);
    }
    entry.from = snap ? pos : { ...entry.to };
    entry.to = pos;
    entry.movedAt = snap ? 0 : performance.now();
  }

  _removePeer(peerId) {
    const entry = this.peers.get(peerId);
    if (entry) {
      entry.sprite.destroy({ children: true });
      this.peers.delete(peerId);
    }
  }

  _showNameLabel(entry, peerId) {
    if (!entry || entry.nameLabel) {
      return;
    }
    const label = new PIXI.Text(peerId, { fontSize: 7, fill: 0xe8f1f2 });
    label.position.set(0, -SPRITE_H - 9);
    entry.sprite.addChild(label);
    entry.nameLabel = label;
  }

  _hideNameLabel(entry) {
    if (entry?.nameLabel) {
      entry.nameLabel.destroy();
      entry.nameLabel = null;
    }
  }

  _frame() {
    const now = performance.now();
    for (const entry of this.peers.values()) {
      const ratio = entry.movedAt ? (now - entry.movedAt) / TICK_MS : 1;
      const x = lerp(entry.from.pos_x, entry.to.pos_x, ratio) * TILE;
      const y = lerp(entry.from.pos_y, entry.to.pos_y, ratio) * TILE;
      entry.sprite.position.set(x, y - (TILE - SPRITE_W) / 2);
    }
    if (this.hardStop) {
      // Night-still frame (REQ-007): lock to Night's exact tint
      // regardless of the actual band while the hard cap holds.
      this.tint.tint = BAND_TINTS.night.color;
      this.tint.alpha = BAND_TINTS.night.alpha;
    } else if (this.reducedMotion) {
      // Reduced motion (prototype-design.md §8.3): tint crossfades snap
      // instantly to the target instead of animating over ~30 world-min.
      this.tint.tint = this.tintTo.color;
      this.tint.alpha = this.tintTo.alpha;
    } else {
      const fade = this.tintChangedAt
        ? Math.min((now - this.tintChangedAt) / TINT_CROSSFADE_MS, 1)
        : 1;
      this.tint.tint = fade < 1 ? this.tintFrom.color : this.tintTo.color;
      this.tint.alpha = lerp(this.tintFrom.alpha, this.tintTo.alpha, fade);
    }
    if (!this.reducedMotion) {
      this.beam.rotation = ((now % BEAM_SWEEP_MS) / BEAM_SWEEP_MS) * Math.PI * 2;
      this.beam.alpha = BEAM_ALPHAS[this.band] ?? BEAM_ALPHAS.day;
    }
    this.lensGlow.alpha = (BEAM_ALPHAS[this.band] ?? 0.06) * 2;

    if (this.fogChangedAt) {
      const seaRatio = Math.min((now - this.fogChangedAt) / FOG_SEA_RAMP_MS, 1);
      const townRatio = Math.min((now - this.fogChangedAt) / FOG_TOWN_RAMP_MS, 1);
      this.seaFog.alpha = lerp(this.seaFogFrom, this.fogTarget, seaRatio);
      this.townFog.alpha = lerp(this.townFogFrom, this.fogTarget, townRatio);
    }
  }
}

export async function initWorld() {
  const renderer = new WorldRenderer(document.getElementById("map-pane"));
  await renderer.init();
  return renderer;
}
