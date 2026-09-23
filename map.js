import { el, fmt, COLORS, lerpCoord, lerpAngle, statusColor, haversineKm } from './util.js';

export const MARKER_ROUND = 0.0008;

export function mountMap(mapEl) {
  if (!mapEl) return null;
  const map = L.map(mapEl, { zoomControl: false }).setView([26.4, 56.5], 8);
  L.control.zoom({ position: 'bottomright' }).addTo(map);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap',
    minZoom: 2,
    maxZoom: 18,
  }).addTo(map);

  const zonesLayer = L.layerGroup().addTo(map);
  const shipsLayer = L.layerGroup().addTo(map);
  const portsLayer = L.layerGroup().addTo(map);

  return { map, zonesLayer, shipsLayer, portsLayer };
}

export function addWater(map) {
  if (!map) return null;
  return L.polygon(
    [
      [25.8, 58],
      [27.6, 62],
      [25.2, 61.4],
      [24.7, 56.8],
    ],
    {
      color: '#1d3550',
      fillColor: '#0a2338',
      fillOpacity: 0.55,
      weight: 1,
    }
  ).addTo(map);
}

export function renderPorts(layer, ports) {
  if (!layer || typeof layer.clearLayers !== 'function') return layer;
  layer.clearLayers();
  for (const p of ports || []) {
    // API returns position:[lat,lng] OR legacy lat/lng fields
    const lat = p.lat ?? (Array.isArray(p.position) ? p.position[0] : null);
    const lng = p.lng ?? (Array.isArray(p.position) ? p.position[1] : null);
    if (!lat || !lng) continue;
    const marker = L.circleMarker([lat, lng], {
      radius: 7,
      color: '#35c2ff',
      fillColor: '#0b1723',
      fillOpacity: 0.9,
      weight: 2,
      interactive: true,
    }).addTo(layer);
    marker.bindPopup(
      `<b>⚓ ${p.name || p.id}</b><br/>ID: ${p.id || '—'}<br/>Coords: ${Number(lat).toFixed(3)}, ${Number(lng).toFixed(3)}`,
      { className: 'port-popup' }
    );
    marker.on('click', (e) => {
      e.originalEvent?.stopPropagation?.();
      marker.openPopup();
    });
  }
  return layer;
}

export function renderZones(layerOrMap, zones, opts = {}) {
  if (!layerOrMap) return;
  if (typeof layerOrMap.clearLayers === 'function') layerOrMap.clearLayers();
  for (const z of zones || []) {
    const coords = z.coords || z.polygon;
    if (!coords || !coords.length) continue;
    const color = opts?.color ? opts.color(z) : (COLORS.zone[z.severity] || COLORS.zone.HIGH);
    const opacity = z.active ? 0.22 : 0.12;
    const poly = L.polygon(coords, {
      color,
      fillColor: color,
      fillOpacity: opacity,
      weight: 1.5,
      dashArray: '5 5',
    }).addTo(layerOrMap);
    poly.bindPopup(
      `<b>${z.name}</b><br/>severity: ${z.severity} | active: ${z.active ? 'yes' : 'no'}${z.ports ? '<br/>ports: ' + z.ports.join(', ') : ''}`,
      { className: 'zone-popup' }
    );
    if (!z.active) poly.setStyle({ opacity: 0.4 });
    if (opts?.onClick) {
      poly.on('click', (e) => {
        e.originalEvent?.stopPropagation?.();
        opts.onClick(poly, z);
      });
    }
  }
}

export function renderShips(layerOrMap, ships, opts = {}) {
  if (!layerOrMap) return new Map();
  if (!layerOrMap._shipMarkers) layerOrMap._shipMarkers = new Map();
  const existingMap = layerOrMap._shipMarkers;
  const currentIds = new Set();
  const isSelected = opts?.selected || (() => false);

  for (const ship of ships || []) {
    const shipId = ship.id || ship.shipId;
    if (!shipId) continue;
    const coords = ship.position || ship.coords;
    if (!coords || coords.length < 2) continue;
    currentIds.add(shipId);

    const color = statusColor(ship);
    const size = 24;
    const isFwd = opts?.pending ?? false;
    const selected = isSelected(shipId);
    const popupHTML = opts?.onPopup ? opts.onPopup(ship) : null;
    const html = `
      <div class="ship-marker${selected ? ' selected' : ''}" style="transform: translate(-50%, -50%)">
        <svg width="${size}" height="${size}" viewBox="0 0 100 100">
          <defs>
            <radialGradient id="hull-${size}" cx="50%" cy="50%" r="50%">
              <stop offset="55%" stop-color="${color}"/>
              <stop offset="100%" stop-color="rgba(255,255,255,0)"/>
            </radialGradient>
          </defs>
          <circle class="ring${isFwd ? ' alerting' : ''}" cx="50" cy="50" r="44" fill="${color}" opacity="0.28"/>
          <circle class="hull${isFwd ? ' alerting-hull' : ''}" cx="50" cy="50" r="22" fill="${color}" stroke="#0b1723" stroke-width="1.5"/>
          <text x="50" y="60" text-anchor="middle" font-family="JetBrains Mono, Consolas" font-size="9" fill="#000">${shipId}</text>
        </svg>
        <div class="ship-label">${ship.name || shipId}</div>
      </div>`;

    let entry = existingMap.get(shipId);
    if (entry) {
      const [marker, lastState] = entry;
      marker.setLatLng([coords[0], coords[1]]);
      const zOffset = selected ? 2000 : (ship.status === 'distressed' ? 1000 : 0);
      marker.setZIndexOffset(zOffset);

      if (lastState.html !== html) {
        const icon = L.divIcon({
          className: '',
          html,
          iconSize: [size + 60 + (isFwd ? 6 : 0), size + 14],
          iconAnchor: [(size + 60 + (isFwd ? 6 : 0)) / 2, size / 2 + 4],
        });
        marker.setIcon(icon);
      }
      if (popupHTML && marker.getPopup()) {
        marker.setPopupContent(popupHTML);
      }
      entry[1] = { html, ship };
    } else {
      const icon = L.divIcon({
        className: '',
        html,
        iconSize: [size + 60 + (isFwd ? 6 : 0), size + 14],
        iconAnchor: [(size + 60 + (isFwd ? 6 : 0)) / 2, size / 2 + 4],
      });
      const zOffset = selected ? 2000 : (ship.status === 'distressed' ? 1000 : 0);
      const marker = L.marker([coords[0], coords[1]], {
        icon,
        zIndexOffset: zOffset,
        riseOnHover: true,
      }).addTo(layerOrMap);

      marker.on('click', (e) => {
        e.originalEvent?.stopPropagation?.();
        if (opts?.onSelect) opts.onSelect(ship);
      });

      if (popupHTML) {
        marker.bindPopup(popupHTML, { className: 'ship-popup', closeButton: false });
      }

      existingMap.set(shipId, [marker, { html, ship }]);
    }
  }

  for (const [id, [marker]] of existingMap.entries()) {
    if (!currentIds.has(id)) {
      if (typeof layerOrMap.removeLayer === 'function') layerOrMap.removeLayer(marker);
      existingMap.delete(id);
    }
  }

  return existingMap;
}

export { statusColor } from './util.js';
