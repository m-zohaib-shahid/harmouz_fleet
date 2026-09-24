// REST client. The auth token is attached as X-Auth-Token on every call, and
// also kept in sessionStorage so a refresh keeps the operator logged in.

import { apiUrl } from './runtime-config.js';

const TOKEN_KEY = 'aegis.token';

class Api {
  constructor() {
    this.token = sessionStorage.getItem(TOKEN_KEY) || '';
  }

  setToken(token) {
    this.token = token || '';
    if (this.token) sessionStorage.setItem(TOKEN_KEY, this.token);
    else sessionStorage.removeItem(TOKEN_KEY);
  }

  headers(extra = {}) {
    const headers = { 'Content-Type': 'application/json', ...extra };
    if (this.token) headers['X-Auth-Token'] = this.token;
    return headers;
  }

  async request(path, { method = 'GET', body = null, raw = false } = {}) {
    const options = { method, headers: this.headers() };
    if (body !== null) options.body = JSON.stringify(body);
    const started = performance.now();
    const response = await fetch(apiUrl(path), options);
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text };
    }
    const elapsed = performance.now() - started;
    if (!response.ok) {
      const detail =
        data && typeof data === 'object'
          ? data.detail?.[0]?.msg || data.detail || response.statusText
          : response.statusText;
      const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return raw ? { data, elapsed, response } : data;
  }

  get(path) {
    return this.request(path);
  }

  post(path, body) {
    return this.request(path, { method: 'POST', body });
  }

  del(path) {
    return this.request(path, { method: 'DELETE' });
  }

  // -- endpoints ---------------------------------------------------------
  state() {
    return this.get('/api/state');
  }
  metrics() {
    return this.get('/api/metrics');
  }
  config() {
    return this.get('/api/config');
  }
  tokens() {
    return this.get('/api/auth/tokens');
  }
  login(token) {
    return this.post('/api/auth/login', { token });
  }
  shipOptions(shipId) {
    return this.get(`/api/ships/${shipId}/options`);
  }
  async createZone(payload, awaitRoutes = false) {
    const res = await this.post(`/api/zones?await_routes=${awaitRoutes ? 'true' : 'false'}`, payload);
    return res?.zone || res;
  }
  deleteZone(zoneId) {
    return this.del(`/api/zones/${zoneId}`);
  }
  patchZone(zoneId, active) {
    return this.request(`/api/zones/${zoneId}?active=${active ? 'true' : 'false'}`, { method: 'PATCH' });
  }
  async demoStraitZone() {
    // Backend returns {zone, affected_ships, alerts, reroutes} - unwrap like createZone.
    const res = await this.post('/api/zones/demo/strait', {});
    return res?.zone || res;
  }
  async createDirective(payload) {
    const res = await this.post('/api/directives', payload);
    return res?.directive || res;
  }
  respondDirective(directiveId, accept, note = '') {
    return this.post(`/api/directives/${directiveId}/respond`, { accept, note });
  }
  reroute(shipId, destination = null, reason = null) {
    const body = {};
    if (destination) body.destination = destination;
    if (reason) body.reason = reason;
    return this.post(`/api/ships/${shipId}/reroute`, body);
  }
  async distress(shipId, text) {
    return this.request('/api/distress', {
      method: 'POST',
      body: { shipId, text, source: 'text' },
    });
  }
  drillPosition(shipId, position) {
    return this.post(`/api/ships/${shipId}/position`, { position });
  }
  forceTick() {
    return this.post('/api/sim/tick', {});
  }
}

export const api = new Api();
export { TOKEN_KEY };
