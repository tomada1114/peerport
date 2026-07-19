// net.js — WebSocket connection with exponential-backoff reconnect.
//
// Per docs/design/architecture.md §4 and requirements.md §5.2: on
// disconnect, the client retries with exponential backoff starting at
// 500ms, doubling each attempt, capped at 16000ms, and keeps retrying
// indefinitely until a connection succeeds. Every (re)connect naturally
// receives a fresh `snapshot` message first (guaranteed server-side by
// server/ws.py), never a resumed diff stream.
//
// Frontend has no automated test harness in this MVP (architecture.md
// §6: "just check covers Python only") — this module is verified by
// manual/browser inspection per the issue's verification checklist.

export const RECONNECT_INITIAL_MS = 500;
export const RECONNECT_MAX_MS = 16000;

export class PeerPortConnection {
  constructor({ url, onSnapshot, onDiff, onEvent, onDisconnect, onReconnect } = {}) {
    this.url = url ?? defaultWsUrl();
    this.onSnapshot = onSnapshot ?? (() => {});
    this.onDiff = onDiff ?? (() => {});
    this.onEvent = onEvent ?? (() => {});
    this.onDisconnect = onDisconnect ?? (() => {});
    this.onReconnect = onReconnect ?? (() => {});
    this.socket = null;
    this.reconnectDelayMs = RECONNECT_INITIAL_MS;
  }

  connect() {
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.reconnectDelayMs = RECONNECT_INITIAL_MS;
      this.onReconnect();
    });

    socket.addEventListener("message", (event) => {
      this._handleMessage(event.data);
    });

    socket.addEventListener("close", () => {
      this.onDisconnect();
      this._scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  }

  _handleMessage(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return; // Malformed server payloads are ignored client-side too.
    }
    if (message.t === "snapshot") {
      this.onSnapshot(message);
    } else if (message.t === "diff") {
      this.onDiff(message);
    } else {
      this.onEvent(message);
    }
  }

  _scheduleReconnect() {
    const delay = this.reconnectDelayMs;
    setTimeout(() => this.connect(), delay);
    this.reconnectDelayMs = Math.min(delay * 2, RECONNECT_MAX_MS);
  }
}

function defaultWsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

export function connect(options) {
  const connection = new PeerPortConnection(options);
  connection.connect();
  return connection;
}
