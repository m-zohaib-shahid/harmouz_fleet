// Zone drawing: polyline tool and rectangle tool. On completion, call the
// create-zone modal with preview + vertices. Also renders a demo Strait zone.

import { el, fmt } from './util.js';
import { api } from './api.js';
import { toast } from './util.js';

export class ZoneDraw {
  constructor({ map, zonesLayer, onComplete }) {
    this.map = map;
    this.zonesLayer = zonesLayer;
    this.onComplete = onComplete;
    this.mode = 'poly';
    this.points = [];
    this.latlngs = [];
    this.polyLine = null;
    this.drawing = false;
    this.rect = null;
    this.startLatLng = null;
    this.activeTool = null; // 'poly' | 'rect'
    this.handleMapClick = this.handleMapClick.bind(this);
    this.finalize = this.finalize.bind(this);
    map.on('click', this.handleMapClick);
  }

  setMode(mode) {
    this.mode = mode === 'rect' ? 'rect' : 'poly';
    this.clear();
  }

  getToolLabel() {
    return this.mode === 'rect' ? 'Rectangle tool' : 'Polygon tool';
  }

  clear() {
    if (this.polyLine) {
      this.polyLine.remove();
      this.polyLine = null;
    }
    if (this.rect) {
      this.rect.remove();
      this.rect = null;
    }
    this.points = [];
    this.latlngs = [];
    this.startLatLng = null;
    this.drawing = false;
  }

  handleMapClick(e) {
    if (this.activeTool !== this.mode) return;
    if (this.mode === 'poly') {
      this.points.push(e.latlng);
      if (!this.polyLine) {
        this.polyLine = L.polyline([], { color: '#35c2ff', weight: 2, dashArray: '5 5' }).addTo(this.map);
      }
      this.polyLine.addLatLng(e.latlng);
      this.latlngs.push([e.latlng.lat, e.latlng.lng]);
    } else if (this.mode === 'rect') {
      if (!this.startLatLng) {
        this.startLatLng = e.latlng;
        this.rect = L.rectangle([e.latlng, e.latlng], {
          color: '#35c2ff',
          fillColor: '#35c2ff',
          fillOpacity: 0.12,
          dashArray: '4 4',
        }).addTo(this.map);
      } else {
        const a = this.startLatLng;
        const b = e.latlng;
        const ll = L.latLng(
          Math.min(a.lat, b.lat),
          Math.min(a.lng, b.lng)
        );
        const ur = L.latLng(
          Math.max(a.lat, b.lat),
          Math.max(a.lng, b.lng)
        );
        this.rect.setBounds([ll, ur]);
        this.latlngs = [
          [ll.lat, ll.lng],
          [ur.lat, ll.lng],
          [ur.lat, ur.lng],
          [ll.lat, ur.lng],
        ];
      }
    }
  }

  cancel() {
    this.clear();
  }

  // Unbind the map listener: without this every tool switch leaked a stale click
  // handler that kept accumulating vertices in a dead controller.
  destroy() {
    try {
      this.map?.off('click', this.handleMapClick);
    } catch {
      /* ignore */
    }
    this.clear();
  }

  async finalize() {
    const vertices = [...this.latlngs];
    this.clear();
    if (!vertices.length) return;
    this.onComplete?.(vertices);
  }
}

let zoneDrawController = null;

export function startZoneDrawing(map, zonesLayer, onComplete) {
  if (zoneDrawController) zoneDrawController.destroy();
  zoneDrawController = new ZoneDraw({ map, zonesLayer, onComplete });
  zoneDrawController.activeTool = zoneDrawController.mode;
  return zoneDrawController;
}

export function stopZoneDrawing() {
  if (zoneDrawController) {
    zoneDrawController.destroy();
    zoneDrawController = null;
  }
}

export function redrawZoneDraw(zoneDraw, polyLine, latlngs) {
  if (!zoneDraw) return;
  if (polyLine) {
    polyLine.setLatLngs(latlngs);
  } else {
    const fresh = L.polyline(latlngs, { color: '#35c2ff', weight: 2, dashArray: '5 5' }).addTo(zoneDraw.map);
    zoneDraw.polyLine = fresh;
  }
  zoneDraw.latlngs = latlngs;
}
