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
    this._renderStateBanner();
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

  // Degraded-state notices (#27): plain flow content above the tab
  // bodies, visible no matter which tab is active. Never a modal — the
  // map and the active tab stay reachable through every state.
  _renderStateBanner() {
    this.stateBanner = document.createElement("div");
    this.stateBanner.className = "state-banner";

    this.fogLine = document.createElement("p");
    this.fogLine.className = "state-line fog-line hidden";
    this.fogLine.textContent = t("state.fog");
    this.fogLine.addEventListener("click", () =>
      this.fogDetail.classList.toggle("hidden"),
    );
    this.fogDetail = document.createElement("p");
    this.fogDetail.className = "state-detail hidden";

    this.lowPowerBanner = document.createElement("p");
    this.lowPowerBanner.className = "state-line low-power-banner hidden";
    this.lowPowerBanner.textContent = t("state.low_power");

    this.hardStopBanner = document.createElement("div");
    this.hardStopBanner.className = "hard-stop-banner hidden";
    const hardStopText = document.createElement("p");
    hardStopText.textContent = t("state.hard_stop");
    const settingsLink = document.createElement("button");
    settingsLink.className = "hud-button hard-stop-settings-link";
    settingsLink.textContent = t("tab.settings");
    settingsLink.addEventListener("click", () => this.switchTab("settings"));
    this.hardStopBanner.append(hardStopText, settingsLink);

    this.stateBanner.append(
      this.fogLine,
      this.fogDetail,
      this.lowPowerBanner,
      this.hardStopBanner,
    );
    this.root.append(this.stateBanner);
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
      if (tab.id === "signal_tower") {
        this._buildBoardBody(body);
      }
      if (tab.id === "logbook") {
        this._buildLogbookBody(body);
      }
      if (tab.id === "mail") {
        this._buildMailBody(body);
      }
      if (tab.id === "notes") {
        this._buildNotesBody(body);
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
    this.searchFlavor = document.createElement("p");
    this.searchFlavor.className = "empty-note search-flavor hidden";
    this.searchFlavor.textContent = t("mate.searching");
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
    body.append(
      this.presenceLine,
      this.emptyNote,
      this.searchFlavor,
      this.chatMessages,
      this.chatInput,
    );
  }

  applySearchFlavor() {
    this.emptyNote.classList.add("hidden");
    this.searchFlavor.classList.remove("hidden");
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
    this.searchFlavor.classList.add("hidden");
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
    this.searchFlavor.classList.add("hidden");
    if (this.streamingBubble) {
      this.streamingText.textContent = frame.text;
      this.streamingCaret.remove();
      this.streamingBubble = null;
    }
    if (frame.filed_note_title) {
      const filedLine = document.createElement("p");
      filedLine.className = "filed-note-line";
      filedLine.textContent = t("mate.filed_note", {
        title: frame.filed_note_title,
      });
      this.chatMessages.append(filedLine);
    }
    this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
  }

  applyDigest(frame) {
    this.emptyNote.classList.add("hidden");
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble mate-bubble";
    bubble.textContent = frame.text;
    this.chatMessages.append(bubble);
    this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
  }

  applyPresence(frame) {
    this.presenceLine.textContent = frame.talking_with
      ? t("mate.presence.talking", { peer: frame.talking_with })
      : t("mate.presence.at", { place: frame.place ?? "" });
  }

  _buildBoardBody(body) {
    this.boardComposer = document.createElement("textarea");
    this.boardComposer.className = "chat-input";
    this.boardComposer.rows = 2;
    this.boardComposer.placeholder = t("board.compose.placeholder");
    const postButton = document.createElement("button");
    postButton.className = "hud-button";
    postButton.textContent = t("board.post");
    postButton.addEventListener("click", () => this._submitBoardPost());
    this.boardList = document.createElement("div");
    this.boardList.className = "board-list";
    body.append(this.boardComposer, postButton, this.boardList);
    window.addEventListener("peerport:board-updated", () => this.refreshBoard());
  }

  async _submitBoardPost() {
    const text = this.boardComposer.value.trim();
    if (!text) {
      this.boardComposer.classList.add("invalid");
      return;
    }
    this.boardComposer.classList.remove("invalid");
    const response = await fetch("/api/board", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: text }),
    });
    if (response.ok) {
      this.boardComposer.value = "";
      await this.refreshBoard();
    }
  }

  async refreshBoard() {
    const response = await fetch("/api/board");
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    this.boardList.replaceChildren();
    if (!data.posts.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = t("board.empty");
      this.boardList.append(empty);
      return;
    }
    for (const post of data.posts) {
      const row = document.createElement("div");
      row.className = "board-post";
      const chip = document.createElement("span");
      chip.className = "author-chip";
      if (post.author_id === "keeper") {
        chip.textContent = t("board.author.keeper");
      } else {
        const face = document.createElement("span");
        face.className = "tab-icon";
        chip.append(face, document.createTextNode(post.author_id));
      }
      const text = document.createElement("p");
      text.textContent = post.body;
      row.append(chip, text);
      this.boardList.append(row);
    }
  }

  _buildLogbookBody(body) {
    this.logbookEmptyNote = document.createElement("p");
    this.logbookEmptyNote.className = "empty-note hidden";
    this.logbookEmptyNote.textContent = t("logbook.empty");

    const whileAwayTitle = document.createElement("h4");
    whileAwayTitle.textContent = t("logbook.while_away");
    this.logbookWhileAwayList = document.createElement("div");
    this.logbookWhileAwayList.className = "logbook-list";

    const chronicleTitle = document.createElement("h4");
    chronicleTitle.textContent = t("logbook.chronicle");
    this.logbookChronicleList = document.createElement("div");
    this.logbookChronicleList.className = "logbook-list";

    body.append(
      this.logbookEmptyNote,
      whileAwayTitle,
      this.logbookWhileAwayList,
      chronicleTitle,
      this.logbookChronicleList,
    );
    window.addEventListener("peerport:logbook-updated", () => {
      this.refreshLogbook();
      if (this.activeTab !== "logbook") {
        this.setUnread("logbook", true);
      }
    });
  }

  async refreshLogbook() {
    const response = await fetch("/api/logbook");
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    this.logbookWhileAwayList.replaceChildren();
    this.logbookChronicleList.replaceChildren();
    this.logbookEmptyNote.classList.toggle(
      "hidden",
      Boolean(data.while_away.length || data.chronicle.length),
    );
    for (const entry of data.while_away) {
      const row = document.createElement("p");
      row.className = "logbook-entry";
      row.textContent = entry.text;
      this.logbookWhileAwayList.append(row);
    }
    for (const day of data.chronicle) {
      const group = document.createElement("div");
      group.className = "logbook-day";
      const heading = document.createElement("h5");
      heading.textContent = t("hud.day", { n: day.day });
      group.append(heading);
      for (const text of day.entries) {
        const row = document.createElement("p");
        row.className = "logbook-entry";
        row.textContent = text;
        group.append(row);
      }
      this.logbookChronicleList.append(group);
    }
  }

  _buildMailBody(body) {
    this.mailEmptyNote = document.createElement("p");
    this.mailEmptyNote.className = "empty-note hidden";
    this.mailEmptyNote.textContent = t("mail.empty");

    this.mailList = document.createElement("div");
    this.mailList.className = "mail-list";

    this.mailDetail = document.createElement("div");
    this.mailDetail.className = "mail-detail hidden";

    body.append(this.mailEmptyNote, this.mailList, this.mailDetail);
    window.addEventListener("peerport:mail-updated", () => {
      this.refreshMail();
      if (this.activeTab !== "mail") {
        this.setUnread("mail", true);
      }
    });
  }

  async refreshMail() {
    const response = await fetch("/api/mail");
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    this.mailEmptyNote.classList.toggle("hidden", Boolean(data.mails.length));
    this.mailList.replaceChildren();
    for (const mail of data.mails) {
      const row = document.createElement("div");
      row.className = `mail-card sender-${mail.friend_id}`;
      if (!mail.read) {
        const dot = document.createElement("span");
        dot.className = "unread-dot";
        row.append(dot);
      }
      const sender = document.createElement("span");
      sender.className = "author-chip";
      sender.textContent = mail.friend_id;
      const subject = document.createElement("p");
      subject.textContent = mail.subject;
      const date = document.createElement("span");
      date.className = "mail-date";
      date.textContent =
        mail.world_day != null ? t("hud.day", { n: mail.world_day }) : "";
      row.append(sender, date, subject);
      row.addEventListener("click", () => this._openMail(mail));
      this.mailList.append(row);
    }
  }

  async _openMail(mail) {
    if (!mail.read) {
      await fetch(`/api/mail/${mail.id}/read`, { method: "POST" });
      await this.refreshMail();
    }
    this.mailDetail.classList.remove("hidden");
    this.mailDetail.replaceChildren();
    const body = document.createElement("p");
    body.textContent = mail.body;
    const composer = document.createElement("textarea");
    composer.className = "chat-input";
    composer.rows = 2;
    composer.placeholder = t("mail.reply.placeholder");
    const sendButton = document.createElement("button");
    sendButton.className = "hud-button";
    sendButton.textContent = t("mail.send");
    sendButton.addEventListener("click", async () => {
      const text = composer.value.trim();
      if (!text) {
        return;
      }
      await fetch(`/api/mail/${mail.id}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      composer.value = "";
    });
    this.mailDetail.append(body, composer, sendButton);
  }

  _buildNotesBody(body) {
    this.notesEmptyNote = document.createElement("p");
    this.notesEmptyNote.className = "empty-note hidden";
    this.notesEmptyNote.textContent = t("notes.empty", { mate: t("tab.mate") });

    this.notesList = document.createElement("div");
    this.notesList.className = "notes-list";

    this.notesDetail = document.createElement("div");
    this.notesDetail.className = "notes-detail hidden";

    body.append(this.notesEmptyNote, this.notesList, this.notesDetail);
    window.addEventListener("peerport:notes-updated", () => this.refreshNotes());
  }

  async refreshNotes() {
    const response = await fetch("/api/notes");
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    this.notesEmptyNote.classList.toggle("hidden", Boolean(data.notes.length));
    this.notesList.replaceChildren();
    for (const note of data.notes) {
      const row = document.createElement("div");
      row.className = "notes-card";
      const title = document.createElement("p");
      title.className = "notes-title";
      title.textContent = note.title;
      const meta = document.createElement("span");
      meta.className = "notes-meta";
      meta.textContent = `${note.updated_date} · ${t("notes.filed_by", {
        mate: t("tab.mate"),
      })}`;
      const summary = document.createElement("p");
      summary.textContent = note.summary;
      row.append(title, meta, summary);
      row.addEventListener("click", () => this._openNote(note.note_id));
      this.notesList.append(row);
    }
  }

  async _openNote(noteId) {
    const response = await fetch(`/api/notes/${noteId}`);
    if (!response.ok) {
      return;
    }
    const detail = await response.json();
    this.notesDetail.classList.remove("hidden");
    this.notesDetail.replaceChildren();
    const content = document.createElement("pre");
    content.className = "notes-content";
    content.textContent = detail.content_markdown;
    const deleteButton = document.createElement("button");
    deleteButton.className = "hud-button notes-delete-button";
    deleteButton.textContent = t("notes.delete");
    const confirmRow = document.createElement("div");
    confirmRow.className = "notes-delete-confirm hidden";
    const confirmText = document.createElement("span");
    confirmText.textContent = t("notes.delete.confirm");
    const confirmButton = document.createElement("button");
    confirmButton.className = "hud-button notes-delete-confirm-button";
    confirmButton.textContent = t("notes.delete");
    confirmButton.addEventListener("click", async () => {
      await fetch(`/api/notes/${noteId}`, { method: "DELETE" });
      this.notesDetail.classList.add("hidden");
      await this.refreshNotes();
    });
    confirmRow.append(confirmText, confirmButton);
    deleteButton.addEventListener("click", () => {
      confirmRow.classList.remove("hidden");
    });
    this.notesDetail.append(content, deleteButton, confirmRow);
  }

  async openPeerPopup(peerId) {
    const response = await fetch(`/api/peer/${peerId}`);
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    for (const existing of document.querySelectorAll(".popup")) {
      existing.remove();
    }
    const popup = document.createElement("div");
    popup.className = "popup peer-popup";
    const header = document.createElement("h3");
    header.textContent = `${data.name} · ${t(`popup.kind.${data.kind}`)}`;
    const mood = document.createElement("p");
    mood.className = "empty-note";
    mood.textContent = data.mood ?? "";
    const tiesTitle = document.createElement("h4");
    tiesTitle.textContent = t("popup.ties");
    const arrows = { up: "\u2197", flat: "\u2192", down: "\u2198" };
    const ties = document.createElement("ul");
    for (const tie of data.ties) {
      const item = document.createElement("li");
      item.textContent = `${tie.peer} — ${tie.label} ${arrows[tie.trend] ?? ""}`;
      ties.append(item);
    }
    const latelyTitle = document.createElement("h4");
    latelyTitle.textContent = t("popup.lately");
    const lately = document.createElement("ul");
    for (const line of data.lately) {
      const item = document.createElement("li");
      item.textContent = line;
      lately.append(item);
    }
    popup.append(header, mood, tiesTitle, ties, latelyTitle, lately);
    popup.addEventListener("click", (event) => event.stopPropagation());
    this.mapPane.append(popup);
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
    window.addEventListener("peerport:open-signal-tower", () => {
      this.switchTab("signal_tower");
      this.refreshBoard();
    });
    window.addEventListener("peerport:peer-selected", (event) =>
      this.openPeerPopup(event.detail.peerId),
    );
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".peer-popup")) {
        for (const popup of document.querySelectorAll(".peer-popup")) {
          popup.remove();
        }
      }
    });
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
    if (tabId === "logbook") {
      this.refreshLogbook();
    }
    if (tabId === "mail") {
      this.refreshMail();
    }
    if (tabId === "notes") {
      this.refreshNotes();
    }
    if (UNREAD_ELIGIBLE.has(tabId)) {
      this.setUnread(tabId, false);
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
    if (typeof frame.low_power === "boolean") {
      this.spendChip.classList.toggle("low-power", frame.low_power);
      this.lowPowerBanner.classList.toggle("hidden", !frame.low_power);
    }
    this._refreshHud();
  }

  setReconnecting(reconnecting) {
    this.reconnectChip.classList.toggle("hidden", !reconnecting);
  }

  // Degraded-state wire frames (#27): `{"t": "state", "state": ...}`.
  // Fog and hard-stop reuse the existing "state" frame type; see
  // net.js/index.html for the dispatch into this method.
  applyStateFrame(frame) {
    if (frame.state === "fog") {
      this._applyFogState(frame.active, frame.status);
    } else if (frame.state === "hard_stop") {
      this._applyHardStop(true);
    } else if (frame.state === "resumed") {
      this._applyHardStop(false);
    }
  }

  _applyFogState(active, status) {
    this.fogLine.classList.toggle("hidden", !active);
    this.fogDetail.classList.add("hidden");
    if (active) {
      this.fogDetail.textContent = t("state.fog.detail", {
        status: String(status ?? ""),
      });
    }
  }

  _applyHardStop(active) {
    this.hardStopBanner.classList.toggle("hidden", !active);
    this.spendChip.classList.toggle("hard-stop", active);
    if (active) {
      this.paused = true;
      this._refreshHud();
    }
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
