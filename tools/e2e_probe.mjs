// tools/e2e_probe.mjs — headless-browser E2E probe for the AegisFleet dashboard.
// Drives a real Chrome/Edge over the DevTools Protocol (no npm deps: Node's global
// fetch + WebSocket) so we can assert on live DOM state instead of guessing.
//
// Usage:  node tools/e2e_probe.mjs [browserPath] [appUrl]

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BROWSER =
  process.argv[2] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const APP_URL = process.argv[3] || 'http://localhost:8000';
const PORT = 9333;

const userDataDir = mkdtempSync(join(tmpdir(), 'aegis-cdp-'));
const child = spawn(
  BROWSER,
  [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    '--remote-allow-origins=*',
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-gpu',
    '--disable-extensions',
    '--window-size=1600,900',
    'about:blank',
  ],
  { stdio: 'ignore' }
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function findTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* browser not up yet */
    }
    await sleep(500);
  }
  throw new Error('CDP target never appeared');
}

const target = await findTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();
const events = [];
const consoleErrors = [];

ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
    return;
  }
  events.push(msg);
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params.exceptionDetails;
    consoleErrors.push('EXCEPTION: ' + (d.exception?.description || d.text));
  }
  if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) {
    consoleErrors.push(
      msg.params.type.toUpperCase() +
        ': ' +
        msg.params.args.map((a) => a.description || a.value).join(' ')
    );
  }
  if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
    consoleErrors.push('LOG: ' + msg.params.entry.text);
  }
});

const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

const waitFor = async (method, timeout = 15000) => {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const hit = events.find((e) => e.method === method);
    if (hit) return hit;
    await sleep(100);
  }
  return null;
};

await new Promise((r) => (ws.onopen = r));
await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');
await send('Page.addScriptToEvaluateOnNewDocument', {
  source: `
    window.__errs = [];
    window.addEventListener('error', (e) => window.__errs.push('ERROR: ' + (e.message || e.type)));
    window.addEventListener('unhandledrejection', (e) =>
      window.__errs.push('REJECTION: ' + String((e.reason && e.reason.message) || e.reason))
    );`,
});

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (res.exceptionDetails) {
    const d = res.exceptionDetails;
    return { __evalError: d.exception?.description || d.text };
  }
  return res.result.value;
}

// ---------------------------------------------------------------------------
// Probe sequence
// ---------------------------------------------------------------------------
const report = { steps: [] };
const step = (name, data) => {
  report.steps.push({ name, data });
  console.log('\n### ' + name);
  console.log(JSON.stringify(data, null, 1));
};

await send('Page.navigate', { url: APP_URL });
await waitFor('Page.loadEventFired', 20000);
await sleep(4000); // let WS snapshot + first ticks land

step(
  '1. boot state',
  await evaluate(`(() => {
    const q = (s) => document.querySelector(s);
    const map = q('#map');
    if (window.__storeHost) window.__storeHost.__probeTag = 'tag-1';
    return {
      connLabel: q('#conn-label')?.textContent,
      connDot: q('#conn-dot')?.className,
      statStripChildren: q('#stat-strip')?.children.length,
      statFleet: q('#stat-fleet')?.textContent,
      statSim: q('#stat-sim')?.textContent,
      statRtt: q('#stat-rtt')?.textContent,
      mapMounted: !!map?.querySelector('.leaflet-pane'),
      mapPanes: map?.querySelectorAll('.leaflet-pane').length ?? null,
      mapSize: map ? map.offsetWidth + 'x' + map.offsetHeight : null,
      leafletMarkerIcons: document.querySelectorAll('.leaflet-marker-icon').length,
      shipMarkers: document.querySelectorAll('.ship-marker').length,
      tiles: document.querySelectorAll('.leaflet-tile').length,
      hasLeafletGlobal: typeof L !== 'undefined',
      firstRowDataId: q('.ship-row')?.getAttribute('data-id'),
      firstRowText: q('.ship-row')?.textContent?.trim().slice(0, 40),
      runtimeErrors: window.__errs || [],
    };
  })()`)
);

step(
  '2. click first fleet row (deep)',
  await evaluate(`(() => {
    const row = document.querySelector('.ship-row');
    if (!row) return { clicked: false };
    const id = row.getAttribute('data-id');
    const store = window.__storeHost;
    const before = {
      id,
      storeHasKey: store ? store.ships.has(id) : null,
      storeSize: store ? store.ships.size : null,
      keys: store ? Array.from(store.ships.keys()).slice(0, 4) : null,
    };
    row.click();
    const q = (s) => document.querySelector(s);
    return {
      before,
      after: {
        selectedId: store?.selectedId,
        tagSurvived: store?.__probeTag || null,
        selectedRows: document.querySelectorAll('.ship-row.selected').length,
        detailHTML: q('#ship-detail')?.innerHTML?.slice(0, 90),
        toolsHTML: q('#ship-tools')?.innerHTML?.slice(0, 90),
        hasDirectiveBtn: !!q('#btn-tool-directive'),
      },
    };
  })()`)
);

step(
  '3. open Issue Directive modal',
  await evaluate(`(async () => {
    const q = (s) => document.querySelector(s);
    q('#btn-tool-directive')?.click();
    await new Promise((r) => setTimeout(r, 400));
    const sel = q('#directive-ship-select');
    const port = q('#directive-port');
    return {
      modalHidden: q('#modal-directive')?.hidden,
      shipOptionCount: sel?.options.length ?? null,
      shipOptions: sel ? Array.from(sel.options).map((o) => o.value + '|' + o.textContent) : null,
      shipSelected: sel?.value || null,
      portOptionCount: port?.options.length ?? null,
      portOptions: port ? Array.from(port.options).map((o) => o.value) : null,
    };
  })()`)
);

step(
  '4. store internals',
  await evaluate(`({
    errs: window.__errs || [],
    hasStore: !!window.__storeHost,
    storeShips: window.__storeHost ? window.__storeHost.ships.size : null,
    storePorts: window.__storeHost ? window.__storeHost.ports.length : null,
    selectedId: window.__storeHost ? window.__storeHost.selectedId : null,
  })`)
);

// --- helpers for interaction-driven steps -----------------------------------
const apiGet = async (path) => {
  const res = await fetch('http://localhost:8000' + path, {
    headers: { 'X-Auth-Token': 'command-alpha' },
  });
  return res.json();
};

const clickAt = async (x, y) => {
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
  await send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1,
  });
  await send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1,
  });
};

// 5. stability: the store must never lose ships/ports/zones between renders.
const samples = [];
for (let i = 0; i < 4; i++) {
  samples.push(
    await evaluate(`(() => {
      const q = (s) => document.querySelector(s);
      const st = window.__storeHost || {};
      return {
        ships: st.ships ? st.ships.size : -1,
        ports: st.ports ? st.ports.length : -1,
        zones: st.zones ? st.zones.length : -1,
        rows: document.querySelectorAll('.ship-row').length,
        markers: document.querySelectorAll('.ship-marker').length,
        statFleet: q('#stat-fleet')?.textContent,
        statEnroute: q('#stat-enroute')?.textContent,
        statSim: q('#stat-sim')?.textContent,
        statNlp: q('#stat-nlp')?.textContent,
        statFan: q('#stat-fan')?.textContent,
      };
    })()`)
  );
  await sleep(2500);
}
step('5. stability samples (every 2.5s)', samples);

// 6. issue a real reroute directive from the modal.
const directiveBefore = (await apiGet('/api/state')).directives.length;
const directiveResult = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  q('#btn-tool-directive')?.click();
  await new Promise((r) => setTimeout(r, 500));
  const sel = q('#directive-ship-select');
  const port = q('#directive-port');
  const kind = q('#directive-kind');
  if (kind) kind.value = 'reroute';
  if (port) port.value = 'MCT-1';
  const picked = {
    ship: sel?.value,
    shipOptions: sel?.options.length,
    shipLabel: sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].textContent : null,
    port: port?.value,
    portOptions: port?.options.length,
  };
  q('#directive-send')?.click();
  await new Promise((r) => setTimeout(r, 2000));
  const st = window.__storeHost || {};
  return {
    picked,
    modalHidden: q('#modal-directive')?.hidden,
    toasts: Array.from(document.querySelectorAll('#toasts .toast')).map((t) => t.textContent.slice(0, 70)),
    storeDirectives: st.directives ? st.directives.length : -1,
  };
})()`);
const directivesNow = (await apiGet('/api/state')).directives;
const mine = directivesNow.filter((d) => d.kind === 'reroute');
step('6. directive send', {
  ...directiveResult,
  directivesBefore: directiveBefore,
  directivesAfter: directivesNow.length,
  serverReroute: mine.length ? { kind: mine[mine.length - 1].kind, shipId: mine[mine.length - 1].shipId || mine[mine.length - 1].ship_id, status: mine[mine.length - 1].status } : null,
});

// 7. draw a zone with the rectangle tool using real mouse input, then create it.
const zonesBefore = (await apiGet('/api/state')).zones.length;
// Ship markers use wide divIcons that swallow clicks (stopPropagation), so pick
// two genuinely empty spots on the water instead of blindly using fixed ratios.
const setup = await evaluate(`(() => {
  const mapEl = document.querySelector('#map');
  const r = mapEl.getBoundingClientRect();
  const icons = Array.from(document.querySelectorAll('.leaflet-marker-icon')).map((n) => n.getBoundingClientRect());
  const free = (x, y) => !icons.some((b) => x >= b.x - 26 && x <= b.x + b.width + 26 && y >= b.y - 26 && y <= b.y + b.height + 26);
  const spots = [];
  for (let gy = 0.18; gy <= 0.88 && spots.length < 2; gy += 0.06) {
    for (let gx = 0.18; gx <= 0.88 && spots.length < 2; gx += 0.06) {
      const x = Math.round(r.x + r.width * gx);
      const y = Math.round(r.y + r.height * gy);
      if (!free(x, y)) continue;
      const prev = spots[0];
      if (prev && (Math.abs(prev.x - x) < 70 || Math.abs(prev.y - y) < 45)) continue;
      spots.push({ x, y });
    }
  }
  window.__mapClicks = [];
  mapEl.addEventListener('click', (e) => window.__mapClicks.push(e.clientX + ',' + e.clientY), true);
  return { rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }, iconCount: icons.length, spots };
})()`);
const rectInfo = setup.rect;
const s1 = (setup.spots && setup.spots[0]) || { x: rectInfo.x + Math.round(rectInfo.w * 0.8), y: rectInfo.y + Math.round(rectInfo.h * 0.8) };
const s2 = (setup.spots && setup.spots[1]) || { x: rectInfo.x + Math.round(rectInfo.w * 0.6), y: rectInfo.y + Math.round(rectInfo.h * 0.6) };
await evaluate(`document.querySelector('#toolbar-zone-rect')?.click()`);
await sleep(300);
await clickAt(s1.x, s1.y);
await sleep(300);
await clickAt(s2.x, s2.y);
await sleep(300);
const drawPreview = await evaluate(`(() => {
  const q = (s) => document.querySelector(s);
  return {
    domClicks: window.__mapClicks ? window.__mapClicks.length : -1,
    overlayPaths: document.querySelectorAll('.leaflet-overlay-pane path').length,
    hint: q('#map-hint')?.textContent,
  };
})()`);
await evaluate(`document.querySelector('#toolbar-zone-done')?.click()`);
await sleep(600);
const zoneModal = await evaluate(`(() => {
  const q = (s) => document.querySelector(s);
  return {
    modalHidden: q('#modal-zone')?.hidden,
    vertices: q('#zone-vertices')?.textContent,
    preview: q('#zone-poly-preview')?.textContent,
    title: q('#modal-zone h3')?.textContent,
  };
})()`);
const zoneCreate = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  if (q('#zone-name')) q('#zone-name').value = 'E2E probe zone';
  q('#zone-save')?.click();
  await new Promise((r) => setTimeout(r, 3000));
  const st = window.__storeHost || {};
  return {
    modalHidden: q('#modal-zone')?.hidden,
    storeZones: st.zones ? st.zones.length : -1,
    toast: Array.from(document.querySelectorAll('#toasts .toast')).map((t) => t.textContent.slice(0, 60)),
  };
})()`);
let zonesAfterCreate = (await apiGet('/api/state')).zones;
step('7. zone draw (rect) + create', {
  zonesBefore, zonesAfterCreate: zonesAfterCreate.length,
  createdName: zonesAfterCreate.map((z) => z.name).slice(-2),
  setup, spots: [s1, s2], drawPreview, zoneModal, zoneCreate,
});

// 8. click the new zone polygon -> manage modal must expose Activate/Delete,
//    then delete it so the demo state is left clean.
const cx = Math.round((s1.x + s2.x) / 2);
const cy = Math.round((s1.y + s2.y) / 2);
await clickAt(cx, cy);
await sleep(600);
const manage = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  const before = q('#modal-zone')?.hidden;
  const btns = Array.from(document.querySelectorAll('#zone-options button')).map((b) => b.textContent);
  const del = Array.from(document.querySelectorAll('#zone-options button')).find((b) => /delete/i.test(b.textContent));
  if (del) del.click();
  await new Promise((r) => setTimeout(r, 2500));
  return { modalWasHiddenBeforeClick: before, title: q('#modal-zone h3')?.textContent, actions: btns, deletedClick: !!del };
})()`);
let zonesFinal = (await apiGet('/api/state')).zones;
if (zonesFinal.some((z) => z.name === 'E2E probe zone')) {
  // UI cleanup did not reach the server (click missed the polygon) - clean via REST.
  for (const z of zonesFinal.filter((z) => z.name === 'E2E probe zone')) {
    await fetch('http://localhost:8000/api/zones/' + encodeURIComponent(z.id), {
      method: 'DELETE', headers: { 'X-Auth-Token': 'command-alpha' },
    });
  }
  zonesFinal = (await apiGet('/api/state')).zones;
}
step('8. zone manage + cleanup', { ...manage, zonesAfterCleanup: zonesFinal.length });

// 9. alert-feed click selects a vessel, distress call parses.
const alertClick = await evaluate(`(async () => {
  const st = window.__storeHost || {};
  const before = st.selectedId;
  const node = document.querySelector('#alert-feed .alert');
  if (!node) return { clicked: false, before };
  node.click();
  await new Promise((r) => setTimeout(r, 400));
  return { clicked: true, before, after: window.__storeHost.selectedId, rowsSelected: document.querySelectorAll('.ship-row.selected').length };
})()`);
const distress = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  q('#btn-tool-distress')?.click();
  await new Promise((r) => setTimeout(r, 500));
  const sel = q('#distress-ship-select');
  const info = { shipOptions: sel?.options.length, ship: sel?.value };
  if (q('#distress-text')) q('#distress-text').value = 'Engine room fire, 2 crew injured, taking water.';
  q('#distress-send')?.click();
  await new Promise((r) => setTimeout(r, 6000));
  return { ...info, resultLen: (q('#distress-result')?.innerHTML || '').length, result: (q('#distress-result')?.textContent || '').slice(0, 180) };
})()`);
step('9. alert select + distress', { alertClick, distress });

// 10. remaining controls: fleet filter, alert severity filter, clear alerts.
const filters = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { rowsAll: document.querySelectorAll('.ship-row').length, alertsAll: document.querySelectorAll('#alert-feed .alert').length };
  const f = q('#fleet-filter');
  if (f) {
    f.value = 'gharial';
    f.dispatchEvent(new Event('input', { bubbles: true }));
    await wait(200);
    out.rowsFiltered = document.querySelectorAll('.ship-row').length;
    out.countLabel = q('#fleet-count')?.textContent;
    f.value = '';
    f.dispatchEvent(new Event('input', { bubbles: true }));
    await wait(200);
    out.rowsRestored = document.querySelectorAll('.ship-row').length;
  }
  const sev = q('#alert-filter');
  if (sev) {
    sev.value = 'CRITICAL';
    sev.dispatchEvent(new Event('change', { bubbles: true }));
    await wait(250);
    out.alertsCritical = document.querySelectorAll('#alert-feed .alert').length;
    sev.value = 'LOW';
    sev.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const clear = q('#btn-clear-alerts');
  if (clear) {
    clear.click();
    await wait(250);
    out.alertsAfterClear = document.querySelectorAll('#alert-feed .alert').length;
    out.storeAlerts = (window.__storeHost?.alerts || []).length;
  }
  const dir = q('#ship-directives');
  out.directivePanel = (dir?.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  return out;
})()`);
step('10. filters + clear alerts', filters);

// 11. timeline scrub must relabel LIVE -> -Nm and render a history frame.
const timeline = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const slider = q('#timeline-slider');
  if (!slider) return { missing: true };
  const historyFrames = window.__storeHost?.history?.length ?? -1;
  slider.value = '30';
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  await wait(400);
  const scrubbed = { label: q('#timeline-label')?.textContent, rows: document.querySelectorAll('.ship-row').length, fleet: q('#stat-fleet')?.textContent };
  slider.value = '60';
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  await wait(500);
  return { historyFrames, scrubbed, live: { label: q('#timeline-label')?.textContent, rows: document.querySelectorAll('.ship-row').length, fleet: q('#stat-fleet')?.textContent } };
})()`);
step('11. timeline scrub', timeline);

// 12. demo strait zone button -> store + map must both show the zone.
const demoZone = await evaluate(`(async () => {
  const q = (s) => document.querySelector(s);
  q('#toolbar-zone-demo')?.click();
  await new Promise((r) => setTimeout(r, 3500));
  const st = window.__storeHost || {};
  return {
    storeZones: (st.zones || []).length,
    names: (st.zones || []).map((z) => z.name),
    mapPaths: document.querySelectorAll('.leaflet-overlay-pane path').length,
    toast: Array.from(document.querySelectorAll('#toasts .toast')).map((t) => t.textContent.slice(0, 60)),
  };
})()`);
const leftover = (await apiGet('/api/state')).zones;
for (const z of leftover) {
  await fetch('http://localhost:8000/api/zones/' + encodeURIComponent(z.id), {
    method: 'DELETE', headers: { 'X-Auth-Token': 'command-alpha' },
  });
}
step('12. demo strait zone (+cleanup)', { ...demoZone, zonesAfterCleanup: (await apiGet('/api/state')).zones.length });

// 13. no literal "undefined"/"NaN" may leak into any rendered panel.
const leaks = await evaluate(`(() => {
  const panels = ['#ship-detail', '#ship-tools', '#alert-feed', '#toasts', '#ship-directives', '#fleet-list', '#distress-result'];
  const bad = {};
  for (const sel of panels) {
    const txt = document.querySelector(sel)?.textContent || '';
    const m = txt.match(/.{0,45}(undefined|NaN).{0,45}/);
    if (m) bad[sel] = m[0];
  }
  return { leaks: bad, bodyHasUndefined: /undefined/.test(document.body.textContent) };
})()`);
step('13. undefined/NaN leak scan', leaks);

step('14. final runtime errors', await evaluate(`({ errs: window.__errs || [],
  ships: window.__storeHost?.ships?.size ?? null,
  ports: window.__storeHost?.ports?.length ?? null,
  zones: window.__storeHost?.zones?.length ?? null })`));

report.consoleErrors = [...new Set(consoleErrors)];
console.log('\n### console errors (' + report.consoleErrors.length + ')');
for (const e of report.consoleErrors) console.log(' - ' + e);

try {
  ws.close();
} catch {
  /* ignore */
}
child.kill();
await sleep(800);
try {
  rmSync(userDataDir, { recursive: true, force: true });
} catch {
  /* ignore */
}
process.exit(0);
