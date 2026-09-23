// WebSocket client: auto-reconnect with backoff, RTT measurement and a tiny
// event bus. Every frame is handed to the store; alerts are also emitted so the
// UI can toast them independently of the state render pass.

export class Feed {
  constructor({ onSnapshot, onTick, onAlert, onEvent, onStatus } = {}) {
    this.handlers = { onSnapshot, onTick, onAlert, onEvent, onStatus };
    this.ws = null;
    this.attempts = 0;
    this.rttMs = null;
    this.token = '';
    this.closed = false;
    this._pingTimer = null;
  }

  url() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const host = location.host && location.host !== '' ? location.host : 'localhost:8000';
    const query = this.token ? `?token=${encodeURIComponent(this.token)}` : '';
    return `${proto}://${host}/ws${query}`;
  }

  connect(token = this.token) {
    this.token = token || '';
    this.closed = false;
    if (this.ws) {
      try {
        this.ws.onclose = null;
        this.ws.close();
      } catch {
        /* ignore */
      }
    }
    this.handlers.onStatus?.('connecting');
    const socket = new WebSocket(this.url());
    this.ws = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.handlers.onStatus?.('open');
      this.startPing();
    };

    socket.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case 'snapshot':
          this.handlers.onSnapshot?.(msg);
          break;
        case 'tick':
          this.handlers.onTick?.(msg);
          break;
        case 'alert':
          this.handlers.onAlert?.(msg.alert, msg);
          break;
        case 'pong':
          if (msg.clientTime) {
            this.rttMs = Math.round(performance.now() - Number(msg.clientTime));
            this.handlers.onStatus?.('open', this.rttMs);
          }
          break;
        default:
          this.handlers.onEvent?.(msg);
      }
    };

    socket.onclose = () => {
      this.stopPing();
      this.handlers.onStatus?.('closed');
      if (this.closed) return;
      const delay = Math.min(8000, 400 * 2 ** this.attempts++);
      setTimeout(() => this.connect(this.token), delay);
    };

    socket.onerror = () => this.handlers.onStatus?.('error');
  }

  startPing() {
    this.stopPing();
    this._pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
      }
    }, 3000);
  }

  stopPing() {
    if (this._pingTimer) clearInterval(this._pingTimer);
    this._pingTimer = null;
  }

  send(payload) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  close() {
    this.closed = true;
    this.stopPing();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
  }

  disconnect() {
    this.close();
  }
}
