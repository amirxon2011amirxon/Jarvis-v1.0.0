import type { ServerEvent } from "../types";

export type SocketStatus = "connecting" | "online" | "reconnecting" | "offline";

type EventHandler = (event: ServerEvent) => void;
type StatusHandler = (status: SocketStatus) => void;

const MAX_DELAY_MS = 15_000;
// The server sends a ping every 20 s. If nothing at all arrives for twice that,
// the TCP connection is half-open and must be replaced.
const LIVENESS_MS = 45_000;

export class AzeSocket {
  private socket: WebSocket | null = null;
  private timer: number | null = null;
  private livenessTimer: number | null = null;
  private lastMessageAt = 0;
  private attempt = 0;
  private closed = false;
  private readonly onEvent: EventHandler;
  private readonly onStatus: StatusHandler;

  private startLiveness(socket: WebSocket): void {
    this.stopLiveness();
    this.lastMessageAt = Date.now();
    this.livenessTimer = window.setInterval(() => {
      if (this.socket !== socket || socket.readyState !== WebSocket.OPEN) {
        return;
      }
      if (Date.now() - this.lastMessageAt > LIVENESS_MS) {
        socket.close();
      }
    }, 15_000);
  }

  private stopLiveness(): void {
    if (this.livenessTimer !== null) {
      window.clearInterval(this.livenessTimer);
      this.livenessTimer = null;
    }
  }

  constructor(onEvent: EventHandler, onStatus: StatusHandler) {
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    window.addEventListener("online", this.handleOnline);
  }

  connect(): void {
    this.closed = false;
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN ||
        this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.onStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.lastMessageAt = Date.now();
      this.startLiveness(socket);
      this.onStatus("online");
    };

    socket.onmessage = (message) => {
      this.lastMessageAt = Date.now();
      let payload: unknown;
      try {
        payload = JSON.parse(String(message.data)) as unknown;
      } catch {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      const event = payload as ServerEvent;
      if (typeof event.type !== "string") return;
      if (event.type === "ping") {
        this.send({ type: "pong" });
        return;
      }
      if (event.type === "pong") return;
      this.onEvent(event);
    };

    socket.onerror = () => {
      socket.close();
    };

    socket.onclose = (event) => {
      // A socket that was already replaced (a manual reconnect or the
      // "online" handler) must not overwrite the healthy connection's status
      // or schedule another reconnect.
      if (this.socket !== socket) return;
      this.socket = null;
      this.stopLiveness();
      if (this.closed) return;
      if (event.code === 1008) {
        this.onStatus("offline");
        return;
      }
      this.onStatus("reconnecting");
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.closed || this.timer !== null) return;
    this.attempt += 1;
    const base = Math.min(MAX_DELAY_MS, 750 * 2 ** (this.attempt - 1));
    const delay = base + Math.random() * 500;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  send(payload: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  sync(): void {
    this.send({ type: "sync" });
  }

  close(): void {
    this.closed = true;
    window.removeEventListener("online", this.handleOnline);
    this.stopLiveness();
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    this.socket?.close();
    this.socket = null;
  }

  private handleOnline = (): void => {
    if (!this.closed && this.socket?.readyState !== WebSocket.OPEN) {
      if (this.timer !== null) {
        window.clearTimeout(this.timer);
        this.timer = null;
      }
      this.attempt = 0;
      this.connect();
    }
  };
}
