// Browser-side runtime configuration.
//
// Localhost connects directly to the FastAPI server. Public deployments use
// the live LocalTunnel backend unless AEGIS_BACKEND_URL overrides it during a
// Vercel build. Keep this value synchronized with the tunnel used for demos.
const BUILD_BACKEND_URL = "";
const PRODUCTION_BACKEND_URL = "https://six-actors-switch.loca.lt";

function configuredBackendUrl() {
  return String(
    globalThis.AEGIS_CONFIG?.backendUrl || BUILD_BACKEND_URL || '',
  ).trim().replace(/\/+$/, '');
}

export const BACKEND_URL = configuredBackendUrl();

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

export const API_ORIGIN = isLocalhost()
  ? localBackendOrigin()
  : BACKEND_URL || PRODUCTION_BACKEND_URL;

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
