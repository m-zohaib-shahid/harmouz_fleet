// Main frontend entrypoint: session boot, dashboard assembly, WS feed, RAF updates.

import { $, $$, el, fmt, rAF, COLORS, statusColor, toast, beep, toggleSound } from './util.js';
import { api } from './api.js';
import { Feed } from './net.js';
import { FeedStore } from './store.js';
import {
  mountStore,
  renderConnState,
  renderRtt,
  renderStatStrip,
  renderSystemMetrics,
  renderFleetList,
  renderShipDetail,
  renderAlertFeed,
  renderPortList,
  onFleetSelect,
  setMap,
} from './store.js';
import { mountMap, addWater, renderPorts, renderZones, renderShips } from './map.js';
import { startZoneDrawing, stopZoneDrawing } from './zone_draw.js';

const TOKEN_KEY = 'aegis_auth_token';

let store = null;
let map = null;
let water = null;
let zonesLayer = null;
let shipsLayer = null;
let portsLayer = null;
let markers = null;
let feed = null;
let zoneDrawController = null;
let bearerToken = null;

// ---------------------------------------------------------------------------
// Login form & session handling
// ---------------------------------------------------------------------------
export function bindSessionControls() {
  const submitBtn = $('#login-submit');
  const tokenInput = $('#login-token');
  const quickHost = $('#quick-tokens');
  
  const handleConnect = (tokenOverride) => {
    const token = tokenOverride || tokenInput?.value.trim() || 'command-alpha';
    if (tokenInput) tokenInput.value = token;
    doLogin(token);
  };

  submitBtn?.addEventListener('click', () => handleConnect());
  tokenInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleConnect();
  });

  if (quickHost) {
    const tokens = ['command-alpha', 'captain-mv-1', 'observer'];
    quickHost.innerHTML = '';
    for (const tok of tokens) {
      const btn = el('button', { class: 'btn ghost sm', text: tok });
      btn.addEventListener('click', () => handleConnect(tok));
      quickHost.appendChild(btn);
    }
  }
}

async function doLogin(token) {
  try {
    bearerToken = token;
    api.setToken(token);
    sessionStorage.setItem(TOKEN_KEY, token);
    const identityEl = $('#identity');
    if (identityEl) {
      const isCmd = token.includes('command') || token.includes('alpha') || token === 'admin';
      const isCapt = token.includes('captain');
      const roleName = isCmd ? 'Fleet Command HQ' : isCapt ? 'Vessel Master' : 'Observer';
      identityEl.innerHTML = `Connected: <b>${token}</b> (${roleName})`;
    }
    toast('Session connected', `Logged in with token: ${token}`, 'LOW');
    connectFeed();
    refreshFromServer();
  } catch (err) {
    toast('Login rejected', String(err?.message ?? err), 'HIGH');
  }
}

// ---------------------------------------------------------------------------
// Dashboard layout + wiring
// ---------------------------------------------------------------------------
function renderDashboard() {
  if (store) return;
  store = new FeedStore();

  // top stat strip
  const strip = el('div', { class: 'stat-strip' }, [
    el('div', { class: 'stat' }, el('label', { text: 'Fleet' }), el('b', { id: 'stat-fleet', text: '0' })),
    el('div', { class: 'stat' }, el('label', { text: 'En route' }), el('b', { id: 'stat-enroute', text: '0' })),
    el('div', { class: 'stat' }, el('label', { text: 'Arrived' }), el('b', { id: 'stat-arrived', text: '0' })),
    el('div', { class: 'stat' }, el('label', { text: 'Stranded' }), el('b', { id: 'stat-strays', text: '0' })),
    el('div', { class: 'stat' }, el('label', { text: 'Low fuel' }), el('b', { id: 'stat-fuel', text: '0' })),
    el('div', { class: 'stat' }, el('label', { text: 'Weather' }), el('b', { id: 'stat-weather-mini', text: 'cache' })),
    el('div', { class: 'stat' }, el('label', { text: 'Sim' }), el('b', { id: 'stat-sim', text: '—' })),
    el('div', { class: 'stat' }, el('label', { text: 'Fan-out' }), el('b', { id: 'stat-fan', text: '—' })),
    el('div', { class: 'stat' }, el('label', { text: 'NLP' }), el('b', { id: 'stat-nlp', text: '—' })),
    el('div', { class: 'stat' }, el('label', { text: 'RTT' }), el('b', { id: 'stat-rtt', text: '—' })),
  ]);
  const host = $('#stat-strip');
  if (host) {
    host.innerHTML = '';
    host.appendChild(strip);
  }
  // Mount AFTER the strip is in the DOM: mountStore caches #stat-fleet etc. by id,
  // and those nodes do not exist while `strip` is still detached. Mounting early
  // left every stat cache null, so the whole top strip stayed frozen at 0.
  mountStore(store);

  renderConnState('offline');
  renderRtt(null);

  $('#toolbar-zone-poly')?.addEventListener('click', () => startZoneDraw('poly'));
  $('#toolbar-zone-rect')?.addEventListener('click', () => startZoneDraw('rect'));
  $('#toolbar-zone-clear')?.addEventListener('click', cancelZoneDraw);
  $('#toolbar-zone-demo')?.addEventListener('click', demoStraitZone);
  $('#toolbar-sound')?.addEventListener('click', () => toggleSound());

  $('#toolbar-zone-done')?.addEventListener('click', () => {
    if (zoneDrawController) zoneDrawController.finalize();
  });

  $('#fleet-filter')?.addEventListener('input', () => {
    if (store) renderFleetList(store);
  });

  $('#alert-filter')?.addEventListener('change', () => {
    if (store) renderAlertFeed(store);
  });

  $('#btn-clear-alerts')?.addEventListener('click', () => {
    if (store) {
      store.alerts = [];
      renderAlertFeed(store);
      toast('Alerts cleared', 'Alert feed reset', 'LOW');
    }
  });

  $('#timeline-slider')?.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    const label = $('#timeline-label');
    if (!store) return;
    if (val >= 60) {
      store.isHistoryMode = false;
      if (label) { label.textContent = 'LIVE'; label.style.color = 'var(--ok)'; }
      renderAll();
    } else {
      store.isHistoryMode = true;
      const minsAgo = 60 - val;
      if (label) { label.textContent = `-${minsAgo}m`; label.style.color = 'var(--high)'; }
      if (store.history.length > 0) {
        const idx = Math.max(0, Math.floor((val / 60) * (store.history.length - 1)));
        const frame = store.history[idx];
        if (frame && frame.ships) {
          const tempStore = { ...store, ships: frame.ships, fleet: frame.fleet, rerouting: frame.rerouting, arrived: frame.arrived, strayCount: frame.strayCount };
          renderStatStrip(tempStore);
          renderFleetList(tempStore);
        }
      }
    }
  });

  mountMapAndLayers();

  bindZoneModal();
  bindDistressModal();
  bindDirectiveModal();
  bindSessionControls();

  refreshFromServer();
  startRafLoop();
}

function mountMapAndLayers() {
  const mapEl = $('#map');
  if (!mapEl || map) return;
  const res = mountMap(mapEl);
  if (!res) return;
  map = res.map;
  zonesLayer = res.zonesLayer;
  shipsLayer = res.shipsLayer;
  portsLayer = res.portsLayer;
  if (store?.ports?.length) renderPorts(portsLayer, store.ports);
}

function startZoneDraw(mode) {
  stopZoneDrawing();
  zoneDrawController = startZoneDrawing(map, zonesLayer || map, (vertices) => {
    openZoneModal(vertices);
  });
  if (zoneDrawController) {
    zoneDrawController.setMode(mode);
    zoneDrawController.activeTool = mode;
  }
  $('#toolbar-zone-poly')?.classList.toggle('active', mode === 'poly');
  $('#toolbar-zone-rect')?.classList.toggle('active', mode === 'rect');
  canvasHint(mode === 'poly' ? 'Drawing polygon. Click map points. Click Done to finish.' : 'Drawing rectangle. Click two opposite corners. Click Done to finish.');
}

function cancelZoneDraw() {
  stopZoneDrawing();
  $('#toolbar-zone-poly')?.classList.remove('active');
  $('#toolbar-zone-rect')?.classList.remove('active');
  canvasHint('Zone drawing cancelled.');
}

function demoStraitZone() {
  api.demoStraitZone()
    .then((zone) => {
      const sev = zone?.severity || 'CRITICAL';
      const affected = Array.isArray(zone?.affected_ships) ? zone.affected_ships.length : null;
      toast(
        'Strait zone loaded',
        `"${zone?.name || 'Strait of Hormuz'}" active (${sev}${affected != null ? `, ${affected} ships affected` : ''})`,
        'LOW'
      );
      refreshFromServer();
    })
    .catch((err) => {
      toast('Demo zone failed', String(err?.message ?? err), 'HIGH');
    });
}

function zoneOptions(poly, zone) {
  const host = $('#zone-options');
  if (!host) return;
  // #zone-options lives inside #modal-zone, so that modal has to be shown for the
  // Activate/Delete actions to be reachable at all (they used to be invisible).
  const manageModal = $('#modal-zone');
  const titleEl = $('#modal-zone h3');
  const saveBtn = $('#zone-save');
  const cancelBtn = $('#zone-cancel');
  const coords = zone.coords || zone.polygon || [];
  if (titleEl) titleEl.textContent = `Restricted zone: ${zone.name}`;
  if (saveBtn) saveBtn.hidden = true;
  if (cancelBtn) cancelBtn.textContent = 'Close';
  const nameInput = $('#zone-name');
  if (nameInput) nameInput.value = zone.name || '';
  const sevSelect = $('#zone-severity');
  if (sevSelect && zone.severity) sevSelect.value = zone.severity;
  const vertEl = $('#zone-vertices');
  if (vertEl) vertEl.textContent = `${coords.length} vertices`;
  const preview = $('#zone-poly-preview');
  if (preview) {
    preview.textContent = coords.map((v) => `[${Number(v[0]).toFixed(3)}, ${Number(v[1]).toFixed(3)}]`).join(' → ');
  }
  host.innerHTML = '';
  host.appendChild(el('div', { class: 'row hint', text: `Zone: ${zone.name}` }));
  host.appendChild(el('div', { class: 'row hint', text: `Severity: ${zone.severity}` }));
  host.appendChild(el('div', { class: 'row hint', text: `Active: ${zone.active ? 'yes' : 'no'}` }));
  const wrap = el('div', { class: 'row end' });
  const deact = el('button', { class: 'btn ghost sm', text: zone.active ? 'Deactivate' : 'Activate' });
  deact.addEventListener('click', async () => {
    try {
      await api.patchZone(zone.id, !zone.active);
      toast('Zone toggled', `${zone.name} is now ${!zone.active ? 'active' : 'inactive'}`, 'LOW');
      refreshFromServer();
    } catch (err) {
      toast('Zone toggle failed', String(err?.message ?? err), 'HIGH');
    }
  });
  const del = el('button', { class: 'btn danger sm', text: 'Delete zone' });
  del.addEventListener('click', async () => {
    try {
      await api.deleteZone(zone.id);
      toast('Zone deleted', `"${zone.name}" removed`, 'LOW');
      refreshFromServer();
    } catch (err) {
      toast('Zone deletion failed', String(err?.message ?? err), 'HIGH');
    }
  });
  wrap.appendChild(deact);
  wrap.appendChild(del);
  host.appendChild(wrap);
  closeAllModals();
  if (manageModal) manageModal.hidden = false;
}

function canvasHint(msg) {
  const host = $('#map-hint');
  if (host) host.textContent = msg;
}

function closeAllModals() {
  ['#modal-zone', '#modal-distress', '#modal-directive'].forEach((sel) => {
    const el_ = $(sel);
    if (el_) el_.hidden = true;
  });
}

function bindZoneModal() {
  const modal = $('#modal-zone');
  const nameInput = $('#zone-name');
  const sevSelect = $('#zone-severity');
  const saveBtn = $('#zone-save');
  const cancelBtn = $('#zone-cancel');

  let currentVertices = null;

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.hidden = true;
      currentVertices = null;
      cancelZoneDraw();
    }
  });

  cancelBtn?.addEventListener('click', () => {
    if (modal) modal.hidden = true;
    currentVertices = null;
    cancelZoneDraw();
  });

  saveBtn?.addEventListener('click', async () => {
    const name = nameInput?.value.trim() || 'Restricted Zone';
    const severity = sevSelect?.value || 'CRITICAL';
    if (!currentVertices || currentVertices.length < 3) {
      toast('Invalid zone', 'Need at least 3 points', 'HIGH');
      return;
    }
    try {
      await api.createZone({ name, polygon: currentVertices, severity });
      toast('Zone created', `Zone "${name}" active`, 'LOW');
      if (modal) modal.hidden = true;
      currentVertices = null;
      stopZoneDrawing();
      refreshFromServer();
    } catch (err) {
      toast('Zone creation failed', String(err?.message ?? err), 'HIGH');
    }
  });

  window.__openZoneModal = (vertices) => {
    closeAllModals();
    currentVertices = vertices;
    // Reset the shared modal back to "create" mode (zoneOptions() reuses it).
    const titleEl = $('#modal-zone h3');
    if (titleEl) titleEl.textContent = 'New restricted zone';
    if (saveBtn) saveBtn.hidden = false;
    if (cancelBtn) cancelBtn.textContent = 'Cancel';
    const optsEl = $('#zone-options');
    if (optsEl) optsEl.innerHTML = '';
    const vertEl = $('#zone-vertices');
    const prevEl = $('#zone-poly-preview');
    if (vertEl) vertEl.textContent = `${vertices?.length || 0} vertices defined`;
    if (prevEl) prevEl.textContent = (vertices || []).map((v) => `[${v[0].toFixed(3)}, ${v[1].toFixed(3)}]`).join(' → ');
    if (modal) modal.hidden = false;
  };
}

function openZoneModal(vertices) {
  window.__openZoneModal?.(vertices);
}

function bindDistressModal() {
  const modal = $('#modal-distress');
  const shipSelect = $('#distress-ship-select');
  const textArea = $('#distress-text');
  const sendBtn = $('#distress-send');
  const cancelBtn = $('#distress-cancel');
  const resultEl = $('#distress-result');
  const samplesHost = $('#distress-samples');

  let pendingShipId = null;

  const sampleTexts = [
    'Engine room fire, 2 crew injured, main propulsion lost.',
    'Heavy sea flooding in cargo hold #2. Pumps failing.',
    'Collision risk with unknown vessel, steering gear jammed.',
    'Out of fuel in active storm cell, drift rate 4 knots.',
  ];

  if (samplesHost) {
    samplesHost.innerHTML = '';
    for (const st of sampleTexts) {
      const chip = el('button', { class: 'btn ghost sm', text: st.slice(0, 30) + '...' });
      chip.addEventListener('click', () => {
        if (textArea) textArea.value = st;
      });
      samplesHost.appendChild(chip);
    }
  }

  shipSelect?.addEventListener('change', () => {
    pendingShipId = shipSelect.value;
  });

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.hidden = true;
      if (resultEl) resultEl.innerHTML = '';
      if (textArea) textArea.value = '';
      pendingShipId = null;
    }
  });

  cancelBtn?.addEventListener('click', () => {
    if (modal) modal.hidden = true;
    if (resultEl) resultEl.innerHTML = '';
    if (textArea) textArea.value = '';
    pendingShipId = null;
  });

  sendBtn?.addEventListener('click', async () => {
    const targetId = shipSelect?.value || pendingShipId;
    const text = textArea?.value.trim();
    if (!targetId) {
      toast('Distress aborted', 'No ship selected', 'HIGH');
      return;
    }
    if (!text) {
      toast('Distress aborted', 'Add a description', 'MEDIUM');
      return;
    }
    sendBtn.disabled = true;
    sendBtn.textContent = 'Transmitting...';
    try {
      const resp = await api.distress(targetId, text);
      if (resultEl) resultEl.innerHTML = aiResultHTML(resp);
      toast('Distress transmitted', `Parsed: ${resp?.severity ?? '—'} — ${resp?.headline || resp?.issue_summary || ''}`, 'MEDIUM');
    } catch (err) {
      toast('Distress send failed', String(err?.message ?? err), 'HIGH');
      if (resultEl) resultEl.innerHTML = el('div', { class: 'hint', text: `Error: ${String(err?.message ?? err)}` });
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = 'Transmit (AI parse)';
    }
  });

  window.__openDistressModal = async (shipId, shipName) => {
    closeAllModals();
    if (modal) modal.hidden = false;
    if (textArea) textArea.value = '';
    if (resultEl) resultEl.innerHTML = '';
    pendingShipId = populateShipSelect(shipSelect, shipId);
    if (!pendingShipId) {
      await ensureFleetLoaded();
      pendingShipId = populateShipSelect(shipSelect, shipId);
      if (!pendingShipId) toast('No vessels', 'Fleet state unavailable — check the connection', 'HIGH');
    }
    textArea?.focus();
  };
}

function aiResultHTML(resp) {
  if (!resp) return '';
  const sev = resp.severity || 'LOW';
  const headline = resp.headline || resp.issue_summary || '—';
  const grid = `
    <div class="ai-grid">
      <div><label>Severity</label><b style="color:${COLORS[sev] || COLORS.normal}">${sev}</b></div>
      <div><label>Headline</label><b>${headline}</b></div>
      <div><label>Source</label><b>${resp.provider?.provider || resp.source || '—'}</b></div>
      <div><label>Suggested Action</label><b>${resp.recommended_action || '—'}</b></div>
    </div>
  `;
  return `
    <div class="ai-card ${sev}">
      <div class="head">
        <span class="ai-sev" style="color:${COLORS[sev] || COLORS.normal}">${sev}</span>
        <span class="time">${fmt.time(resp.ts || Date.now() / 1000)}</span>
      </div>
      ${grid}
      <pre class="json">${JSON.stringify(resp, null, 1)}</pre>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Feed
// ---------------------------------------------------------------------------
function connectFeed() {
  if (feed) {
    try { feed.disconnect(); } catch { /* ignore */ }
  }
  feed = new Feed({
    onSnapshot: (msg) => {
      store?.applySnapshot(msg);
      renderAll();
      if (store?.zones) renderZonesOnMap(store.zones);
      if (store?.ports && portsLayer) renderPorts(portsLayer, store.ports);
      // A snapshot without ships means we are out of sync - pull a fresh one.
      if (!store?.ships?.size) ensureFleetLoaded();
    },
    onTick: (msg) => {
      store?.applyTick(msg);
      renderTick();
    },
    onAlert: (alert, msg) => {
      store?.applyTick(msg);
      renderTick();
      if (alert) handleRealTimeAlert(alert);
    },
    onEvent: (msg) => {
      if (!['zone', 'directive', 'tick', 'alert'].includes(msg.type)) return;
      // Events used to be ignored entirely (only renderAll ran), so a zone event
      // never reached the store and the map kept stale polygons.
      store?.applySnapshot(msg);
      renderAll();
      if (msg.type === 'zone' && store) renderZonesOnMap(store.zones);
    },
    onStatus: (state, rtt) => {
      store?.setConnState?.(state);
      renderConnState(state);
      if (state === 'open') {
        renderRtt(rtt ?? null);
        fetchMetricsLoop();
      } else {
        renderRtt(null);
      }
    },
  });

  feed.connect(bearerToken || 'command-alpha');
}

// ---------------------------------------------------------------------------
// Ship markers (incremental)
// ---------------------------------------------------------------------------
function updateShipMarkers() {
  if (!map || !shipsLayer) return;
  const ships = Array.from(store?.ships?.values() ?? []);
  markers = renderShips(shipsLayer, ships, {
    selected: (id) => id === store?.selectedId,
    onSelect: (ship) => onFleetSelect(ship.id || ship.shipId),
    onPopup: (ship) => shipPopupContent(ship),
    pending: true,
  });
  if (markers) setMap(map, markers);
}

function shipPopupContent(ship) {
  const status = ship.status || 'normal';
  const color = COLORS[status] || COLORS.normal;
  const name = ship.name || ship.id || ship.shipId;
  const fuelVal = ship.fuel != null ? fmt.num(ship.fuel, 0) : '—';
  const fuelCap = ship.fuel_capacity != null ? fmt.num(ship.fuel_capacity, 0) : '';
  const fuelStr = fuelCap ? `${fuelVal} / ${fuelCap}` : fuelVal;
  return `
  <div style="display:grid;grid-template-columns:auto 1fr;gap:2px 8px;font-size:11px">
    <b style="color:${color};grid-column:1/-1">${name}</b>
    <span>Status</span><b style="color:${color}">${status}</b>
    <span>Role</span><b>${ship.role || 'cargo'}</b>
    <span>Flag</span><b>${ship.flag || 'Panama'}</b>
    <span>Cargo</span><b>${ship.payload || ship.cargo || '—'}</b>
    <span>Speed</span><b>${ship.speed ? fmt.num(ship.speed, 1) + ' kt' : '—'}</b>
    <span>Heading</span><b>${fmt.num(ship.heading, 0)}°</b>
    <span>Fuel</span><b>${fuelStr}</b>
    <span>Destination</span><b>${ship.destination_label || ship.destination || '—'}</b>
    <span>ETA</span><b>${ship.eta_hours != null ? fmt.hours(ship.eta_hours) : '—'}</b>
  </div>`;
}

// ---------------------------------------------------------------------------
// RAF loop (interpolation-only; state from WS ticks)
// ---------------------------------------------------------------------------
function startRafLoop() {
  let running = true;
  const frame = () => {
    if (!running) return;
    updateShipMarkers();
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return () => { running = false; };
}

// Populate a <select> with the live fleet. The option list is only rebuilt when the
// fleet composition actually changes, so an open dropdown never flickers/reset, and
// the vessel the operator is looking at always stays pre-selected.
function populateShipSelect(selectEl, selectedId) {
  if (!selectEl) return null;
  const ships = Array.from(store?.ships?.values() ?? []).sort((a, b) =>
    String(a.name || a.id || a.shipId).localeCompare(b.name || b.id || b.shipId)
  );
  if (!ships.length) {
    if (selectEl.dataset.sig !== 'empty') {
      selectEl.innerHTML = '<option value="">— no ships loaded —</option>';
      selectEl.dataset.sig = 'empty';
    }
    return null;
  }
  const ids = ships.map((s) => s.id || s.shipId);
  const sig = ids.join('|');
  const wanted =
    [selectedId, selectEl.value, store?.selectedId, ids[0]].find((v) => v && ids.includes(v)) || ids[0];
  if (selectEl.dataset.sig === sig && selectEl.options.length === ships.length) {
    selectEl.value = wanted;
    return wanted;
  }
  selectEl.innerHTML = '';
  for (const s of ships) {
    const sid = s.id || s.shipId;
    const opt = document.createElement('option');
    opt.value = sid;
    opt.textContent = s.name ? `${s.name} (${sid})` : sid;
    if (sid === wanted) opt.selected = true;
    selectEl.appendChild(opt);
  }
  selectEl.dataset.sig = sig;
  selectEl.value = wanted;
  return wanted;
}

function waitUntil(predicate, timeoutMs) {
  return new Promise((resolve) => {
    const started = performance.now();
    const poll = () => {
      if (predicate()) return resolve(true);
      if (performance.now() - started >= timeoutMs) return resolve(false);
      setTimeout(poll, 120);
    };
    poll();
  });
}

// Self-healing fleet load: ask the socket for a fresh snapshot first (backend
// handles {type:"fleet"}), then fall back to REST. Without this, any transiently
// empty store left the modals stuck on "no ships loaded" until the next tick.
let fleetLoad = null;
function ensureFleetLoaded(timeoutMs = 1200) {
  if (store?.ships?.size) return Promise.resolve(true);
  if (!fleetLoad) {
    fleetLoad = (async () => {
      if (feed?.send({ type: 'fleet' })) {
        if (await waitUntil(() => (store?.ships?.size || 0) > 0, timeoutMs)) return true;
      }
      try {
        store?.applySnapshot(await api.state());
      } catch {
        /* keep whatever we already have */
      }
      return (store?.ships?.size || 0) > 0;
    })().finally(() => {
      fleetLoad = null;
    });
  }
  return fleetLoad;
}

// Keep any open modal dropdown in step with the 1 Hz fleet broadcast.
function syncOpenShipSelects() {
  if (!store) return;
  if ($('#modal-directive')?.hidden === false) populateShipSelect($('#directive-ship-select'));
  if ($('#modal-distress')?.hidden === false) populateShipSelect($('#distress-ship-select'));
}

function bindDirectiveModal() {
  const modal = $('#modal-directive');
  const shipSelect = $('#directive-ship-select');
  const portSelect = $('#directive-port');
  const kindSelect = $('#directive-kind');
  const noteInput = $('#directive-note');
  const sendBtn = $('#directive-send');
  const cancelBtn = $('#directive-cancel');

  let pendingShipId = null;

  shipSelect?.addEventListener('change', () => {
    pendingShipId = shipSelect.value;
  });

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.hidden = true;
      pendingShipId = null;
    }
  });

  cancelBtn?.addEventListener('click', () => {
    if (modal) modal.hidden = true;
    pendingShipId = null;
  });

  sendBtn?.addEventListener('click', async () => {
    const targetShipId = shipSelect?.value || pendingShipId;
    const kind = kindSelect?.value || 'course';
    const note = noteInput?.value.trim() || '';
    if (!targetShipId) {
      toast('Directive aborted', 'No ship selected', 'HIGH');
      return;
    }
    try {
      const payload = { shipId: targetShipId, kind, note };
      if (kind === 'course' || kind === 'reroute') {
        const portId = portSelect?.value;
        if (portId) payload.destination = portId;
      }
      const directive = await api.createDirective(payload);
      toast('Directive sent', `${directive?.kind ?? kind} → ship ${targetShipId}`, 'LOW');
      if (modal) modal.hidden = true;
      pendingShipId = null;
      refreshFromServer();
    } catch (err) {
      toast('Directive send failed', String(err?.message ?? err), 'HIGH');
    }
  });

  refreshPortsForDirective();

  window.__openDirectiveModal = async (shipId, shipName) => {
    closeAllModals();
    if (modal) modal.hidden = false;
    pendingShipId = populateShipSelect(shipSelect, shipId);
    if (!pendingShipId) {
      await ensureFleetLoaded();
      pendingShipId = populateShipSelect(shipSelect, shipId);
    }
    await refreshPortsForDirective();
  };

  async function refreshPortsForDirective() {
    if (!portSelect) return;
    try {
      let rawPorts = store?.ports;
      if (!rawPorts || !rawPorts.length) {
        const state = await api.state();
        rawPorts = state?.ports || [];
      }
      const ports = (rawPorts || []).map((p) => ({ value: p.id || p.name, text: p.name || p.id }));
      const current = portSelect.value;
      portSelect.innerHTML = '';
      if (!ports.length) {
        portSelect.innerHTML = '<option value="">— no ports available —</option>';
        return;
      }
      for (const p of ports) {
        const opt = document.createElement('option');
        opt.value = p.value;
        opt.textContent = p.text;
        portSelect.appendChild(opt);
      }
      if (ports.length && (!current || !ports.find((p) => p.value === current))) {
        portSelect.selectedIndex = 0;
      }
    } catch {
      portSelect.innerHTML = '<option value="">— no ports available —</option>';
    }
  }
}

function handleRealTimeAlert(alert) {
  const sev = alert.severity || alert.sev || 'LOW';
  const shipId = alert.shipId || (alert.shipIds ? alert.shipIds[0] : null);
  const headline = alert.headline || alert.message || 'Alert';
  if (sev === 'CRITICAL') {
    toast('Critical alert', headline, sev, 14000, 'crit-' + shipId + '-' + alert.ts);
    beep('CRITICAL');
  } else if (sev === 'HIGH') {
    toast('High alert', headline, sev, 9000, 'high-' + shipId + '-' + alert.ts);
  } else if (sev === 'MEDIUM') {
    toast('Alert', headline, sev, 6000, 'med-' + shipId + '-' + alert.ts);
  } else {
    toast('Info', headline, sev, 4000, 'info-' + shipId + '-' + alert.ts);
  }
  if (store) {
    renderAlertFeed(store);
    renderShipDetail(store);
  }
  if (shipId) onFleetSelect(shipId);
}

async function refreshFromServer() {
  try {
    const state = await api.state();
    store?.applySnapshot(state);
    renderAll();
    if (store?.zones) renderZonesOnMap(store.zones);
    if (store?.ports && portsLayer) renderPorts(portsLayer, store.ports);
  } catch {
    /* keep last known state */
  }
}

async function fetchMetricsLoop() {
  while (feed?.ws?.readyState === WebSocket.OPEN) {
    try {
      const m = await api.metrics();
      store?.applyMetrics(m);
      if (store) {
        renderStatStrip(store);
        renderSystemMetrics(store, m);
      }
    } catch {
      /* ignore */
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

function renderAll() {
  if (!store) return;
  renderStatStrip(store);
  renderSystemMetrics(store, store.metrics);
  renderFleetList(store);
  renderShipDetail(store);
  renderAlertFeed(store);
  syncOpenShipSelects();
  updateShipMarkers();
}

function renderTick() {
  if (!store) return;
  renderStatStrip(store);
  renderSystemMetrics(store, store.metrics);
  renderFleetList(store);
  renderShipDetail(store);
  renderAlertFeed(store);
  syncOpenShipSelects();
  updateShipMarkers();
}

function renderZonesOnMap(zones) {
  if (!map || !zonesLayer) return;
  renderZones(zonesLayer, zones, { onClick: (poly, zone) => zoneOptions(poly, zone) });
}

// ---------------------------------------------------------------------------
// Session resume / boot
// ---------------------------------------------------------------------------
export function resumeSession() {
  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) {
    bearerToken = saved;
    api.setToken(saved);
  } else {
    bearerToken = 'command-alpha';
    api.setToken('command-alpha');
  }
  renderDashboard();
  connectFeed();
  return true;
}

export async function boot() {
  resumeSession();
}

export function refresh() {
  refreshFromServer();
}

export function openZoneForm(vertices) {
  window.__openZoneModal?.(vertices);
}

export function openDistress(shipId, shipName) {
  window.__openDistressModal?.(shipId, shipName);
}

export function openDirective(shipId, shipName) {
  window.__openDirectiveModal?.(shipId, shipName);
}
