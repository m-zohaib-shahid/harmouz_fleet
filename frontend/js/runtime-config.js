// Browser-side runtime configuration.
//
// Localhost connects directly to FastAPI. Public deployments connect to the
// active Microsoft DevTunnels endpoint over HTTPS/WSS.
const BUILD_BACKEND_URL = "";
const PRODUCTION_BACKEND_URL = "https://hpltn945-8000.inc1.devtunnels.ms";

// The tunnel hostname above is ephemeral: DevTunnels issues a new host every time
// the backend restarts, and a stale one fails the TLS handshake. Operators can
// repoint a deployed build at runtime without a rebuild (DevTools console):
//     window.AEGIS_BACKEND_URL = 'https://<new-tunnel-host>'
// A localStorage value wins over both constants so the override survives reloads.
function resolveBackendUrl() {
  try {
    const stored = globalThis.localStorage?.getItem('aegis.backendUrl');
    if (stored) return stored.trim();
  } catch {
    /* private mode / disabled storage - fall through to constants */
  }
  return (BUILD_BACKEND_URL || PRODUCTION_BACKEND_URL || '').trim().replace(/\/+$/, '');
}

function isLocalhost() {
  if (!globalThis.location) return true;
  const host = globalThis.location.hostname;
  return host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
}

function localBackendOrigin() {
  if (!globalThis.location) return 'http://localhost:8000';
  // When FastAPI serves the frontend itself, keep same-origin. During local
  // Vite/static development, use the explicit backend port instead.
  if (globalThis.location.port === '8000') return globalThis.location.origin;
  return `${globalThis.location.protocol}//${globalThis.location.hostname}:8000`;
}

export function resolveActiveBackendUrl() {
  return isLocalhost() ? localBackendOrigin() : resolveBackendUrl();
}

export const BACKEND_URL = resolveActiveBackendUrl();

export const API_ORIGIN = BACKEND_URL;

// Public base API origin used by REST callers. Local development keeps the
// explicit :8000 backend while Vercel resolves to the active DevTunnels host.
export function getBaseApiUrl() {
  return API_ORIGIN;
}

export function apiUrl(path = '/') {
  const value = String(path || '/');
  if (/^https?:\/\//i.test(value)) return value;
  // With no backend configured, fall back to same-origin so a co-located
  // deployment (FastAPI serving the SPA, or a same-origin proxy) still works
  // instead of producing "undefined/api/state".
  const base = API_ORIGIN || (globalThis.location ? globalThis.location.origin : '');
  return `${base}/${value.replace(/^\/+/, '')}`;
}

export function websocketUrl(path = '/ws') {
  const url = new URL(apiUrl(path), globalThis.location?.origin || 'http://localhost');
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}
