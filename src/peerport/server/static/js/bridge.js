// bridge.js — Bridge shell: tab rail, HUD, spend chip, reconnect chip (#15).
//
// Every user-visible string resolves through i18n.t(); tab bodies are
// empty placeholders for M1 (their content is owned by later issues).

import { t } from "./i18n.js";

export const TABS = [
  { id: "mate", labelKey: "tab.mate" },
  { id: "mail", labelKey: "tab.mail" },
  { id: "signal_tower", labelKey: "tab.signal_tower" },
  { id: "logbook", labelKey: "tab.logbook" },
  { id: "notes", labelKey: "tab.notes" },
  { id: "settings", labelKey: "tab.settings" },
];

const UNREAD_ELIGIBLE = new Set(["mail", "logbook"]);
const MAX_INPUT_LINES = 4;

export class Bridge {
  constructor(root, mapPane) {
    this.root = root;
    this.mapPane = mapPane;
    this.activeTab = "mate";
    this.day = 1;
    this.band = "morning";
    this.paused = false;
    this.speed = 1;
    this.spendToday = "$0.00";
  }

  init() {
    this._renderRail();
    this._renderBodies();
    this._renderHud();
    this._bindKeyboard();
    this._bindWorldEvents();
    this.switchTab("mate");
  }

  _renderRail() {
    const rail = document.createElement("div");
    rail.className = "tab-rail";
    for (const tab of TABS) {
      const button = document.createElement("button");
      button.className = "tab";
      button.dataset.tab = tab.id;
      const icon = document.createElement("span");
      icon.className = "tab-icon";
      icon.setAttribute("aria-hidden", "true");
      button.append(icon, document.createTextNode(t(tab.labelKey)));
      button.addEventListener("click", () => this.switchTab(tab.id));
      rail.append(button);
    }
    this.reconnectChip = document.createElement("span");
    this.reconnectChip.className = "reconnect-chip hidden";
    this.reconnectChip.textContent = t("state.reconnecting");
    rail.append(this.reconnectChip);
    this.root.append(rail);
  }

  _renderBodies() {
    this.bodies = {};
    for (const tab of TABS) {
      const body = document.createElement("div");
      body.className = "tab-body hidden";
      body.dataset.tabBody = tab.id;
      if (tab.id === "mate") {
        this._buildMateBody(body);
      }
      this.bodies[tab.id] = body;
      this.root.append(body);
    }
  }

  _buildMateBody(body) {
    this.presenceLine = document.createElement("div");
    this.presenceLine.className = "presence-line";
    this.emptyNote = document.createElement("p");
    this.emptyNote.className = "empty-note";
    this.emptyNote.textContent = t("mate.empty");
    this.chatMessages = document.createElement("div");
    this.chatMessages.className = "chat-messages";
    this.chatInput = document.createElement("textarea");
    this.chatInput.className = "chat-input";
    this.chatInput.rows = 1;
    this.chatInput.placeholder = t("mate.input.placeholder", {
      mate: t("tab.mate"),
    });
    this.chatInput.addEventListener("input", () => {
      const lines = this.chatInput.value.split("\n").length;
      this.chatInput.rows = Math.min(Math.max(lines, 1), MAX_INPUT_LINES);
    });
    this.chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this._sendChat();
      }
    });
    body.append(this.presenceLine, this.emptyNote, this.chatMessages, this.chatInput);
  }

  async _sendChat() {
    const text = this.chatInput.value.trim();
    if (!text) {
      return;
    }
    this.emptyNote.classList.add("hidden");
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble keeper-bubble";
    bubble.textContent = text;
    this.chatMessages.append(bubble);
    this.chatInput.value = "";
    this.chatInput.rows = 1;
    await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  applyChatDelta(frame) {
    if (!this.streamingBubble) {
      this.emptyNote.classList.add("hidden");
      this.streamingBubble = document.createElement("div");
      this.streamingBubble.className = "chat-bubble mate-bubble";
      this.streamingText = document.createElement("span");
      this.streamingCaret = document.createElement("span");
      this.streamingCaret.className = "chat-caret";
      this.streamingBubble.append(this.streamingText, this.streamingCaret);
      this.chatMessages.append(this.streamingBubble);
    }
    this.streamingText.textContent += frame.text;
  }

  applyChatDone(frame) {
    if (this.streamingBubble) {
      this.streamingText.textContent = frame.text;
      this.streamingCaret.remove();
      this.streamingBubble = null;
    }
    this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
  }

  applyPresence(frame) {
    this.presenceLine.textContent = frame.talking_with
      ? t("mate.presence.talking", { peer: frame.talking_with })
      : t("mate.presence.at", { place: frame.place ?? "" });
  }

  _renderHud() {
    this.hud = document.createElement("div");
    this.hud.className = "hud-chip";
    this.hud.id = "hud";
    this.clockLabel = document.createElement("span");
    this.pauseButton = document.createElement("button");
    this.pauseButton.className = "hud-button";
    this.pauseButton.addEventListener("click", () => this._togglePause());
    this.speedButton = document.createElement("button");
    this.speedButton.className = "hud-button";
    this.speedButton.addEventListener("click", () => this._toggleSpeed());
    this.hud.append(this.clockLabel, this.pauseButton, this.speedButton);

    this.spendChip = document.createElement("div");
    this.spendChip.className = "hud-chip";
    this.spendChip.id = "spend-chip";
    this.spendChip.addEventListener("click", () => this.switchTab("settings"));
    this.mapPane.append(this.hud, this.spendChip);
    this._refreshHud();
  }

  _refreshHud() {
    this.clockLabel.textContent = `${t("hud.day", { n: this.day })} · ${t(
      `hud.band.${this.band}`,
    )}`;
    this.pauseButton.textContent = this.paused ? t("hud.resume") : t("hud.pause");
    this.speedButton.textContent = `${this.speed}x`;
    this.spendChip.textContent = t("hud.spend_today", { amount: this.spendToday });
  }

  _bindKeyboard() {
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key >= "1" && event.key <= "6") {
        event.preventDefault();
        this.switchTab(TABS[Number(event.key) - 1].id);
      }
      if (event.key === "Escape") {
        for (const popup of document.querySelectorAll(".popup")) {
          popup.remove();
        }
      }
    });
  }

  _bindWorldEvents() {
    window.addEventListener("peerport:open-signal-tower", () =>
      this.switchTab("signal_tower"),
    );
  }

  switchTab(tabId) {
    this.activeTab = tabId;
    for (const button of this.root.querySelectorAll(".tab")) {
      button.classList.toggle("active", button.dataset.tab === tabId);
    }
    for (const [id, body] of Object.entries(this.bodies)) {
      body.classList.toggle("hidden", id !== tabId);
    }
    if (tabId === "mate" && this.chatInput) {
      this.chatInput.focus();
    }
  }

  setUnread(tabId, unread) {
    if (!UNREAD_ELIGIBLE.has(tabId)) {
      return;
    }
    const button = this.root.querySelector(`.tab[data-tab="${tabId}"]`);
    const existing = button.querySelector(".unread-dot");
    if (unread && !existing) {
      const dot = document.createElement("span");
      dot.className = "unread-dot";
      button.append(dot);
    } else if (!unread && existing) {
      existing.remove();
    }
  }

  applyClockFrame(frame) {
    this.day = frame.day;
    this.band = frame.band;
    this._refreshHud();
  }

  applySpendFrame(frame) {
    this.spendToday = frame.amount;
    this._refreshHud();
  }

  setReconnecting(reconnecting) {
    this.reconnectChip.classList.toggle("hidden", !reconnecting);
  }

  async _togglePause() {
    const action = this.paused ? "resume" : "pause";
    const response = await fetch("/api/world", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (response.ok) {
      const result = await response.json();
      this.paused = result.paused;
      this._refreshHud();
    }
  }

  async _toggleSpeed() {
    const next = this.speed === 1 ? 2 : 1;
    const response = await fetch("/api/world", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "speed", speed: next }),
    });
    if (response.ok) {
      const result = await response.json();
      this.speed = result.speed;
      this._refreshHud();
    }
  }
}

export function initBridge() {
  const bridge = new Bridge(
    document.getElementById("bridge"),
    document.getElementById("map-pane"),
  );
  bridge.init();
  return bridge;
}
