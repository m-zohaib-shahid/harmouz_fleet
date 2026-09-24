// Browser-side runtime configuration.
//
// During a Vercel build, tools/vercel-build.mjs replaces BUILD_BACKEND_URL
// with the AEGIS_BACKEND_URL project environment variable. Local/static runs
// use an empty value and automatically target the local port-8000 backend.
const BUILD_BACKEND_URL = "";

function configuredBackendUrl() {
  return String(
    globalThis.AEGIS_CONFIG?.backendUrl || BUILD_BACKEND_URL || '',
  ).trim().replace(/\/+$/, '');
}

export const BACKEND_URL = configuredBackendUrl();

function localBackendOrigin() {
  if (!globalThis.location) return 'http://localhost:8000';
  const current = globalThis.location.origin;
  const host = globalThis.location.hostname;
  const isLocal = host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
  // A deployed URL assumes an API/WS proxy on the same origin unless the
  // Vercel build supplied AEGIS_BACKEND_URL. Local frontend dev uses port 8000.
  if (!isLocal || globalThis.location.port === '8000') return current;
  return `${globalThis.location.protocol}//${host}:8000`;
}

export const API_ORIGIN = BACKEND_URL || localBackendOrigin();

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
