"""Dynamic A* pathfinding over a lat/lng grid clipped to navigable water.

Design notes
------------
* The grid is built once at boot from the `navigableWater` polygon in
  fleet.json. A cell is navigable when its **centre** falls inside the polygon,
  which is what lets a route thread the ~5 km wide Strait of Hormuz narrows
  (the polygon pinches shut at ~lng 56.44, so cells must be ~3 km).
* Every cell stores a *clearance* value (distance in cells to the nearest
  non-navigable cell). Clearance penalises coastal hugging and keeps routes
  mid-channel, which is what a real bridge team would do.
* Restricted zones are converted to blocked cell sets, inflated by
  ZONE_SAFETY_KM so ships never clip a zone edge.
* A* uses a geometric (admissible) heuristic plus a small weight for speed, an
  expansion cap, and a greedy line-of-sight smoothing pass so the resulting
  track is made of long legs instead of staircase cells.
"""

from __future__ import annotations

import heapq
import math
import time
from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import config
from .config import KM_PER_DEG_LAT
from .geo import (
    NM_PER_KM_CONST,
    haversine_km,
    lerp_point,
    point_in_polygon,
    simplify_path,
)

_N8: Tuple[Tuple[int, int], ...] = (
    (-1, 0),
    (1, 0),
    (0, -1),
    (0, 1),
    (-1, -1),
    (-1, 1),
    (1, -1),
    (1, 1),
)


class NavGrid:
    """Spatial grid of navigable-water cells with A* pathfinding on top."""

    def __init__(
        self,
        bbox: Dict[str, float],
        navigable_polygon: Sequence[Sequence[float]],
        cell_deg: float = config.GRID_CELL_DEG,
    ) -> None:
        self.polygon: List[List[float]] = [[float(p[0]), float(p[1])] for p in navigable_polygon]
        self.south = float(bbox["south"])
        self.north = float(bbox["north"])
        self.west = float(bbox["west"])
        self.east = float(bbox["east"])
        self.cell = float(cell_deg)
        self.nrows = max(1, int(math.ceil((self.north - self.south) / self.cell)))
        self.ncols = max(1, int(math.ceil((self.east - self.west) / self.cell)))
        self.navigable = bytearray(self.nrows * self.ncols)
        self.clearance = bytearray(self.nrows * self.ncols)
        self.build_ms = 0.0
        self.stats: Dict[str, float] = {"searches": 0, "expansions": 0, "last_search_ms": 0.0}
        self._build()
        self._build_clearance()

    # -- grid construction -------------------------------------------------
    def _build(self) -> None:
        t0 = time.perf_counter()
        for r in range(self.nrows):
            lat = self.south + (r + 0.5) * self.cell
            base = r * self.ncols
            for c in range(self.ncols):
                lng = self.west + (c + 0.5) * self.cell
                if point_in_polygon((lat, lng), self.polygon):
                    self.navigable[base + c] = 1
        self.build_ms = (time.perf_counter() - t0) * 1000.0

    def _build_clearance(self) -> None:
        """Multi-source BFS: distance (in cells) to the nearest blocked cell."""
        size = self.nrows * self.ncols
        dist = bytearray([config.MAX_CLEARANCE_CELLS]) * size
        queue: deque = deque()
        for r in range(self.nrows):
            base = r * self.ncols
            for c in range(self.ncols):
                i = base + c
                if not self.navigable[i]:
                    dist[i] = 0
                    queue.append(i)
        while queue:
            i = queue.popleft()
            r, c = divmod(i, self.ncols)
            d = dist[i]
            if d + 1 > config.MAX_CLEARANCE_CELLS:
                continue
            for dr, dc in _N8:
                nr, nc = r + dr, c + dc
                if 0 <= nr < self.nrows and 0 <= nc < self.ncols:
                    j = nr * self.ncols + nc
                    if dist[j] > d + 1:
                        dist[j] = d + 1
                        queue.append(j)
        self.clearance = dist

    # -- cell lookups ------------------------------------------------------
    @property
    def size(self) -> int:
        return self.nrows * self.ncols

    def cell_of(self, lat: float, lng: float) -> Tuple[int, int]:
        r = int((lat - self.south) / self.cell)
        c = int((lng - self.west) / self.cell)
        return max(0, min(self.nrows - 1, r)), max(0, min(self.ncols - 1, c))

    def center(self, r: int, c: int) -> List[float]:
        return [self.south + (r + 0.5) * self.cell, self.west + (c + 0.5) * self.cell]

    def is_open(self, r: int, c: int, blocked: Set[int]) -> bool:
        i = r * self.ncols + c
        if not self.navigable[i]:
            return False
        if i in blocked:
            return False
        return self.clearance[i] >= config.MIN_CLEARANCE_CELLS

    def nearest_open_cell(
        self,
        lat: float,
        lng: float,
        blocked: Iterable[int] = (),
        max_rings: int = 900,
    ) -> Optional[Tuple[int, int]]:
        """BFS outward from the requested point to the closest usable cell."""
        blocked_set = blocked if isinstance(blocked, (set, frozenset)) else set(blocked)
        r0, c0 = self.cell_of(lat, lng)
        if self.is_open(r0, c0, blocked_set):
            return r0, c0
        seen = {(r0, c0)}
        frontier: deque = deque([(r0, c0, 0)])
        while frontier:
            r, c, ring = frontier.popleft()
            if ring > max_rings:
                return None
            for dr, dc in _N8:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < self.nrows and 0 <= nc < self.ncols):
                    continue
                if (nr, nc) in seen:
                    continue
                seen.add((nr, nc))
                if self.is_open(nr, nc, blocked_set):
                    return nr, nc
                frontier.append((nr, nc, ring + 1))
        return None

    # -- restricted zones --------------------------------------------------
    def zone_blocked_cells(
        self, polygon: Sequence[Sequence[float]], safety_km: float = config.ZONE_SAFETY_KM
    ) -> Set[int]:
        """Cell indices covered by a restricted zone, inflated by `safety_km`."""
        if len(polygon) < 3:
            return set()
        pad = (safety_km / KM_PER_DEG_LAT) + self.cell
        lats = [p[0] for p in polygon]
        lngs = [p[1] for p in polygon]
        r_lo, c_lo = self.cell_of(min(lats) - pad, min(lngs) - pad)
        r_hi, c_hi = self.cell_of(max(lats) + pad, max(lngs) + pad)
        blocked: Set[int] = set()
        ring = max(1, int(math.ceil(safety_km / (self.cell * KM_PER_DEG_LAT))) + 1)
        for r in range(r_lo, r_hi + 1):
            for c in range(c_lo, c_hi + 1):
                lat, lng = self.center(r, c)
                if point_in_polygon((lat, lng), polygon):
                    blocked.add(r * self.ncols + c)
                    continue
                inside_ring = False
                for dr in range(-ring, ring + 1):
                    if inside_ring:
                        break
                    for dc in range(-ring, ring + 1):
                        nr, nc = r + dr, c + dc
                        if not (0 <= nr < self.nrows and 0 <= nc < self.ncols):
                            continue
                        pl, pn = self.center(nr, nc)
                        if point_in_polygon((pl, pn), polygon):
                            if haversine_km((lat, lng), (pl, pn)) <= safety_km:
                                blocked.add(r * self.ncols + c)
                                inside_ring = True
                                break
        return blocked

    # -- line of sight -----------------------------------------------------
    def line_of_sight(
        self, a: Sequence[float], b: Sequence[float], blocked: Set[int], samples: int = 0
    ) -> bool:
        span = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        n = samples or max(2, int(span / (self.cell * 0.5)) + 1)
        for i in range(n + 1):
            p = lerp_point(a, b, i / n)
            r, c = self.cell_of(p[0], p[1])
            if not self.is_open(r, c, blocked):
                return False
        return True

    def smooth_path(
        self, points: Sequence[Sequence[float]], blocked: Set[int]
    ) -> List[List[float]]:
        """Greedy string-pulling: keep the longest legal straight legs."""
        pts = [[float(p[0]), float(p[1])] for p in points]
        if len(pts) <= 2:
            return pts
        out: List[List[float]] = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1 and not self.line_of_sight(pts[i], pts[j], blocked):
                j -= 1
            out.append(pts[j])
            i = j
        return out

    # -- A* ----------------------------------------------------------------
    def astar(
        self,
        start: Sequence[float],
        goal: Sequence[float],
        blocked: Optional[Iterable[int]] = None,
    ) -> Optional[List[List[float]]]:
        """Return waypoints for start -> goal (excluding start) or None if cut off."""
        t0 = time.perf_counter()
        blocked_set: Set[int] = set(blocked) if blocked else set()
        s_cell = self.nearest_open_cell(start[0], start[1], blocked_set)
        g_cell = self.nearest_open_cell(goal[0], goal[1], blocked_set)
        if s_cell is None or g_cell is None:
            self._record_search(t0, 0)
            return None
        if s_cell == g_cell:
            self._record_search(t0, 0)
            return [[float(goal[0]), float(goal[1])]]

        sr, sc = s_cell
        gr, gc = g_cell
        ncols = self.ncols
        start_i = sr * ncols + sc
        goal_i = gr * ncols + gc
        goal_ll = self.center(gr, gc)
        heuristic = config.ASTAR_HEURISTIC_WEIGHT

        def h_of(r: int, c: int) -> float:
            lat, lng = self.center(r, c)
            return haversine_km((lat, lng), goal_ll) * NM_PER_KM_CONST * heuristic

        g_score: Dict[int, float] = {start_i: 0.0}
        came: Dict[int, int] = {}
        open_heap: List[Tuple[float, int]] = [(h_of(sr, sc), start_i)]
        closed: Set[int] = set()
        expansions = 0
        reached = start_i == goal_i

        while open_heap:
            _, i = heapq.heappop(open_heap)
            if i in closed:
                continue
            closed.add(i)
            expansions += 1
            if i == goal_i:
                reached = True
                break
            if expansions > config.ASTAR_MAX_EXPANSIONS:
                break
            r, c = divmod(i, ncols)
            gi = g_score[i]
            lat1, lng1 = self.center(r, c)
            for dr, dc in _N8:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < self.nrows and 0 <= nc < ncols):
                    continue
                j = nr * ncols + nc
                if j in closed or not self.is_open(nr, nc, blocked_set):
                    continue
                lat2, lng2 = self.center(nr, nc)
                step_nm = haversine_km((lat1, lng1), (lat2, lng2)) * NM_PER_KM_CONST
                clear = max(1, self.clearance[j])
                tentative = gi + step_nm * (1.0 + config.CLEARANCE_WEIGHT / clear)
                if tentative < g_score.get(j, float("inf")):
                    g_score[j] = tentative
                    came[j] = i
                    heapq.heappush(open_heap, (tentative + h_of(nr, nc), j))

        if not reached:
            # Corridor is closed: the caller decides between `stranded` and an
            # emergency-exit reroute.
            self._record_search(t0, expansions)
            return None

        cells: List[int] = [goal_i]
        node = goal_i
        while node != start_i:
            node = came[node]
            cells.append(node)
        cells.reverse()

        pts: List[List[float]] = [list(map(float, start))]
        pts.extend(self.center(*divmod(i, ncols)) for i in cells[1:])
        pts.append(list(map(float, goal)))
        pts = self.smooth_path(pts, blocked_set)
        if pts and haversine_km(pts[0], start) < 0.05:
            pts = pts[1:]
        pts = simplify_path(pts)
        self._record_search(t0, expansions)
        return pts

    def _record_search(self, t0: float, expansions: int) -> None:
        self.stats["searches"] = int(self.stats["searches"]) + 1
        self.stats["expansions"] = int(self.stats["expansions"]) + expansions
        self.stats["last_search_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)

    # -- helpers used by the engine ---------------------------------------
    def route_to_water(
        self,
        start: Sequence[float],
        goal: Sequence[float],
        blocked: Optional[Iterable[int]] = None,
    ) -> Optional[List[List[float]]]:
        """A* plus a final hop onto the real destination (ports sit on land)."""
        pts = self.astar(start, goal, blocked)
        if pts is None:
            return None
        goal_ll = [float(goal[0]), float(goal[1])]
        if not pts or haversine_km(pts[-1], goal_ll) > 1.0:
            pts = list(pts) + [goal_ll]
        return simplify_path(pts)

    def describe(self) -> Dict[str, float]:
        total = self.nrows * self.ncols
        water = sum(self.navigable)
        return {
            "rows": self.nrows,
            "cols": self.ncols,
            "cell_deg": self.cell,
            "cells": total,
            "water_cells": water,
            "water_ratio": round(water / max(1, total), 4),
            "build_ms": round(self.build_ms, 2),
            "cell_size_km": round(self.cell * KM_PER_DEG_LAT, 2),
        }
