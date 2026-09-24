// Browser-side runtime configuration.
//
// Localhost connects directly to FastAPI. Public deployments connect to the
// active Microsoft DevTunnels endpoint over HTTPS/WSS.
const BUILD_BACKEND_URL = "";
const PRODUCTION_BACKEND_URL = "https://hpltn945-8000.inc1.devtunnels.ms";

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

export const BACKEND_URL = isLocalhost()
  ? localBackendOrigin()
  : PRODUCTION_BACKEND_URL;

export const API_ORIGIN = BACKEND_URL;

// Public base API origin used by REST callers. Local development keeps the
// explicit :8000 backend while Vercel resolves to the active DevTunnels host.
export function getBaseApiUrl() {
  return API_ORIGIN;
}

export function apiUrl(path = '/') {
  const value = String(path || '/');
  if (/^https?:\/\//i.test(value)) return value;
  return `${API_ORIGIN}/${value.replace(/^\/+/, '')}`;
}

export function websocketUrl(path = '/ws') {
  const url = new URL(apiUrl(path));
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}
