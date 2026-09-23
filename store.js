// State store + snapshot renderer: ships/ports/zones/alerts/directives.
// Renders fleet list, alert feed, selected ship detail, and stat strip.

import { el, fmt, SEVERITY_ORDER, COLORS, $, $$, toast } from './util.js';
import { api } from './api.js';

// Cached DOM nodes can go stale: the stat strip is created after mountStore() runs
// and panels get re-rendered, so re-query instead of silently rendering nothing.
function live(cached, selector) {
  if (cached && cached.isConnected) return cached;
  return $(selector);
}

export class FeedStore {
  constructor() {
    this.ships = new Map();
    this.ports = [];
    this.zones = [];
    this.alerts = [];
    this.directives = [];
    this.config = {};
    this.metrics = {};
    this.selectedId = null;
    this.lastAlertTime = 0;
    this.tapwatch = '—';
    this.grpcMs = 0;
    this.rerouting = 0;
    this.arrived = 0;
    this.strayCount = 0;
    this.fleet = 0;
    this.fuelWarnCount = 0;
    this.rttMs = null;
    this.history = [];
    this.isHistoryMode = false;
  }

  recordHistory() {
    if (this.isHistoryMode) return;
    const shipsCopy = new Map();
    for (const [id, s] of this.ships.entries()) {
      shipsCopy.set(id, { ...s, position: Array.isArray(s.position) ? [...s.position] : s.position });
    }
    this.history.push({
      time: Date.now(),
      ships: shipsCopy,
      fleet: this.fleet,
      rerouting: this.rerouting,
      arrived: this.arrived,
      strayCount: this.strayCount,
    });
    if (this.history.length > 120) this.history.shift();
  }

  applySnapshot(msg) {
    // Merge-only semantics: a partial frame (e.g. `{metrics}`) must NEVER wipe
    // keys the frame does not carry. Previously the 2 s metrics poll called this
    // with `{metrics}` only, which deleted every ship from the Map and reset
    // ports/zones to [] - blanking the fleet list, the map markers and the
    // "Target vessel" dropdowns in the directive/distress modals.
    if (Array.isArray(msg.ships)) {
      const byKey = new Map();
      for (const s of msg.ships) byKey.set(s.id || s.shipId, s);
      const incoming = new Set(byKey.keys());
      for (const id of this.ships.keys()) if (!incoming.has(id)) this.ships.delete(id);
      for (const s of byKey.values()) this.ships.set(s.id || s.shipId, s);
    }
    if (Array.isArray(msg.ports)) this.ports = msg.ports;
    if (Array.isArray(msg.zones)) this.zones = msg.zones;
    // Zone events carry ONE zone (created/updated/removed) instead of a list.
    if (msg.zone && msg.zone.id) {
      const rest = this.zones.filter((z) => z.id !== msg.zone.id);
      this.zones = msg.action === 'removed' ? rest : [...rest, msg.zone];
    }
    if (msg.lastAlertTime) this.lastAlertTime = msg.lastAlertTime;
    if (Array.isArray(msg.alerts)) this.alerts = msg.alerts.filter(Boolean).slice(0, 600);
    else if (msg.alert) {
      this.alerts.unshift(msg.alert);
      if (this.alerts.length > 600) this.alerts.pop();
    }
    if (Array.isArray(msg.directives)) this.directives = msg.directives.filter(Boolean).slice(0, 200);
    else if (msg.directive) {
      this.directives.unshift(msg.directive);
      if (this.directives.length > 200) this.directives.pop();
    }
    if (msg.metrics) this.applyMetrics(msg.metrics);
    if (msg.config) this.config = msg.config;
    this.recomputeCounters();
    this.recordHistory();
  }

  applyMetrics(m) {
    if (!m) return;
    this.metrics = m;
    if (m.config) this.config = m.config;
  }

  applyTick(msg) {
    if (msg.t0 != null && Number.isFinite(msg.t0)) {
      this.tapwatch = ((performance.now() - msg.t0)).toFixed(1) + ' ms';
    } else {
      this.tapwatch = '—';
    }
    const t = msg.jobTime;
    this.grpcMs = Number.isFinite(t) ? Math.round(t) : 0;

    for (const s of (msg.ships || [])) this.ships.set(s.id || s.shipId, s);

    if (msg.ports) this.ports = msg.ports;
    if (msg.zones) this.zones = msg.zones;
    if (msg.alert) {
      this.alerts.unshift(msg.alert);
      if (this.alerts.length > 600) this.alerts.pop();
    }
    if (msg.directive) {
      this.directives.unshift(msg.directive);
      if (this.directives.length > 200) this.directives.pop();
    }
    this.recomputeCounters();
    this.recordHistory();
  }

  recomputeCounters() {
    let arr = 0, dest = 0, strays = 0, fuelWarn = 0, fleet = 0;
    for (const s of this.ships.values()) {
      fleet++;
      if (s.status === 'arrived') arr++;
      if (s.status === 'en_route' || s.status === 'normal' || s.status === 'rerouting') dest++;
      if (s.status === 'stranded' || s.status === 'out_of_fuel' || s.status === 'distressed') strays++;
      if (s.flags?.includes('insufficient_fuel')) fuelWarn++;
      else if ((s.fuel ?? 0) < 0.25) fuelWarn++;
    }
    this.fleet = fleet;
    this.rerouting = this.ships.size - arr;
    this.arrived = arr;
    this.strayCount = strays;
    this.fuelWarnCount = fuelWarn;
  }
}

export function mountStore(host) {
  window.__storeHost = host;
  host.statFleet = $('#stat-fleet');
  host.statEnroute = $('#stat-enroute');
  host.statArrived = $('#stat-arrived');
  host.statStrays = $('#stat-strays');
  host.statFuel = $('#stat-fuel');
  host.statWeather = $('#stat-weather-mini');
  host.statWeatherMini = $('#stat-weather-mini');
  host.statSim = $('#stat-sim');
  host.statFan = $('#stat-fan');
  host.statNlp = $('#stat-nlp');
  host.fleetList = $('#fleet-list');
  host.alertFeed = $('#alert-feed');
  host.shipDetail = $('#ship-detail');
  host.shipTools = $('#ship-tools');
}

export function renderConnState(state) {
  const dot = $('#conn-dot');
  const label = $('#conn-label');
  if (!dot || !label) return;
  dot.className = 'dot' + (state === 'open' ? ' on' : '');
  label.textContent = state === 'open' ? 'live' : state === 'connecting' ? 'connecting' : state;
}

export function renderRtt(rtt) {
  const el_ = $('#stat-rtt');
  if (el_) el_.textContent = rtt != null ? `${rtt} ms` : '—';
}

export function renderStatStrip(s) {
  s.statFleet = live(s.statFleet, '#stat-fleet');
  s.statEnroute = live(s.statEnroute, '#stat-enroute');
  s.statArrived = live(s.statArrived, '#stat-arrived');
  s.statStrays = live(s.statStrays, '#stat-strays');
  s.statFuel = live(s.statFuel, '#stat-fuel');
  if (!s.statFleet) return;
  s.statFleet.textContent = s.fleet;
  if (s.statEnroute) s.statEnroute.textContent = s.rerouting;
  if (s.statArrived) s.statArrived.textContent = s.arrived;
  if (s.statStrays) s.statStrays.textContent = s.strayCount;
  if (s.statFuel) s.statFuel.textContent = s.fuelWarnCount;
}

export function renderSystemMetrics(s, m) {
  m = m || {};
  s.statWeatherMini = live(s.statWeatherMini, '#stat-weather-mini');
  s.statSim = live(s.statSim, '#stat-sim');
  s.statNlp = live(s.statNlp, '#stat-nlp');
  s.statFan = live(s.statFan, '#stat-fan');
  // Field names mirror SimulationEngine.metrics_payload(): the previous keys
  // (sim.batchMS / weather.cacheHit / nlp.parseMs / fanout.*) do not exist in the
  // payload, so "Sim", "NLP" and "Fan-out" were permanently blank.
  const w = m.weather || {};
  const nlp = m.nlp || {};
  if (s.statWeatherMini) {
    s.statWeatherMini.textContent = w.offline ? 'offline' : (w.error_count > 0 ? 'cache' : 'live');
    s.statWeatherMini.title = `${w.provider || 'weather'} | cells=${w.cached_cells ?? 0} | refresh=${w.last_refresh_ms ?? 0}ms`;
  }
  if (s.statSim) {
    const t = m.tick_ms_last;
    s.statSim.textContent = Number.isFinite(t) ? t.toFixed(1) + 'ms' : '-';
    s.statSim.title = `tick avg ${m.tick_ms_avg ?? '-'}ms / max ${m.tick_ms_max ?? '-'}ms / ${m.ticks ?? 0} ticks`;
  }
  if (s.statNlp) {
    const l = nlp.last_latency_ms;
    s.statNlp.textContent = Number.isFinite(l) ? l.toFixed(1) + 'ms' : '-';
    s.statNlp.title = `${nlp.provider || 'nlp'} | source=${nlp.last_source || '-'} | llm=${nlp.llm_calls ?? 0} / heuristic=${nlp.heuristic_calls ?? 0}`;
  }
  if (s.statFan) {
    const b = m.broadcast_ms_last;
    s.statFan.textContent = Number.isFinite(b) ? `${b}ms / ${m.client_count ?? 0}c` : '-';
    s.statFan.title = `broadcast max ${m.broadcast_ms_max ?? '-'}ms | messages ${m.messages_sent ?? 0}`;
  }
}

export function renderFleetList(s) {
  s.fleetList = live(s.fleetList, '#fleet-list');
  const host = s.fleetList;
  if (!host) return;
  const filterVal = $('#fleet-filter')?.value?.trim().toLowerCase() || '';
  let ships = Array.from(s.ships.values()).sort((a, b) => String(a.name || a.id || a.shipId).localeCompare(b.name || b.id || b.shipId));
  if (filterVal) {
    ships = ships.filter((ship) => {
      const text = `${ship.name || ''} ${ship.id || ''} ${ship.shipId || ''} ${ship.status || ''} ${ship.flag || ''} ${ship.role || ''} ${ship.payload || ''}`.toLowerCase();
      return text.includes(filterVal);
    });
  }
  const countEl = $('#fleet-count');
  if (countEl) countEl.textContent = `${ships.length}/${s.ships.size}`;
  if (ships.length === 0) {
    host.innerHTML = '';
    host.appendChild(critNote(s));
    return;
  }
  renderFleetRows(host, ships, s.selectedId);
}

function critNote(s) {
  const parts = [];
  if (s.strayCount > 0) parts.push(`${s.strayCount} stranded`);
  if (s.fuelWarnCount > 0) parts.push(`${s.fuelWarnCount} fuel-critical`);
  return el('div', { class: 'hint', text: parts.length ? `fleet: ${parts.join(' · ')}` : 'no ships match filter' });
}

function shipChipsHTML(status, flags) {
  const list = [status, ...(flags || [])].filter(Boolean);
  return list
    .map((c) => `<span class="badge" style="color:${COLORS[c] || COLORS.normal}">${c}</span>`)
    .join('');
}

export function renderFleetRows(host, ships, selectedId) {
  const html = ships
    .map((ship) => {
      const shipId = ship.id || ship.shipId;
      const status = ship.status || 'en_route';
      const flags = ship.flags || [];
      const isSelected = shipId === selectedId;
      const meter = ship.fuel != null ? (ship.fuel_capacity ? ship.fuel / ship.fuel_capacity : ship.fuel) : 0;
      const fuelCls = meter < 0.25 ? 'crit' : meter < 0.5 ? 'warn' : '';
      const color = COLORS[status] || COLORS.normal;
      const shipChips = shipChipsHTML(status, flags);
      const firstFlag = flags[0] || status;
      const cargo = ship.payload || ship.cargo || 'cargo';
      return `
      <div class="ship-row${isSelected ? ' selected' : ''}" data-id="${shipId}" data-color="${color}">
        <div class="id">
          <div>${ship.name || shipId} <span class="badge" style="color:${color}">${status}</span></div>
          <div class="meta">${ship.flag || 'Panama'} · ${firstFlag} · ${cargo}</div>
          <div class="fuel-bar ${fuelCls}"><i style="width:${Math.max(0, Math.min(100, meter * 100))}%"></i></div>
        </div>
        <div class="chips">${shipChips}</div>
      </div>`;
    })
    .join('');
  host.innerHTML = html;
  host.querySelectorAll('.ship-row').forEach((node) => {
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      onFleetSelect(node.getAttribute('data-id'));
    });
  });
}

export function renderShipDetail(s) {
  s.shipDetail = live(s.shipDetail, '#ship-detail');
  const host = s.shipDetail;
  if (!host) return;
  const ship = s.ships.get(s.selectedId);
  if (!ship) {
    host.innerHTML = `<div class="hint">Select a ship to inspect.</div>`;
    renderShipTools(s);
    return;
  }
  host.innerHTML = shipDetailHTML(ship);
  const drillHost = $('#ship-directives');
  if (drillHost) drillHost.innerHTML = drillsHTML(ship.id || ship.shipId);
  renderShipTools(s);
}

export function renderShipTools(s) {
  s.shipTools = live(s.shipTools, '#ship-tools');
  const host = s.shipTools;
  if (!host) return;
  const ship = s.ships.get(s.selectedId);
  if (!ship) {
    host.innerHTML = `<div class="hint">Select a vessel on map or fleet list to enable command tools.</div>`;
    return;
  }
  const shipId = ship.id || ship.shipId;
  const shipName = ship.name || shipId;
  const portsOptions = (s.ports || [])
    .map((p) => {
      const pid = p.id || p.name;
      const pname = p.name || p.id;
      return `<option value="${pid}">${pname}</option>`;
    })
    .join('');

  host.innerHTML = `
    <div class="row wrap" style="gap: 6px; margin-bottom: 8px">
      <button class="btn primary sm" id="btn-tool-directive">Issue Directive</button>
      <button class="btn danger sm" id="btn-tool-distress">Distress Signal</button>
    </div>
    <div class="tool-block">
      <div class="tool-title">Quick Reroute</div>
      <div class="row" style="gap: 6px">
        <select id="tool-reroute-port" class="mini">
          <option value="">Choose Port...</option>
          ${portsOptions}
        </select>
        <button class="btn sm" id="btn-tool-reroute">Reroute</button>
      </div>
    </div>
  `;

  $('#btn-tool-directive')?.addEventListener('click', () => {
    window.__openDirectiveModal?.(shipId, shipName);
  });
  $('#btn-tool-distress')?.addEventListener('click', () => {
    window.__openDistressModal?.(shipId, shipName);
  });
  $('#btn-tool-reroute')?.addEventListener('click', async () => {
    const portId = $('#tool-reroute-port')?.value;
    if (!portId) {
      toast('Reroute aborted', 'Select a destination port first', 'MEDIUM');
      return;
    }
    try {
      await api.reroute(shipId, portId, 'Manual quick reroute');
      toast('Reroute sent', `Rerouting ${shipName} to ${portId}`, 'LOW');
    } catch (err) {
      toast('Reroute failed', String(err?.message ?? err), 'HIGH');
    }
  });
}

export function drillsHTML(shipId) {
  const s = storeRender();
  if (!s) return '';
  const ds = (s.directives || []).filter((d) => d && (d.shipId === shipId || d.ship_id === shipId));
  if (!ds.length) return `<div class="hint">No open directives.</div>`;
  return ds
    .map((d) => {
      const statusStr = d.status || (d.accept === true ? 'accepted' : d.accept === false ? 'rejected' : 'pending');
      const isAccepted = statusStr === 'accepted' || statusStr === 'completed';
      const isRejected = statusStr === 'rejected' || statusStr === 'cancelled';
      const isPending = statusStr === 'pending';
      const color = isAccepted ? COLORS.normal : isRejected ? COLORS.CRITICAL : COLORS.HIGH;
      const label = d.headline || d.destination_label || d.destination_port || (Array.isArray(d.destination) ? `[${d.destination.map(n => Number(n).toFixed(2)).join(', ')}]` : d.destination) || 'Waypoint';
      const noteStr = d.note ? ` — ${d.note}` : (d.response_note ? ` — ${d.response_note}` : '');
      return `
    <div class="directive${isPending ? ' pending' : ''}">
      <div class="head">${d.kind || 'course'} <span class="badge" style="color:${color}">${statusStr}</span></div>
      <div class="msg">${label}${noteStr}</div>
    </div>`;
    })
    .join('');
}

function fuelBar(fuel, capacity) {
  const ratio = fuel != null ? (capacity ? fuel / capacity : fuel) : 0;
  const pct = Math.max(0, Math.min(100, ratio * 100));
  const color = pct < 25 ? COLORS.CRITICAL : pct < 50 ? COLORS.HIGH : COLORS.normal;
  return `<div class="meter"><i style="width:${pct}%;background:${color}"></i></div>`;
}

function shipDetailHTML(ship) {
  const status = ship.status || 'en_route';
  const color = COLORS[status] || COLORS.normal;
  const flags = ship.flags || [];
  const chipItems = [status, ...flags];
  const speed = ship.speed ? fmt.num(ship.speed, 1) + ' kt' : '—';
  const cargo = ship.payload || ship.cargo || '—';
  const distKm = ship.distance_remaining_nm != null ? (ship.distance_remaining_nm * 1.852).toFixed(0) + ' km' : '—';
  return `
    <h4 style="color:${color}">${ship.name || ship.id || ship.shipId}</h4>
    <div class="row hint">${chipItems.map((c) => `<span class="badge" style="color:${COLORS[c] || COLORS.normal}">${c}</span>`).join(' ')} ${ship.flag || 'Panama'}</div>
    <div class="kv">
      <span>Status</span><b>${status}</b>
      <span>Role</span><b>${ship.role || 'cargo'}</b>
      <span>Flag</span><b>${ship.flag || 'Panama'}</b>
      <span>Cargo</span><b>${cargo}</b>
      <span>Speed</span><b>${speed}</b>
      <span>Heading</span><b>${fmt.num(ship.heading, 0)}°</b>
      <span>Fuel</span><b>${fmt.num(ship.fuel, 0)} / ${fmt.num(ship.fuel_capacity || 100, 0)}</b>
      <span>Dist. left</span><b>${distKm}</b>
    </div>
    ${fuelBar(ship.fuel, ship.fuel_capacity)}
    <div class="kv">
      <span>Destination</span><b>${ship.destination_label || ship.destination || '—'}</b>
      <span>ETA</span><b>${ship.eta_hours != null ? fmt.hours(ship.eta_hours) : (ship.eta ? fmt.hours(ship.eta) : '—')}</b>
      <span>Fuel range</span><b>${ship.fuel_range_nm != null ? ship.fuel_range_nm.toFixed(0) + ' nm' : '—'}</b>
    </div>
    <div class="tool-block">
      <div class="tool-title">Directives received</div>
      <div class="scroll" id="ship-directives"></div>
    </div>
  `;
}

export function renderAlertFeed(s) {
  s.alertFeed = live(s.alertFeed, '#alert-feed');
  const host = s.alertFeed;
  if (!host) return;
  const filterSev = $('#alert-filter')?.value || 'LOW';
  const minSevVal = SEVERITY_ORDER[filterSev] || 1;
  const alerts = (s.alerts || []).filter((a) => {
    const sev = a.severity || a.sev || 'LOW';
    return (SEVERITY_ORDER[sev] || 1) >= minSevVal;
  });
  host.innerHTML = '';
  if (!alerts.length) {
    host.innerHTML = '<div class="hint">No alerts matching filter.</div>';
    return;
  }
  for (const a of alerts) {
    const node = el('div', { class: `alert ${a.severity || a.sev || 'LOW'}` });
    node.innerHTML = alertHTML(a);
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      if (a.shipId) onFleetSelect(a.shipId);
    });
    host.appendChild(node);
  }
}

function alertHTML(a) {
  const time = fmt.time(a.ts || 0);
  const meta = a.shipId ? `<span class="meta">${a.shipName || a.shipId}</span>` : '';
  const chips = (a.flags || [])
    .map((f) => `<span class="badge">${f}</span>`)
    .join('');
  const detail = a.detail ? `<div class="meta">${a.detail}</div>` : '';
  return `<div class="head">
    <span class="type" style="color:${COLORS[a.severity] || COLORS.normal}">${a.severity || a.sev}</span>
    <span class="time">${time}</span>
  </div>
  <div class="msg">
    ${a.headline || a.message || ''}${meta}${detail}${chips}
  </div>`;
}

export function renderPortList(s) {
  const host = s.portList;
  if (!host) return;
  const ports = s.ports || [];
  host.innerHTML = '';
  for (const p of ports) {
    const lat = p.lat ?? (Array.isArray(p.position) ? p.position[0] : null);
    const lng = p.lng ?? (Array.isArray(p.position) ? p.position[1] : null);
    const row = el('div', { class: 'row hint', text: `${p.name} (${lat != null ? Number(lat).toFixed(3) : '?'}, ${lng != null ? Number(lng).toFixed(3) : '?'})` });
    host.appendChild(row);
  }
}

let currentMarkers = null;
let currentMap = null;

export function setMap(m, markers) {
  currentMap = m;
  currentMarkers = markers;
}

export function onFleetSelect(id) {
  const s = storeRender();
  if (!s) return;
  s.selectedId = id;
  renderFleetList(s);
  renderShipDetail(s);
  focusMarker(id);
}

function storeRender() {
  return window.__storeHost;
}

function focusMarker(id) {
  if (!currentMarkers || !currentMap) return;
  const pair = currentMarkers.get(id);
  if (!pair) return;
  currentMap.setView(pair[0].getLatLng(), currentMap.getZoom(), { animate: true, duration: 0.6 });
  pair[0].openPopup();
}
