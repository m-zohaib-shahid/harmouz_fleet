"""Pure-python geospatial kernel (no numpy / shapely / geopandas).

Everything the engine needs is implemented here so the backend has zero native
dependencies and installs in seconds inside the Docker build:

* Haversine distance, initial bearing, destination projection
* Point-in-polygon (ray casting) used for geofencing and navigable water
* Point-to-polygon boundary distance (for zone-approach warnings)
* Path length / waypoint walking helpers used by the 1 Hz movement integrator
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

LatLng = Sequence[float]  # [lat, lng]
EARTH_R_KM = 6371.0088
NM_PER_KM_CONST = 0.5399568
NM_PER_KM = NM_PER_KM_CONST  # alias used by the simulation engine


# ---------------------------------------------------------------------------
# Distance / bearing
# ---------------------------------------------------------------------------
def haversine_km(a: LatLng, b: LatLng) -> float:
    """Great-circle distance between two [lat, lng] points, in kilometres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: LatLng, b: LatLng) -> float:
    """Initial true bearing from a to b, degrees clockwise from north."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination_point(a: LatLng, bearing: float, distance_km: float) -> List[float]:
    """Project a point `distance_km` along `bearing` (degrees) from a."""
    ang = distance_km / EARTH_R_KM
    brg = math.radians(bearing)
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return [math.degrees(lat2), ((math.degrees(lon2) + 540.0) % 360.0) - 180.0]


def lerp_point(a: LatLng, b: LatLng, t: float) -> List[float]:
    t = max(0.0, min(1.0, t))
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]


def angle_diff(a: float, b: float) -> float:
    """Shortest signed delta in degrees between two headings (-180, 180]."""
    return ((b - a + 540.0) % 360.0) - 180.0


def _point_segment_km(p: LatLng, a: LatLng, b: LatLng) -> float:
    """Distance from point p to segment a-b using an equirectangular projection.

    Accurate to a few metres over the ~10 km scale of a sea zone, and far
    cheaper than a full geodesic solution - which matters because this runs
    inside the 1 Hz geofence check for every ship / zone pair.
    """
    scale = math.cos(math.radians(p[0]))
    px, py = p[1] * scale, p[0]
    ax, ay = a[1] * scale, a[0]
    bx, by = b[1] * scale, b[0]
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    deg = math.hypot(px - cx, py - cy)
    return deg * 111.32


def distance_to_polygon_km(point: LatLng, polygon: Sequence[LatLng]) -> float:
    """Shortest distance from `point` to the polygon boundary, in km."""
    if len(polygon) < 2:
        return float("inf")
    best = float("inf")
    for i in range(len(polygon)):
        a = polygon[i]
        b = polygon[(i + 1) % len(polygon)]
        d = _point_segment_km(point, a, b)
        if d < best:
            best = d
    return best


def segments_intersect(p1: LatLng, p2: LatLng, p3: LatLng, p4: LatLng) -> bool:
    """2-D orientation test for segment intersection (used for path-vs-zone)."""

    def orient(a: LatLng, b: LatLng, c: LatLng) -> float:
        return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def path_intersects_polygon(path: Sequence[LatLng], polygon: Sequence[LatLng]) -> bool:
    """True when the polyline `path` enters the polygon at any point."""
    if len(path) < 2 or len(polygon) < 3:
        return False
    bbox = polygon_bbox(polygon)
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        if bbox_contains(bbox, a, 0.05) or bbox_contains(bbox, b, 0.05):
            if point_in_polygon(a, polygon) or point_in_polygon(b, polygon):
                return True
        # coarse reject: only run the expensive intersection test near the zone
        if bbox_contains(bbox, a, 1.0) or bbox_contains(bbox, b, 1.0):
            for j in range(len(polygon)):
                c = polygon[j]
                d = polygon[(j + 1) % len(polygon)]
                if segments_intersect(a, b, c, d):
                    return True
    return False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def path_length_nm(points: Sequence[LatLng], start: LatLng | None = None) -> float:
    """Total length of a waypoint list in nautical miles."""
    if not points:
        return 0.0
    total_km = 0.0
    prev = start
    for p in points:
        if prev is not None:
            total_km += haversine_km(prev, p)
        prev = p
    return total_km * NM_PER_KM_CONST


def advance_along_path(
    position: LatLng,
    waypoints: Sequence[LatLng],
    distance_km: float,
    start_index: int = 0,
) -> Tuple[List[float], int, float]:
    """Move `distance_km` along `waypoints` starting at `position`.

    Returns ``(new_position, new_index, remaining_km)`` where ``new_index`` is
    the index of the next waypoint to steer for and ``remaining_km`` is the
    unused part of the budget (i.e. the ship reached the end of the path).
    """
    pos = [float(position[0]), float(position[1])]
    budget = distance_km
    idx = start_index
    while idx < len(waypoints):
        target = [float(waypoints[idx][0]), float(waypoints[idx][1])]
        leg = haversine_km(pos, target)
        if leg <= 1e-9:
            idx += 1
            continue
        if budget < leg:
            brg = bearing_deg(pos, target)
            pos = destination_point(pos, brg, budget)
            return pos, idx, 0.0
        budget -= leg
        pos = target
        idx += 1
    return pos, idx, budget


def simplify_path(
    points: Sequence[LatLng], tolerance_deg: float = 1e-3
) -> List[List[float]]:
    """Drop duplicate / near-duplicate consecutive waypoints."""
    out: List[List[float]] = []
    for p in points:
        pt = [float(p[0]), float(p[1])]
        if out and abs(out[-1][0] - pt[0]) < tolerance_deg and abs(out[-1][1] - pt[1]) < tolerance_deg:
            continue
        out.append(pt)
    return out


def nearest(items: Iterable[LatLng], point: LatLng) -> Tuple[int, float]:
    """Index + distance (km) of the closest item to `point`."""
    best_i, best_d = -1, float("inf")
    for i, item in enumerate(items):
        d = haversine_km(point, item)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


# ---------------------------------------------------------------------------
# Polygons (shape tests; python resolves these names at call time)
# ---------------------------------------------------------------------------
def point_in_polygon(point: LatLng, polygon: Sequence[LatLng]) -> bool:
    """Ray-casting point-in-polygon test (geo coordinates: x=lng, y=lat)."""
    if len(polygon) < 3:
        return False
    x, y = point[1], point[0]
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_bbox(polygon: Sequence[LatLng]) -> Tuple[float, float, float, float]:
    """(south, west, north, east) bounding box of a polygon."""
    lats = [p[0] for p in polygon]
    lngs = [p[1] for p in polygon]
    return min(lats), min(lngs), max(lats), max(lngs)


def bbox_contains(
    bbox: Tuple[float, float, float, float], point: LatLng, margin: float = 0.0
) -> bool:
    south, west, north, east = bbox
    return (
        south - margin <= point[0] <= north + margin
        and west - margin <= point[1] <= east + margin
    )


def bboxes_intersect(
    a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
