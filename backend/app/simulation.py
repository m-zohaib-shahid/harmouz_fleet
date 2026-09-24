"""The AegisFleet simulation engine.

One tick per second, in this exact order (see README "timing contract"):

1. advance every ship along its current path (speed / heading integration)
2. sample weather for the ship's grid cell (cache read, never blocks) and apply
   the +30% adverse-weather fuel-burn penalty
3. proximity detection - pairwise Haversine, alert < 2 km
4. geofence / boundary check - polygon point-in-polygon per active zone
5. fuel & status evaluation (insufficient_fuel -> out_of_fuel -> stranded, arrived)
6. hand the payload to the WebSocket fan-out (< 500 ms, measured and published)

Everything here is synchronous on purpose: the tick either completes atomically
inside the event loop or the machine is too slow for a 1 Hz contract. The only
off-thread work is A* route computation, which is dispatched with
``asyncio.to_thread`` and applied back on the event loop between ticks.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config
from .config import KM_PER_NM
from .alerts import AlertStore
from .auth import Principal
from .geo import (
    NM_PER_KM,
    advance_along_path,
    bearing_deg,
    distance_to_polygon_km,
    haversine_km,
    path_intersects_polygon,
    path_length_nm,
    point_in_polygon,
)
from .models import (
    Directive,
    DistressLog,
    DistressReport,
    FleetDataset,
    RestrictedZone,
    Severity,
    VesselSeed,
    VesselView,
)
from .nlp import DistressProcessor
from .pathfinding import NavGrid
from .weather import WeatherService

MOVING_STATUSES = {"normal", "rerouting", "distressed", "insufficient_fuel"}


@dataclass
class ShipState:
    """Live in-memory state of one vessel (seeded from fleet.json)."""

    shipId: str
    name: str
    position: List[float]
    speed: float
    heading: float
    destination: str
    destination_label: str
    destination_position: List[float]
    fuel: float
    fuel_capacity: float
    cargo: str
    status: str = "normal"
    base_speed: float = 0.0
    path: List[List[float]] = field(default_factory=list)
    path_index: int = 0
    flags: List[str] = field(default_factory=list)
    inside_zones: List[str] = field(default_factory=list)
    nearest_zone_km: Optional[float] = None
    weather: Dict[str, Any] = field(default_factory=dict)
    distance_remaining_nm: float = 0.0
    eta_hours: float = 0.0
    fuel_required: float = 0.0
    fuel_range_nm: float = 0.0
    distressed: bool = False
    directive_id: Optional[str] = None
    out_of_fuel_since: Optional[float] = None
    last_replan_at: float = -1e9
    updated_at: float = 0.0

    # -- derived helpers ---------------------------------------------------
    @property
    def effective_speed(self) -> float:
        if self.status in {"arrived", "stranded"}:
            return 0.0
        if self.status == "out_of_fuel":
            return config.OUT_OF_FUEL_DRIFT_KN
        if self.distressed:
            return self.speed * config.DISTRESS_SPEED_FACTOR
        return self.speed

    def burn_tons_per_hour(self) -> float:
        """Fuel burn at the speed the ship is actually making good."""
        speed = self.effective_speed if self.status not in {"arrived", "stranded"} else 0.0
        mult = float(self.weather.get("burn_multiplier", 1.0) or 1.0)
        return (
            config.FUEL_BASE_COEFF
            * max(speed, 0.0) ** 2
            * mult
            * config.FUEL_BURN_MULTIPLIER
        )

    def to_view(self) -> VesselView:
        return VesselView(
            shipId=self.shipId,
            name=self.name,
            position=[round(self.position[0], 5), round(self.position[1], 5)],
            speed=round(self.speed, 1),
            heading=round(self.heading, 1),
            destination=self.destination,
            destination_label=self.destination_label,
            destination_position=self.destination_position,
            fuel=round(self.fuel, 1),
            fuel_capacity=round(self.fuel_capacity, 1),
            cargo=self.cargo,
            status=self.status,
            flags=list(self.flags),
            distance_remaining_nm=round(self.distance_remaining_nm, 1),
            eta_hours=round(self.eta_hours, 2),
            fuel_required=round(self.fuel_required, 1),
            fuel_range_nm=round(self.fuel_range_nm, 1),
            inside_zones=list(self.inside_zones),
            nearest_zone_km=(
                round(self.nearest_zone_km, 2) if self.nearest_zone_km is not None else None
            ),
            weather=dict(self.weather),
            path=[[round(p[0], 5), round(p[1], 5)] for p in self.path[self.path_index :]],
            path_index=0,
            distressed=self.distressed,
            directive_id=self.directive_id,
            updated_at=self.updated_at,
        )


class SimulationEngine:
    """Owns all live state and drives the 1 Hz tick."""

    def __init__(
        self,
        dataset: FleetDataset,
        alerts: AlertStore,
        weather: WeatherService,
        nlp: DistressProcessor,
        ws_manager: Any = None,
    ) -> None:
        self.dataset = dataset
        self.scenario = dataset.scenario
        self.bbox = dataset.boundingBox.model_dump()
        self.navigable_polygon = [list(p) for p in dataset.navigableWater]
        self.ports = {p.id: p for p in dataset.ports}
        self.alerts = alerts
        self.weather = weather
        self.nlp = nlp
        self.ws = ws_manager

        self.ships: Dict[str, ShipState] = {}
        self.ship_order: List[str] = []
        for seed in dataset.fleet:
            self._add_seed(seed)

        self.grid = NavGrid(self.bbox, self.navigable_polygon)

        self.zones: Dict[str, RestrictedZone] = {}
        self.directives: Dict[str, Directive] = {}
        self.distress_logs: List[DistressLog] = []

        self.sim_seconds: float = 0.0
        self.tick_count: int = 0
        self.seq: int = 0
        self.started_at: float = time.time()
        self._ids = itertools.count(1)
        self._tasks: set = set()
        self._pending_replans: List[Dict[str, Any]] = []
        self._blocked_cache: Optional[set] = None
        self.metrics: Dict[str, Any] = {
            "tick_ms_last": 0.0,
            "tick_ms_avg": 0.0,
            "tick_ms_max": 0.0,
            "broadcast_ms_last": 0.0,
            "broadcast_ms_max": 0.0,
            "ticks": 0,
            "last_tick_started_at": 0.0,
            "client_count": 0,
            "messages_sent": 0,
            "route_plans": 0,
        }

    # -- construction helpers ---------------------------------------------
    def _add_seed(self, seed: VesselSeed) -> None:
        port = self.ports.get(seed.destination)
        dest_pos = list(port.position) if port else [26.5, 56.5]
        ship = ShipState(
            shipId=seed.shipId,
            name=seed.name,
            position=[float(seed.position[0]), float(seed.position[1])],
            speed=float(seed.speed),
            heading=float(seed.heading),
            destination=seed.destination,
            destination_label=port.name if port else seed.destination,
            destination_position=dest_pos,
            fuel=float(seed.fuel),
            fuel_capacity=float(seed.fuel),
            cargo=seed.cargo,
            status="normal",
            base_speed=float(seed.speed),
            weather=self.weather.sample(seed.position[0], seed.position[1]),
            updated_at=time.time(),
        )
        self.ships[ship.shipId] = ship
        self.ship_order.append(ship.shipId)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # -- clock -------------------------------------------------------------
    def sim_clock(self, offset_seconds: float = 0.0) -> str:
        total = int(self.sim_seconds + offset_seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def sim_block(self) -> Dict[str, Any]:
        return {
            "tick": self.tick_count,
            "seconds": round(self.sim_seconds, 1),
            "clock": self.sim_clock(),
            "time_scale": config.TIME_SCALE,
            "seconds_per_tick": config.SIM_SECONDS_PER_TICK,
            "tick_interval_s": config.TICK_SECONDS,
        }

    # -- views / payloads --------------------------------------------------
    def ship_views(self) -> List[Dict[str, Any]]:
        return [self.ships[sid].to_view().model_dump() for sid in self.ship_order]

    def snapshot(self, with_alerts: bool = True) -> Dict[str, Any]:
        return {
            "type": "snapshot",
            "seq": self.seq,
            "server_time": time.time(),
            "sim": self.sim_block(),
            "scenario": self.scenario.model_dump(),
            "boundingBox": self.bbox,
            "navigableWater": self.navigable_polygon,
            "ports": [p.model_dump() for p in self.dataset.ports],
            "ships": self.ship_views(),
            "zones": [z.model_dump() for z in self.zones.values()],
            "directives": [d.model_dump() for d in self.directives.values()],
            "alerts": [a.model_dump() for a in self.alerts.recent(120)] if with_alerts else [],
            "distress": [d.model_dump() for d in self.distress_logs[-25:]],
            "metrics": self.metrics_payload(),
            "grid": self.grid.describe(),
        }

    def tick_payload(self) -> Dict[str, Any]:
        return {
            "type": "tick",
            "seq": self.seq,
            "server_time": time.time(),
            "sim": self.sim_block(),
            "ships": self.ship_views(),
            "metrics": {
                "tick_ms": self.metrics["tick_ms_last"],
                "broadcast_ms": self.metrics["broadcast_ms_last"],
                "client_count": self.metrics["client_count"],
            },
        }

    def metrics_payload(self) -> Dict[str, Any]:
        return {
            **self.metrics,
            "uptime_s": round(time.time() - self.started_at, 1),
            "grid": self.grid.describe(),
            "astar": dict(self.grid.stats),
            "weather": self.weather.describe(),
            "nlp": self.nlp.describe(),
            "alerts": self.alerts.counts(),
            "zones": len(self.zones),
            "directives": {
                "total": len(self.directives),
                "pending": sum(1 for d in self.directives.values() if d.status == "pending"),
            },
            "config": {
                "tick_seconds": config.TICK_SECONDS,
                "time_scale": config.TIME_SCALE,
                "proximity_km": config.PROXIMITY_KM,
                "arrival_km": config.ARRIVAL_KM,
                "fuel_coeff": config.FUEL_BASE_COEFF,
                "weather_penalty_pct": int(config.WEATHER_BURN_PENALTY * 100),
                "grid_cell_deg": config.GRID_CELL_DEG,
            },
        }

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------
    def tick(self) -> List[Dict[str, Any]]:
        """Run one simulation step. Returns the alerts raised during the step."""
        t0 = time.perf_counter()
        fired: List[Dict[str, Any]] = []
        self.tick_count += 1
        self.seq += 1
        self.sim_seconds += config.SIM_SECONDS_PER_TICK
        clock = self.sim_clock()

        # 1) + 2) advance along the path, sampling weather for the fuel model
        self._advance_ships(fired, clock)
        # 3) proximity watch (pairwise Haversine, < 2 km)
        self._check_proximity(fired, clock)
        # 4) geofence + navigable-water boundary
        self._check_geofence(fired, clock)
        # 5) fuel and status evaluation
        for sid in self.ship_order:
            self._evaluate_ship(self.ships[sid], fired, clock)

        elapsed = (time.perf_counter() - t0) * 1000.0
        m = self.metrics
        m["tick_ms_last"] = round(elapsed, 2)
        m["tick_ms_max"] = max(float(m["tick_ms_max"]), round(elapsed, 2))
        m["ticks"] = int(m["ticks"]) + 1
        m["tick_ms_avg"] = round(
            (float(m["tick_ms_avg"]) * (int(m["ticks"]) - 1) + elapsed) / int(m["ticks"]), 2
        )
        m["last_tick_started_at"] = time.time()
        return fired

    def _fire(
        self,
        fired: Optional[List[Dict[str, Any]]],
        type_: str,
        severity: Severity,
        message: str,
        *,
        dedupe_key: Optional[str] = None,
        cooldown_s: float = 0.0,
        shipIds: Iterable[str] = (),
        zoneId: Optional[str] = None,
        **data: Any,
    ) -> Optional[Dict[str, Any]]:
        """Create an alert and stage it for broadcast. Cooldowns stop spam."""
        alert = self.alerts.fire(
            type_,
            severity,
            message,
            dedupe_key=dedupe_key,
            cooldown_s=cooldown_s,
            shipIds=shipIds,
            zoneId=zoneId,
            sim_time=self.sim_clock(),
            **data,
        )
        if alert is not None and fired is not None:
            fired.append(alert.model_dump())
        return alert.model_dump() if alert is not None else None

    # ------------------------------------------------------------------
    # Phase 1 + 2: movement and weather
    # ------------------------------------------------------------------
    def _advance_ships(self, fired: List[Dict[str, Any]], clock: str) -> None:
        for sid in self.ship_order:
            ship = self.ships[sid]
            # weather for this ship's grid cell (cache read; adverse => +30% burn)
            ship.weather = self.weather.sample(ship.position[0], ship.position[1])

            speed = ship.effective_speed
            if speed > 0.0 and ship.path and ship.path_index < len(ship.path):
                budget_km = speed * KM_PER_NM * config.SIM_HOURS_PER_TICK
                previous = [ship.position[0], ship.position[1]]
                new_pos, index, _leftover = advance_along_path(
                    ship.position, ship.path, budget_km, ship.path_index
                )
                if haversine_km(previous, new_pos) > 1e-6:
                    ship.heading = bearing_deg(previous, new_pos)
                ship.position = new_pos
                ship.path_index = index

            if ship.status in {"arrived", "stranded"}:
                ship.updated_at = time.time()
                continue

            # fuel burn for this step
            burn = ship.burn_tons_per_hour() * config.SIM_HOURS_PER_TICK
            if ship.status == "out_of_fuel":
                burn = 0.0
            ship.fuel = max(0.0, ship.fuel - burn)
            ship.updated_at = time.time()

            if ship.fuel <= 0.0 and ship.status != "out_of_fuel":
                ship.status = "out_of_fuel"
                ship.out_of_fuel_since = self.sim_seconds
                if "out_of_fuel" not in ship.flags:
                    ship.flags.append("out_of_fuel")
                self._fire(
                    fired,
                    "out_of_fuel",
                    "CRITICAL",
                    f"{ship.shipId} {ship.name} is out of fuel and drifting "
                    f"({ship.distance_remaining_nm or haversine_km(ship.position, ship.destination_position) * 0.5399568:.0f} Nm from {ship.destination_label})",
                    dedupe_key=f"out_of_fuel:{ship.shipId}",
                    shipIds=[ship.shipId],
                    position=[round(ship.position[0], 4), round(ship.position[1], 4)],
                )

    # ------------------------------------------------------------------
    # Phase 3: proximity detection (Haversine, pairwise, O(n^2) with n=15)
    # ------------------------------------------------------------------
    def _check_proximity(self, fired: List[Dict[str, Any]], clock: str) -> None:
        positions = [(sid, self.ships[sid].position) for sid in self.ship_order]
        for i in range(len(positions)):
            sid_a, pos_a = positions[i]
            for j in range(i + 1, len(positions)):
                sid_b, pos_b = positions[j]
                distance_km = haversine_km(pos_a, pos_b)
                if distance_km > config.PROXIMITY_KM:
                    continue
                ship_a, ship_b = self.ships[sid_a], self.ships[sid_b]
                if "proximity_alert" not in ship_a.flags:
                    ship_a.flags.append("proximity_alert")
                if "proximity_alert" not in ship_b.flags:
                    ship_b.flags.append("proximity_alert")
                severity: Severity = "HIGH" if distance_km < config.PROXIMITY_KM / 2 else "MEDIUM"
                self._fire(
                    fired,
                    "proximity",
                    severity,
                    f"{sid_a} {ship_a.name} and {sid_b} {ship_b.name} are "
                    f"{distance_km:.2f} km apart (limit {config.PROXIMITY_KM:g} km)",
                    dedupe_key=f"proximity:{min(sid_a, sid_b)}:{max(sid_a, sid_b)}",
                    cooldown_s=config.PROXIMITY_COOLDOWN_S,
                    shipIds=[sid_a, sid_b],
                    distance_km=round(distance_km, 3),
                    positions=[
                        [round(pos_a[0], 4), round(pos_a[1], 4)],
                        [round(pos_b[0], 4), round(pos_b[1], 4)],
                    ],
                )

    # ------------------------------------------------------------------
    # Phase 4: geofence + navigable-water boundary
    # ------------------------------------------------------------------
    def _check_geofence(self, fired: List[Dict[str, Any]], clock: str) -> None:
        active = [z for z in self.zones.values() if z.active]
        for sid in self.ship_order:
            ship = self.ships[sid]
            inside: List[str] = []
            nearest: Optional[float] = None
            for zone in active:
                if point_in_polygon(ship.position, zone.polygon):
                    inside.append(zone.id)
                    nearest = 0.0
                    if zone.id not in ship.inside_zones:
                        self._fire(
                            fired,
                            "geofence_breach",
                            "CRITICAL" if zone.severity == "CRITICAL" else "HIGH",
                            f"{sid} {ship.name} has entered restricted zone '{zone.name}' "
                            f"({zone.id}) carrying {ship.cargo}",
                            dedupe_key=f"geofence:{zone.id}:{sid}",
                            shipIds=[sid],
                            zoneId=zone.id,
                            zone_name=zone.name,
                            position=[round(ship.position[0], 4), round(ship.position[1], 4)],
                        )
                    continue
                distance = distance_to_polygon_km(ship.position, zone.polygon)
                if nearest is None or distance < nearest:
                    nearest = distance
            # zone exit -> resolve the state so the UI badge clears
            for zone_id in list(ship.inside_zones):
                if zone_id not in inside:
                    zone = self.zones.get(zone_id)
                    self._fire(
                        fired,
                        "geofence_exit",
                        "LOW",
                        f"{sid} {ship.name} has cleared restricted zone "
                        f"'{zone.name if zone else zone_id}'",
                        dedupe_key=f"geofence_exit:{zone_id}:{sid}",
                        shipIds=[sid],
                        zoneId=zone_id,
                    )
            if inside and "in_restricted_zone" not in ship.flags:
                ship.flags.append("in_restricted_zone")
            elif not inside and "in_restricted_zone" in ship.flags:
                ship.flags.remove("in_restricted_zone")
            ship.inside_zones = inside
            ship.nearest_zone_km = nearest
            # approach warning (advisory, one alert per zone+ship per 5 min)
            if not inside and nearest is not None and nearest <= config.ZONE_APPROACH_KM:
                closest = min(
                    (z for z in active if distance_to_polygon_km(ship.position, z.polygon) <= config.ZONE_APPROACH_KM),
                    key=lambda z: distance_to_polygon_km(ship.position, z.polygon),
                    default=None,
                )
                if closest is not None:
                    self._fire(
                        fired,
                        "zone_approach",
                        "MEDIUM",
                        f"{sid} {ship.name} will be {nearest:.1f} km from restricted zone "
                        f"'{closest.name}' on its current track",
                        dedupe_key=f"approach:{closest.id}:{sid}",
                        cooldown_s=300.0,
                        shipIds=[sid],
                        zoneId=closest.id,
                        distance_km=round(nearest, 2),
                    )

    # ------------------------------------------------------------------
    # Phase 5: fuel + status evaluation
    # ------------------------------------------------------------------
    def _evaluate_ship(
        self, ship: ShipState, fired: List[Dict[str, Any]], clock: str
    ) -> None:
        burn_tph = ship.burn_tons_per_hour()
        remaining = ship.path[ship.path_index :] if ship.path else []
        remaining_nm = (
            path_length_nm(remaining, start=ship.position)
            if remaining
            else haversine_km(ship.position, ship.destination_position) * NM_PER_KM
        )
        hours = remaining_nm / max(ship.effective_speed if ship.status not in {"arrived", "stranded"} else ship.speed, 0.1)
        ship.distance_remaining_nm = remaining_nm
        ship.eta_hours = hours
        ship.fuel_required = burn_tph * hours
        ship.fuel_range_nm = (
            (ship.fuel / burn_tph) * ship.speed if burn_tph > 0 else 0.0
        )

        # proximity is transient (raised in phase 3 of this very tick)
        flags: List[str] = [f for f in ship.flags if f == "proximity_alert"]
        if ship.weather.get("adverse"):
            flags.append("adverse_weather")
        distance_km = haversine_km(ship.position, ship.destination_position)

        # -- arrival -------------------------------------------------------
        if ship.status not in {"stranded"} and distance_km <= config.ARRIVAL_KM:
            if ship.status != "arrived":
                ship.status = "arrived"
                ship.flags = []
                self._fire(
                    fired,
                    "arrived",
                    "LOW",
                    f"{ship.shipId} {ship.name} has arrived at {ship.destination_label} "
                    f"({ship.destination}) with {ship.fuel:.0f} t fuel remaining",
                    dedupe_key=f"arrived:{ship.shipId}",
                    shipIds=[ship.shipId],
                    port=ship.destination,
                    fuel_remaining=round(ship.fuel, 1),
                )

        # -- insufficient fuel (flag, ship keeps moving) --------------------
        if (
            ship.status not in {"arrived", "stranded", "out_of_fuel"}
            and ship.fuel > 0
            and remaining_nm > 1.0
            and ship.fuel_required > ship.fuel
        ):
            flags.append("insufficient_fuel")
            self._fire(
                fired,
                "insufficient_fuel",
                "HIGH",
                f"{ship.shipId} {ship.name} cannot reach {ship.destination_label}: needs "
                f"{ship.fuel_required:.0f} t, has {ship.fuel:.0f} t "
                f"(short by {ship.fuel_required - ship.fuel:.0f} t, {remaining_nm:.0f} Nm to run)",
                dedupe_key=f"insufficient_fuel:{ship.shipId}",
                cooldown_s=600.0,
                shipIds=[ship.shipId],
                fuel=round(ship.fuel, 1),
                fuel_required=round(ship.fuel_required, 1),
                distance_remaining_nm=round(remaining_nm, 1),
                destination=ship.destination,
            )

        # -- out of fuel -> adrift -> stranded ------------------------------
        if ship.status == "out_of_fuel":
            flags.append("out_of_fuel")
            since = ship.out_of_fuel_since if ship.out_of_fuel_since is not None else self.sim_seconds
            adrift_hours = (self.sim_seconds - since) / 3600.0
            if adrift_hours >= config.STRANDED_AFTER_SIM_HOURS:
                ship.status = "stranded"
                ship.distressed = True
                self._fire(
                    fired,
                    "stranded",
                    "CRITICAL",
                    f"{ship.shipId} {ship.name} is stranded and adrift after "
                    f"{adrift_hours:.1f} h without fuel - tug/tow required "
                    f"({remaining_nm:.0f} Nm from {ship.destination_label})",
                    dedupe_key=f"stranded:{ship.shipId}",
                    shipIds=[ship.shipId],
                    distance_remaining_nm=round(remaining_nm, 1),
                    position=[round(ship.position[0], 4), round(ship.position[1], 4)],
                )

        if ship.status == "stranded":
            flags.append("stranded")
        if ship.status == "rerouting":
            flags.append("rerouting")
        if ship.status == "normal" and ship.speed < ship.base_speed - 0.01:
            flags.append("reduced_speed")
        if ship.distressed:
            flags.append("distress_active")
        if ship.inside_zones:
            flags.append("in_restricted_zone")
        if ship.inside_zones and ship.status in {"normal", "insufficient_fuel"}:
            flags.append("emergency_exit")

        # -- safety net: never let a ship sit still with no path ------------
        if (
            ship.status in {"normal", "rerouting"}
            and ship.fuel > 0
            and distance_km > config.ARRIVAL_KM
            and (not ship.path or ship.path_index >= len(ship.path))
            and (time.time() - ship.last_replan_at) > 30.0
        ):
            flags.append("replanning")
            self.schedule_route(
                ship.shipId,
                list(ship.destination_position),
                label=ship.destination_label,
                port=ship.destination,
                reason="path_exhausted",
            )

        ship.flags = list(dict.fromkeys(flags))

    # ------------------------------------------------------------------
    # Pathfinding / dynamic rerouting
    # ------------------------------------------------------------------
    def blocked_cells(self, extra_polygons: Sequence[Sequence[Sequence[float]]] = ()) -> set:
        """Union of the blocked cell sets of every active restricted zone."""
        if self._blocked_cache is None:
            cells: set = set()
            for zone in self.zones.values():
                if zone.active:
                    cells |= self.grid.zone_blocked_cells(zone.polygon)
            self._blocked_cache = cells
        if not extra_polygons:
            return set(self._blocked_cache)
        cells = set(self._blocked_cache)
        for polygon in extra_polygons:
            cells |= self.grid.zone_blocked_cells(polygon)
        return cells

    def _invalidate_blocked(self) -> None:
        self._blocked_cache = None

    def schedule_route(
        self,
        ship_id: str,
        goal: Sequence[float],
        *,
        label: Optional[str] = None,
        port: Optional[str] = None,
        reason: str = "",
        status: Optional[str] = "rerouting",
    ) -> None:
        """Queue an asynchronous A* re-plan (never blocks the tick loop)."""
        self._pending_replans.append(
            {
                "shipId": ship_id,
                "goal": [float(goal[0]), float(goal[1])],
                "label": label,
                "port": port,
                "reason": reason,
            }
        )
        ship = self.ships.get(ship_id)
        if ship is not None and status and ship.status not in {"arrived", "stranded"}:
            ship.status = status

    async def process_pending_routes(
        self,
        limit: Optional[int] = None,
        ship_id: Optional[str] = None,
        priority: bool = False,
    ) -> List[Dict[str, Any]]:
        """Drain the re-plan queue.

        * ``limit``: None -> the per-tick budget, ``<= 0`` -> drain everything.
        * ``ship_id``: only drain the queued plans for that ship.
        * ``priority``: move that ship's plans to the front instead of filtering.
        """
        if ship_id and priority:
            mine = [p for p in self._pending_replans if p["shipId"] == ship_id]
            others = [p for p in self._pending_replans if p["shipId"] != ship_id]
            self._pending_replans = mine + others
        budget = config.ROUTES_PER_TICK if limit is None else limit
        if ship_id:
            selected = [p for p in self._pending_replans if p["shipId"] == ship_id]
            if budget > 0:
                selected = selected[:budget]
            remaining = [p for p in self._pending_replans if p["shipId"] != ship_id]
            rest = [p for p in self._pending_replans if p["shipId"] == ship_id]
            self._pending_replans = remaining + rest[len(selected) :]
        elif budget <= 0:
            selected = list(self._pending_replans)
            self._pending_replans = []
        else:
            selected = self._pending_replans[:budget]
            self._pending_replans = self._pending_replans[len(selected) :]
        events: List[Dict[str, Any]] = []
        for item in selected:
            events.append(await self._plan_route(**item))
        return events

    async def _plan_route(
        self,
        shipId: str,
        goal: List[float],
        label: Optional[str] = None,
        port: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        ship = self.ships.get(shipId)
        if ship is None:
            return {"type": "route", "shipId": shipId, "ok": False, "error": "unknown ship"}
        blocked = self.blocked_cells()
        t0 = time.perf_counter()
        path = await asyncio.to_thread(
            self.grid.route_to_water, list(ship.position), list(goal), set(blocked)
        )
        self.metrics["route_plans"] = int(self.metrics["route_plans"]) + 1
        fired: List[Dict[str, Any]] = []
        emergency_exit = False

        if path is None and ship.inside_zones:
            # Enclosed by a restricted zone: try an emergency exit to open water.
            exit_cell = await asyncio.to_thread(
                self.grid.nearest_open_cell, ship.position[0], ship.position[1], blocked
            )
            if exit_cell is not None:
                escape = self.grid.center(*exit_cell)
                path = await asyncio.to_thread(
                    self.grid.route_to_water, list(ship.position), escape, set(blocked)
                )
                emergency_exit = path is not None
                if emergency_exit:
                    self._fire(
                        fired,
                        "emergency_exit",
                        "CRITICAL",
                        f"{ship.shipId} {ship.name} is enclosed by a restricted zone - "
                        "executing emergency exit routing to open water",
                        dedupe_key=f"emergency_exit:{ship.shipId}",
                        cooldown_s=120.0,
                        shipIds=[ship.shipId],
                        escape_point=[round(escape[0], 4), round(escape[1], 4)],
                    )

        plan_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if path is None:
            ship.status = "stranded"
            self._fire(
                fired,
                "stranded",
                "CRITICAL",
                f"No navigable route for {ship.shipId} {ship.name} to "
                f"{label or ship.destination_label} - every corridor is blocked "
                "(status set to stranded)",
                dedupe_key=f"stranded:{ship.shipId}",
                cooldown_s=300.0,
                shipIds=[ship.shipId],
                reason=reason,
            )
            return {
                "type": "route",
                "shipId": ship.shipId,
                "ok": False,
                "reason": reason,
                "plan_ms": plan_ms,
                "status": ship.status,
                "alerts": fired,
                "sim": self.sim_block(),
            }

        ship.path = [list(p) for p in path]
        ship.path_index = 0
        ship.last_replan_at = time.time()
        if label:
            ship.destination_label = label
        if port:
            ship.destination = port
        ship.destination_position = [float(goal[0]), float(goal[1])]
        # 'rerouting' is the transient re-plan state; clear it once locked in.
        if ship.status == "rerouting":
            if ship.distressed:
                ship.status = "distressed"
            elif ship.fuel <= 0:
                ship.status = "out_of_fuel"
            else:
                ship.status = "normal"

        return {
            "type": "route",
            "shipId": ship.shipId,
            "ok": True,
            "reason": reason,
            "plan_ms": plan_ms,
            "legs": len(ship.path),
            "distance_nm": round(path_length_nm(ship.path, start=ship.position), 1),
            "emergency_exit": emergency_exit,
            "status": ship.status,
            "destination": ship.destination,
            "destination_label": ship.destination_label,
            "path": [[round(p[0], 5), round(p[1], 5)] for p in ship.path],
            "alerts": fired,
            "sim": self.sim_block(),
        }

    # ------------------------------------------------------------------
    # Restricted zones (dynamic geofences)
    # ------------------------------------------------------------------
    def add_zone(
        self,
        *,
        name: str,
        polygon: List[List[float]],
        severity: str = "HIGH",
        note: str = "",
        principal: Optional[Principal] = None,
        active: bool = True,
    ) -> Tuple[RestrictedZone, List[Dict[str, Any]]]:
        """Create a zone and evaluate its impact **in the same request**.

        Ships already inside breach instantly (< 1 s, no tick needed); ships whose
        remaining track intersects the zone are switched to `rerouting` and a new
        A* path is queued around every active zone.
        """
        zone = RestrictedZone(
            id=self.next_id("ZONE"),
            name=name,
            polygon=[[float(p[0]), float(p[1])] for p in polygon],
            severity=severity,  # type: ignore[arg-type]
            active=active,
            note=note,
            created_by=principal.label if principal else "command",
            created_sim_time=self.sim_clock(),
        )
        fired: List[Dict[str, Any]] = []
        affected: List[str] = []

        for sid in self.ship_order:
            ship = self.ships[sid]
            if point_in_polygon(ship.position, zone.polygon):
                affected.append(sid)
                if zone.id not in ship.inside_zones:
                    ship.inside_zones.append(zone.id)
                if "in_restricted_zone" not in ship.flags:
                    ship.flags.append("in_restricted_zone")
                self._fire(
                    fired,
                    "geofence_breach",
                    "CRITICAL" if zone.severity == "CRITICAL" else "HIGH",
                    f"{sid} {ship.name} is INSIDE restricted zone '{zone.name}' ({zone.id}) "
                    f"carrying {ship.cargo}",
                    dedupe_key=f"geofence:{zone.id}:{sid}",
                    shipIds=[sid],
                    zoneId=zone.id,
                    zone_name=zone.name,
                    position=[round(ship.position[0], 4), round(ship.position[1], 4)],
                )
                self.schedule_route(
                    sid,
                    ship.destination_position,
                    label=ship.destination_label,
                    port=ship.destination,
                    reason=f"zone_created:{zone.id}",
                )
                continue

            remaining_track = [list(ship.position)] + [
                list(p) for p in ship.path[ship.path_index :]
            ]
            if len(remaining_track) >= 2 and path_intersects_polygon(remaining_track, zone.polygon):
                affected.append(sid)
                self.schedule_route(
                    sid,
                    ship.destination_position,
                    label=ship.destination_label,
                    port=ship.destination,
                    reason=f"zone_created:{zone.id}",
                )
                self._fire(
                    fired,
                    "reroute_triggered",
                    "HIGH" if zone.severity in {"CRITICAL", "HIGH"} else "MEDIUM",
                    f"{sid} {ship.name} planned track crosses new restricted zone "
                    f"'{zone.name}' - recalculating A* route",
                    dedupe_key=f"reroute:{zone.id}:{sid}",
                    shipIds=[sid],
                    zoneId=zone.id,
                    zone_name=zone.name,
                )

        zone.affected_ships = affected
        self.zones[zone.id] = zone
        self._invalidate_blocked()
        return zone, fired

    def remove_zone(self, zone_id: str) -> Tuple[Optional[RestrictedZone], List[Dict[str, Any]]]:
        zone = self.zones.pop(zone_id, None)
        fired: List[Dict[str, Any]] = []
        if zone is None:
            return None, fired
        for sid in self.ship_order:
            ship = self.ships[sid]
            if zone_id in ship.inside_zones:
                ship.inside_zones.remove(zone_id)
            if zone_id in zone.affected_ships:
                # zone lifted: re-plan the direct route to the original destination
                self.schedule_route(
                    sid,
                    ship.destination_position,
                    label=ship.destination_label,
                    port=ship.destination,
                    reason=f"zone_removed:{zone_id}",
                )
        if zone_id in self.zones:
            self.zones.pop(zone_id, None)
        self._invalidate_blocked()
        self._fire(
            fired,
            "zone_lifted",
            "LOW",
            f"Restricted zone '{zone.name}' ({zone.id}) has been lifted",
            dedupe_key=f"zone_lifted:{zone_id}",
            zoneId=zone_id,
        )
        return zone, fired

    def set_zone_active(
        self, zone_id: str, active: bool
    ) -> Tuple[Optional[RestrictedZone], List[Dict[str, Any]]]:
        zone = self.zones.get(zone_id)
        if zone is None:
            return None, []
        zone.active = active
        self._invalidate_blocked()
        fired: List[Dict[str, Any]] = []
        if active:
            for sid in self.ship_order:
                ship = self.ships[sid]
                if point_in_polygon(ship.position, zone.polygon) or path_intersects_polygon(
                    [list(ship.position)] + [list(p) for p in ship.path[ship.path_index :]],
                    zone.polygon,
                ):
                    self.schedule_route(
                        sid,
                        ship.destination_position,
                        label=ship.destination_label,
                        port=ship.destination,
                        reason=f"zone_activated:{zone_id}",
                    )
        return zone, fired

    # ------------------------------------------------------------------
    # Directives (Command issues -> Captain accepts/rejects)
    # ------------------------------------------------------------------
    def resolve_destination(self, destination: Any) -> Tuple[List[float], Optional[str], str]:
        """Accept either a port id ('DXB-1') or a raw [lat, lng] waypoint."""
        if isinstance(destination, str):
            port = self.ports.get(destination)
            if port is None:
                raise KeyError(destination)
            return [float(port.position[0]), float(port.position[1])], port.id, port.name
        return (
            [float(destination[0]), float(destination[1])],
            None,
            f"{float(destination[0]):.3f}N {float(destination[1]):.3f}E",
        )

    def create_directive(
        self,
        *,
        ship_id: str,
        destination: Any,
        kind: str = "course",
        note: str = "",
        principal: Optional[Principal] = None,
    ) -> Directive:
        goal, port_id, label = self.resolve_destination(destination)
        directive = Directive(
            id=self.next_id("DIR"),
            shipId=ship_id,
            kind=kind,  # type: ignore[arg-type]
            destination=goal,
            destination_label=label,
            destination_port=port_id,
            note=note,
            issued_by=principal.label if principal else "command",
            sim_time=self.sim_clock(),
        )
        self.directives[directive.id] = directive
        ship = self.ships.get(ship_id)
        if ship is not None:
            ship.directive_id = directive.id
        return directive

    async def respond_directive(
        self,
        directive_id: str,
        *,
        accept: bool,
        note: str = "",
        principal: Optional[Principal] = None,
    ) -> Tuple[Optional[Directive], List[Dict[str, Any]]]:
        directive = self.directives.get(directive_id)
        fired: List[Dict[str, Any]] = []
        if directive is None:
            return None, fired
        directive.status = "accepted" if accept else "rejected"
        directive.resolved_at = time.time()
        directive.response_note = note
        ship = self.ships.get(directive.shipId)
        if ship is not None:
            ship.directive_id = None
            if accept:
                if directive.kind == "hold":
                    ship.speed = 1.0  # loiter in place
                elif directive.kind == "resume":
                    ship.speed = ship.base_speed or ship.speed
                    if directive.destination:
                        self.schedule_route(
                            ship.shipId,
                            directive.destination,
                            label=directive.destination_label,
                            port=directive.destination_port,
                            reason=f"directive:{directive.id}",
                        )
                elif directive.destination:
                    self.schedule_route(
                        ship.shipId,
                        directive.destination,
                        label=directive.destination_label,
                        port=directive.destination_port,
                        reason=f"directive:{directive.id}",
                    )
                if directive.kind == "assist":
                    ship.flags.append("assist_tasking")
            elif ship.status == "rerouting":
                ship.status = "distressed" if ship.distressed else "normal"
        self._fire(
            fired,
            "directive_accepted" if accept else "directive_rejected",
            "MEDIUM" if accept else "LOW",
            f"{directive.shipId} {'ACCEPTED' if accept else 'REJECTED'} directive "
            f"{directive.id} ({directive.kind}) -> {directive.destination_label}"
            + (f": {note}" if note else ""),
            dedupe_key=f"directive:{directive.id}",
            shipIds=[directive.shipId],
            directiveId=directive.id,
            kind=directive.kind,
            accepted=accept,
        )
        return directive, fired

    # ------------------------------------------------------------------
    # AI distress pipeline
    # ------------------------------------------------------------------
    def nearest_assist(self, ship_id: str) -> Optional[Dict[str, Any]]:
        """Closest other vessel that could render assistance."""
        ship = self.ships.get(ship_id)
        if ship is None:
            return None
        best: Optional[Dict[str, Any]] = None
        for other in self.ships.values():
            if other.shipId == ship_id or other.status == "arrived":
                continue
            distance_km = haversine_km(ship.position, other.position)
            speed_kn = max(other.effective_speed, 1.0)
            entry = {
                "shipId": other.shipId,
                "name": other.name,
                "position": [round(other.position[0], 4), round(other.position[1], 4)],
                "distance_km": round(distance_km, 1),
                "eta_hours": round(distance_km / (speed_kn * KM_PER_NM), 2),
                "speed_kn": round(speed_kn, 1),
                "status": other.status,
                "cargo": other.cargo,
                "fuel": round(other.fuel, 1),
            }
            if best is None or distance_km < best["distance_km"]:
                best = entry
        return best

    async def handle_distress(
        self, ship_id: str, text: str, source: str = "text"
    ) -> Tuple[DistressLog, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Run the LLM pipeline, raise alerts and suggest tasking for Command."""
        ship = self.ships[ship_id]
        assist = self.nearest_assist(ship_id)
        report: DistressReport = await self.nlp.parse(
            text, ship_id=ship_id, nearest_assist=assist
        )
        log = DistressLog(
            id=self.next_id("DSTR"),
            shipId=ship_id,
            raw_text=text,
            report=report,
            sim_time=self.sim_clock(),
        )
        self.distress_logs.append(log)
        if len(self.distress_logs) > config.DISTRESS_LOG_SIZE:
            self.distress_logs = self.distress_logs[-config.DISTRESS_LOG_SIZE :]

        fired: List[Dict[str, Any]] = []
        if report.severity in {"CRITICAL", "HIGH"}:
            ship.distressed = True
            if ship.status in {"normal", "rerouting", "insufficient_fuel"}:
                ship.status = "distressed"
        self._fire(
            fired,
            "distress",
            report.severity,
            f"{ship_id} {ship.name} [{(source or 'text').upper()}] "
            f"{report.issue_summary} - {report.recommended_action}",
            dedupe_key=f"distress:{log.id}",
            shipIds=[ship_id],
            report=report.model_dump(),
            raw_text=text,
            log_id=log.id,
            nearest_assist=assist,
        )
        if report.severity == "CRITICAL" and assist is not None:
            self._fire(
                fired,
                "assist_required",
                "HIGH",
                f"Nearest asset for {ship_id} is {assist['shipId']} {assist['name']} "
                f"({assist['distance_km']} km, ETA {assist['eta_hours']} h) - tasking recommended",
                dedupe_key=f"assist:{log.id}",
                shipIds=[ship_id, str(assist["shipId"])],
                distress_ship=ship_id,
                assist_ship=assist["shipId"],
            )
        suggestion: Optional[Dict[str, Any]] = None
        if assist is not None and report.severity in {"CRITICAL", "HIGH"}:
            suggestion = {
                "shipId": assist["shipId"],
                "destination": ship.position,
                "kind": "assist",
                "note": f"Assist {ship_id} ({report.issue_summary}) - ETA {assist['eta_hours']} h",
                "destination_label": f"Distress position of {ship_id}",
            }
        return log, fired, suggestion

    # ------------------------------------------------------------------
    # Weather warm-up provider + lifecycle
    # ------------------------------------------------------------------
    def weather_points(self) -> List[List[float]]:
        """Grid cells to keep warm: every ship's position + next waypoint."""
        points: List[List[float]] = []
        for sid in self.ship_order:
            ship = self.ships[sid]
            points.append([ship.position[0], ship.position[1]])
            if ship.path_index < len(ship.path):
                points.append(list(ship.path[ship.path_index]))
        return points

    def boot(self) -> Dict[str, Any]:
        """Plan every initial route and run a first status pass (pre-server)."""
        t0 = time.perf_counter()
        fired: List[Dict[str, Any]] = []
        for sid in self.ship_order:
            ship = self.ships[sid]
            path = self.grid.route_to_water(
                list(ship.position), list(ship.destination_position), None
            )
            if path:
                ship.path = [list(p) for p in path]
                ship.path_index = 0
            else:
                ship.status = "stranded"
                self._fire(
                    fired,
                    "stranded",
                    "CRITICAL",
                    f"{ship.shipId} {ship.name} has no navigable route to "
                    f"{ship.destination_label}",
                    dedupe_key=f"stranded:{ship.shipId}",
                    shipIds=[ship.shipId],
                )
        for sid in self.ship_order:
            self._evaluate_ship(self.ships[sid], fired, self.sim_clock())
        return {
            "ships": len(self.ship_order),
            "ports": len(self.ports),
            "route_plan_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "alerts": fired,
            "grid": self.grid.describe(),
        }

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if self.ws is not None:
            await self.ws.broadcast(message)

    async def broadcast_events(self, events: List[Dict[str, Any]]) -> None:
        """Push A* route events + any alerts they raised."""
        if self.ws is None:
            return
        for event in events:
            for alert in event.get("alerts", []) or []:
                await self.ws.broadcast({"type": "alert", "alert": alert, "sim": self.sim_block()})
            await self.ws.broadcast({k: v for k, v in event.items() if k != "alerts"})

    async def broadcast_tick(
        self, fired: List[Dict[str, Any]], events: List[Dict[str, Any]]
    ) -> None:
        """Fan out alerts -> route events -> state, measuring the state push."""
        if self.ws is None:
            return
        for alert in fired:
            await self.ws.broadcast({"type": "alert", "alert": alert, "sim": self.sim_block()})
        await self.broadcast_events(events)
        t0 = time.perf_counter()
        await self.ws.broadcast(self.tick_payload())
        elapsed = (time.perf_counter() - t0) * 1000.0
        self.metrics["broadcast_ms_last"] = round(elapsed, 2)
        self.metrics["broadcast_ms_max"] = max(
            float(self.metrics["broadcast_ms_max"]), round(elapsed, 2)
        )

    async def run(self) -> None:
        """The 1 Hz loop. Booking-critical: it NEVER awaits A* work.

        Route planning runs in a sibling task (`planner_loop`) so an expensive
        search can never stretch the tick period beyond the 1 s contract.
        """
        interval = config.TICK_SECONDS
        next_at = time.perf_counter()
        while True:
            try:
                fired = self.tick()
                await self.broadcast_tick(fired, [])
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self.alerts.fire(
                    "engine_error",
                    "HIGH",
                    f"Simulation tick error: {type(exc).__name__}: {exc}",
                    dedupe_key="engine_error",
                    cooldown_s=30.0,
                )
            next_at += interval
            delay = next_at - time.perf_counter()
            if delay < 0:  # we fell behind: re-anchor instead of burst-catching up
                next_at = time.perf_counter()
                delay = 0.0
            await asyncio.sleep(delay)

    async def planner_loop(self) -> None:
        """Background A* worker: drains the re-plan queue a few routes at a time."""
        while True:
            try:
                if self._pending_replans:
                    events = await self.process_pending_routes()
                    if events:
                        await self.broadcast_events(events)
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception as exc:  # noqa: BLE001
                self.alerts.fire(
                    "planner_error",
                    "MEDIUM",
                    f"Route planner error: {type(exc).__name__}: {exc}",
                    dedupe_key="planner_error",
                    cooldown_s=60.0,
                )
                await asyncio.sleep(0.5)
