// Small shared helpers: DOM, formatting, toasts, alert sound, colour scales.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== null && value !== undefined && value !== false) {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const fmt = {
  num: (v, d = 0) => (Number.isFinite(v) ? Number(v).toFixed(d) : '—'),
  hours: (h) => {
    if (!Number.isFinite(h)) return '—';
    if (h >= 24) return `${Math.floor(h / 24)}d ${Math.round(h % 24)}h`;
    const hours = Math.floor(h);
    const mins = Math.round((h - hours) * 60);
    return `${hours}h ${String(mins).padStart(2, '0')}m`;
  },
  clock: (seconds) => {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = String(Math.floor(s / 3600)).padStart(2, '0');
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(s % 60).padStart(2, '0');
    return `${h}:${m}:${sec}`;
  },
  time: (ts) => new Date((ts || 0) * 1000).toLocaleTimeString([], { hour12: false }),
  km: (v) => `${Number(v || 0).toFixed(1)} km`,
};

export const SEVERITY_ORDER = { LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4 };

export const COLORS = {
  normal: '#24d07f',
  rerouting: '#35c2ff',
  distressed: '#ff3b56',
  out_of_fuel: '#ff3b56',
  stranded: '#8b93a7',
  arrived: '#6b8ba4',
  LOW: '#6b8ba4',
  MEDIUM: '#f5b642',
  HIGH: '#ff8a3d',
  CRITICAL: '#ff3b56',
  zone: {
    CRITICAL: '#ff3b56',
    HIGH: '#ff8a3d',
    MEDIUM: '#f5b642',
    LOW: '#6b8ba4',
  },
};

export function statusColor(ship) {
  if (!ship) return COLORS.normal;
  if (ship.status === 'distressed' || ship.flags?.includes('in_restricted_zone')) return COLORS.distressed;
  if (ship.status === 'out_of_fuel') return COLORS.CRITICAL;
  if (ship.status === 'stranded') return COLORS.stranded;
  if (ship.flags?.includes('insufficient_fuel')) return COLORS.HIGH;
  if (ship.flags?.includes('proximity_alert')) return COLORS.MEDIUM;
  if (ship.flags?.includes('adverse_weather')) return '#a06bff';
  return COLORS[ship.status] || COLORS.normal;
}

// ---------------------------------------------------------------------------
// Toasts + sound
// ---------------------------------------------------------------------------
const toastHost = () => document.getElementById('toasts');
const shown = new Set();

export function toast(title, message, severity = 'MEDIUM', ttl = 8000, key = null) {
  if (key) {
    if (shown.has(key)) return;
    shown.add(key);
    if (shown.size > 400) shown.clear();
  }
  const node = el(
    'div',
    { class: `toast ${severity}` },
    el('div', { class: 't', text: `${severity} · ${title}` }),
    el('div', { class: 'm', text: message })
  );
  toastHost()?.append(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .4s';
    setTimeout(() => node.remove(), 400);
  }, ttl);
}

let audioCtx = null;
let soundOn = false;
export function toggleSound(force) {
  soundOn = force === undefined ? !soundOn : Boolean(force);
  return soundOn;
}
export function beep(severity = 'CRITICAL') {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = severity === 'CRITICAL' ? 880 : 620;
    gain.gain.value = 0.05;
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + (severity === 'CRITICAL' ? 0.35 : 0.18));
  } catch {
    /* audio blocked until user gesture — ignore */
  }
}

// ---------------------------------------------------------------------------
// Interpolation helpers (ship motion must never teleport)
// ---------------------------------------------------------------------------
export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const lerp = (a, b, t) => a + (b - a) * t;

export function lerpCoord(from, to, t) {
  return [lerp(from[0], to[0], t), lerp(from[1], to[1], t)];
}

export function lerpAngle(from, to, t) {
  let delta = ((to - from + 540) % 360) - 180;
  return (from + delta * t + 360) % 360;
}

export function haversineKm(a, b) {
  const R = 6371.0088;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLng = toRad(b[1] - a[1]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

export const rAF = typeof requestAnimationFrame !== 'undefined' ? requestAnimationFrame : (f) => setTimeout(f, 16);
