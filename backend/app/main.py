"""AegisFleet API - FastAPI application.

Run locally:  uvicorn app.main:app --host 0.0.0.0 --port 8000
In Docker:    the nginx container serves the dashboard and proxies /api + /ws.

The app also serves the static dashboard itself (StaticFiles mount) whenever the
`frontend/` folder is present, so a single `uvicorn` process is a complete stack
for local development.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config
from .alerts import AlertStore
from .auth import AuthService, Principal
from .models import (
    AuthInfo,
    Directive,
    DirectiveCreateRequest,
    DirectiveResponseRequest,
    DistressRequest,
    FleetDataset,
    LoginRequest,
    RestrictedZone,
    ZoneCreateRequest,
)
from .geo import haversine_km
from .nlp import DistressProcessor, render_report
from .simulation import SimulationEngine
from .weather import WeatherService
from .ws import ConnectionManager


def load_dataset() -> FleetDataset:
    with open(config.FLEET_FILE, "r", encoding="utf-8") as fh:
        return FleetDataset.model_validate(json.load(fh))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot the scenario: dataset -> nav grid -> initial A* routes -> loops."""
    dataset = load_dataset()
    alerts = AlertStore()
    weather = WeatherService()
    nlp = DistressProcessor()
    ws = ConnectionManager()
    engine = SimulationEngine(dataset, alerts, weather, nlp, ws)
    auth = AuthService(engine.ship_order)

    app.state.dataset = dataset
    app.state.alerts = alerts
    app.state.weather = weather
    app.state.nlp = nlp
    app.state.ws = ws
    app.state.engine = engine
    app.state.auth = auth
    app.state.boot_summary = engine.boot()
    app.state.started_at = time.time()

    tick_task = asyncio.create_task(engine.run(), name="aegis-tick-loop")
    planner_task = asyncio.create_task(engine.planner_loop(), name="aegis-planner-loop")
    weather_task = asyncio.create_task(
        weather.run_forever(engine.weather_points), name="aegis-weather-refresh"
    )
    app.state.tasks = [tick_task, planner_task, weather_task]
    try:
        yield
    finally:
        for task in app.state.tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await weather.aclose()
        await nlp.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AegisFleet - Real-Time Maritime Crisis Operations Platform",
        version="1.0.0",
        description=(
            "Live 1 Hz simulation of 15 vessels in the Strait of Hormuz: dynamic A* "
            "rerouting, geofencing, proximity watch, Open-Meteo weather penalties and "
            "Groq Llama-3 distress parsing."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if config.CORS_ORIGINS == "*" else config.CORS_ORIGINS.split(","),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- dependencies ------------------------------------------------------
    def get_principal(request: Request) -> Principal:
        return request.app.state.auth.from_request(request)

    def command_principal(request: Request) -> Principal:
        auth: AuthService = request.app.state.auth
        principal = auth.from_request(request, required=True)
        return auth.require(principal, "zone:create")

    def get_engine(request: Request) -> SimulationEngine:
        return request.app.state.engine

    # -- health / meta -----------------------------------------------------
    @app.get("/health", tags=["meta"])
    async def health(request: Request) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        ws: ConnectionManager = request.app.state.ws
        return {
            "status": "ok",
            "scenario": engine.scenario.name,
            "ships": len(engine.ship_order),
            "tick": engine.tick_count,
            "sim_clock": engine.sim_clock(),
            "clients": ws.client_count,
            "tick_ms": engine.metrics["tick_ms_last"],
            "broadcast_ms": engine.metrics["broadcast_ms_last"],
            "boot": request.app.state.boot_summary,
        }

    @app.get("/api/config", tags=["meta"])
    async def api_config(request: Request) -> Dict[str, Any]:
        return {
            "tick_seconds": config.TICK_SECONDS,
            "time_scale": config.TIME_SCALE,
            "proximity_km": config.PROXIMITY_KM,
            "arrival_km": config.ARRIVAL_KM,
            "zone_safety_km": config.ZONE_SAFETY_KM,
            "fuel_base_coeff": config.FUEL_BASE_COEFF,
            "weather_burn_penalty": config.WEATHER_BURN_PENALTY,
            "grid_cell_deg": config.GRID_CELL_DEG,
            "offline_mode": config.OFFLINE,
            "ai": {
                "provider": "Groq (Llama-3, free tier)",
                "model": config.GROQ_MODEL,
                "key_present": bool(config.GROQ_API_KEY),
            },
            "demo_strait_zone": config.DEMO_STRAIT_ZONE,
            # Fleet metadata: without this the config endpoint could not describe
            # a single vessel or destination port.
            "vessels": len(request.app.state.engine.ships),
            "ports": [
                {"id": p.id, "name": p.name, "position": list(p.position)}
                for p in request.app.state.engine.dataset.ports
            ],
            "boot": request.app.state.boot_summary,
        }

    # -- auth --------------------------------------------------------------
    @app.get("/api/auth/tokens", tags=["auth"])
    async def api_tokens(request: Request) -> Dict[str, Any]:
        auth: AuthService = request.app.state.auth
        return {
            "note": (
                "Hackathon demo tokens. Any 'captain-<shipId>' token (e.g. captain-mv7) "
                "is accepted for a ship present in fleet.json."
            ),
            "tokens": auth.demo_tokens(),
        }

    @app.post("/api/auth/login", response_model=AuthInfo, tags=["auth"])
    async def api_login(payload: LoginRequest, request: Request) -> AuthInfo:
        auth: AuthService = request.app.state.auth
        principal = auth.resolve_token_for_api(payload.token)
        if principal is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return AuthInfo(**principal.to_dict())

    # -- state -------------------------------------------------------------
    @app.get("/api/state", tags=["state"])
    async def api_state(request: Request) -> Dict[str, Any]:
        return request.app.state.engine.snapshot()

    @app.get("/api/metrics", tags=["state"])
    async def api_metrics(request: Request) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        ws: ConnectionManager = request.app.state.ws
        return {**engine.metrics_payload(), "ws": ws.describe()}

    @app.get("/api/dataset", tags=["state"])
    async def api_dataset(request: Request) -> Dict[str, Any]:
        dataset: FleetDataset = request.app.state.dataset
        return dataset.model_dump()

    @app.get("/api/alerts", tags=["state"])
    async def api_alerts(
        request: Request,
        limit: int = Query(100, ge=1, le=400),
        min_severity: Optional[str] = Query(None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$"),
    ) -> Dict[str, Any]:
        alerts: AlertStore = request.app.state.alerts
        recent = alerts.recent(limit, min_severity)  # type: ignore[arg-type]
        return {"counts": alerts.counts(), "alerts": [a.model_dump() for a in recent]}

    @app.get("/api/ships/{ship_id}", tags=["state"])
    async def api_ship(ship_id: str, request: Request) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        ship = engine.ships.get(ship_id.upper())
        if ship is None:
            raise HTTPException(status_code=404, detail=f"Unknown ship {ship_id}")
        return ship.to_view().model_dump()

    @app.get("/api/weather", tags=["state"])
    async def api_weather(request: Request, lat: float, lng: float) -> Dict[str, Any]:
        weather: WeatherService = request.app.state.weather
        return {"sample": weather.sample(lat, lng), "service": weather.describe()}

    # -- broadcast helper for request-triggered changes --------------------
    async def publish(request: Request, alerts: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
        """Push request-triggered alerts/events immediately (< 1 s contract)."""
        ws: ConnectionManager = request.app.state.ws
        engine: SimulationEngine = request.app.state.engine
        for alert in alerts:
            await ws.broadcast({"type": "alert", "alert": alert, "sim": engine.sim_block()})
        for event in events:
            for alert in event.get("alerts", []) or []:
                await ws.broadcast({"type": "alert", "alert": alert, "sim": engine.sim_block()})
            await ws.broadcast({k: v for k, v in event.items() if k != "alerts"})

    # -- restricted zones --------------------------------------------------
    @app.get("/api/zones", tags=["zones"])
    async def api_zones(request: Request) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        return {
            "zones": [z.model_dump() for z in engine.zones.values()],
            "blocked_cells": len(engine.blocked_cells()),
        }

    @app.post("/api/zones", tags=["zones"])
    async def api_create_zone(
        payload: ZoneCreateRequest,
        request: Request,
        principal: Principal = Depends(command_principal),
        await_routes: bool = Query(
            False, description="Block until A* re-plans finish (handy for curl/judging)"
        ),
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        t0 = time.perf_counter()
        zone, fired = engine.add_zone(
            name=payload.name,
            polygon=payload.polygon,
            severity=payload.severity,
            note=payload.note,
            principal=principal,
            active=payload.active,
        )
        # Geofence breach alerts are computed inside add_zone and pushed here,
        # so the < 1 s contract holds even when A* re-plans are still queued.
        detected_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        events = await engine.process_pending_routes(limit=-1) if await_routes else []
        await publish(request, fired, events)
        await request.app.state.ws.broadcast(
            {
                "type": "zone",
                "action": "created",
                "zone": zone.model_dump(),
                "sim": engine.sim_block(),
            }
        )
        return {
            "zone": zone.model_dump(),
            "affected_ships": zone.affected_ships,
            "alerts": fired,
            "reroutes": events,
            "queued_reroutes": len(engine._pending_replans),
            "detection_ms": detected_ms,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    @app.delete("/api/zones/{zone_id}", tags=["zones"])
    async def api_delete_zone(
        zone_id: str,
        request: Request,
        principal: Principal = Depends(command_principal),
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        zone, fired = engine.remove_zone(zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone {zone_id}")
        events = await engine.process_pending_routes()
        await publish(request, fired, events)
        await request.app.state.ws.broadcast(
            {"type": "zone", "action": "removed", "zone": zone.model_dump(), "sim": engine.sim_block()}
        )
        return {"removed": zone.model_dump(), "alerts": fired, "reroutes": events}

    @app.patch("/api/zones/{zone_id}", tags=["zones"])
    async def api_patch_zone(
        zone_id: str,
        request: Request,
        active: bool = Query(...),
        principal: Principal = Depends(command_principal),
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        zone, fired = engine.set_zone_active(zone_id, active)
        if zone is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone {zone_id}")
        events = await engine.process_pending_routes()
        await publish(request, fired, events)
        await request.app.state.ws.broadcast(
            {"type": "zone", "action": "updated", "zone": zone.model_dump(), "sim": engine.sim_block()}
        )
        return {"zone": zone.model_dump(), "alerts": fired, "reroutes": events}

    @app.post("/api/zones/demo/strait", tags=["zones"])
    async def api_demo_zone(
        request: Request,
        principal: Principal = Depends(command_principal),
        await_routes: bool = Query(False),
    ) -> Dict[str, Any]:
        """One-click scripted closure of the Strait of Hormuz narrows."""
        engine: SimulationEngine = request.app.state.engine
        demo = config.DEMO_STRAIT_ZONE
        zone, fired = engine.add_zone(
            name=str(demo["name"]),
            polygon=[list(p) for p in demo["polygon"]],  # type: ignore[arg-type]
            severity=str(demo.get("severity", "CRITICAL")),  # type: ignore[arg-type]
            note="Scripted demo closure of the Hormuz narrows",
            principal=principal,
        )
        events = await engine.process_pending_routes(limit=-1) if await_routes else []
        await publish(request, fired, events)
        await request.app.state.ws.broadcast(
            {"type": "zone", "action": "created", "zone": zone.model_dump(), "sim": engine.sim_block()}
        )
        return {
            "zone": zone.model_dump(),
            "affected_ships": zone.affected_ships,
            "alerts": fired,
            "reroutes": events,
        }

    # -- directives (Command -> Captain) -----------------------------------
    @app.get("/api/directives", tags=["directives"])
    async def api_directives(request: Request) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        items = sorted(engine.directives.values(), key=lambda d: d.issued_at, reverse=True)
        return {"directives": [d.model_dump() for d in items]}

    @app.post("/api/directives", tags=["directives"])
    async def api_create_directive(
        payload: DirectiveCreateRequest,
        request: Request,
        principal: Principal = Depends(command_principal),
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        if payload.shipId.upper() not in engine.ships:
            raise HTTPException(status_code=404, detail=f"Unknown ship {payload.shipId}")
        try:
            directive = engine.create_directive(
                ship_id=payload.shipId.upper(),
                destination=payload.destination,
                kind=payload.kind,
                note=payload.note,
                principal=principal,
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"Unknown port {exc}") from exc
        await request.app.state.ws.broadcast(
            {
                "type": "directive",
                "action": "created",
                "directive": directive.model_dump(),
                "sim": engine.sim_block(),
            }
        )
        return {"directive": directive.model_dump()}

    @app.post("/api/directives/{directive_id}/respond", tags=["directives"])
    async def api_respond_directive(
        directive_id: str, payload: DirectiveResponseRequest, request: Request
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        principal = request.app.state.auth.from_request(request, required=True)
        directive = engine.directives.get(directive_id)
        if directive is None:
            raise HTTPException(status_code=404, detail=f"Unknown directive {directive_id}")
        if not principal.can_drive(directive.shipId):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' cannot respond for {directive.shipId}",
            )
        updated, fired = await engine.respond_directive(
            directive_id, accept=payload.accept, note=payload.note, principal=principal
        )
        # Deterministic for the caller: run this ship's queued re-plan first.
        events = await engine.process_pending_routes(limit=-1, ship_id=directive.shipId)
        await publish(request, fired, events)
        await request.app.state.ws.broadcast(
            {
                "type": "directive",
                "action": "responded",
                "directive": updated.model_dump() if updated else None,
                "sim": engine.sim_block(),
            }
        )
        return {
            "directive": updated.model_dump() if updated else None,
            "alerts": fired,
            "reroutes": events,
        }

    # -- ship-level actions -------------------------------------------------
    @app.get("/api/ships/{ship_id}/options", tags=["routing"])
    async def api_ship_options(ship_id: str, request: Request) -> Dict[str, Any]:
        """Fuel-aware port options: 'which ports can this ship still reach?'"""
        engine: SimulationEngine = request.app.state.engine
        ship = engine.ships.get(ship_id.upper())
        if ship is None:
            raise HTTPException(status_code=404, detail=f"Unknown ship {ship_id}")
        burn_tph = ship.burn_tons_per_hour()
        options: List[Dict[str, Any]] = []
        for port in engine.ports.values():
            straight_nm = round(haversine_km(ship.position, port.position) * config.NM_PER_KM, 1)
            hours = straight_nm / max(ship.speed, 0.1)
            required = round(burn_tph * hours, 1)
            options.append(
                {
                    "port": port.id,
                    "name": port.name,
                    "position": port.position,
                    "distance_nm": straight_nm,
                    "eta_hours": round(hours, 1),
                    "fuel_required_t": required,
                    "fuel_available_t": round(ship.fuel, 1),
                    "feasible": required <= ship.fuel,
                }
            )
        options.sort(key=lambda o: o["distance_nm"])
        return {
            "ship": ship.to_view().model_dump(),
            "fuel_burn_t_per_h": round(burn_tph, 2),
            "weather": ship.weather,
            "options": options,
            "nearest_feasible": next((o for o in options if o["feasible"]), None),
        }

    @app.post("/api/ships/{ship_id}/reroute", tags=["routing"])
    async def api_reroute(ship_id: str, request: Request) -> Dict[str, Any]:
        """Force a re-plan. Command may drive any ship; a captain only their own."""
        engine: SimulationEngine = request.app.state.engine
        principal = request.app.state.auth.from_request(request, required=True)
        ship = engine.ships.get(ship_id.upper())
        if ship is None:
            raise HTTPException(status_code=404, detail=f"Unknown ship {ship_id}")
        if not principal.can_drive(ship.shipId):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' cannot reroute {ship.shipId}",
            )
        body: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        destination = body.get("destination")
        if destination is None:
            options = await api_ship_options(ship.shipId, request)
            nearest = options["nearest_feasible"] or (
                options["options"][0] if options["options"] else None
            )
            if nearest is None:
                raise HTTPException(
                    status_code=400, detail="No destination supplied and no ports available"
                )
            destination = nearest["port"]
        try:
            goal, port_id, label = engine.resolve_destination(destination)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"Unknown port {exc}") from exc
        reason = str(body.get("reason") or f"manual:{principal.role}")
        engine.schedule_route(ship.shipId, goal, label=label, port=port_id, reason=reason)
        events = await engine.process_pending_routes(limit=-1, ship_id=ship.shipId)
        await publish(request, [], events)
        return {"shipId": ship.shipId, "status": ship.status, "reroutes": events}

    # -- AI distress pipeline ----------------------------------------------
    @app.get("/api/distress", tags=["ai"])
    async def api_distress_log(
        request: Request, limit: int = Query(25, ge=1, le=100)
    ) -> Dict[str, Any]:
        engine: SimulationEngine = request.app.state.engine
        logs = engine.distress_logs[-limit:][::-1]
        return {"logs": [log.model_dump() for log in logs], "service": engine.nlp.describe()}

    @app.post("/api/distress", tags=["ai"])
    async def api_distress(payload: DistressRequest, request: Request) -> Dict[str, Any]:
        """Free-form (or voice-transcribed) distress log -> structured JSON."""
        engine: SimulationEngine = request.app.state.engine
        principal = request.app.state.auth.from_request(request, required=True)
        ship_id = (payload.shipId or principal.shipId or "").upper()
        if not ship_id:
            raise HTTPException(
                status_code=400,
                detail="shipId is required when the caller is not a captain-bound token",
            )
        if ship_id not in engine.ships:
            raise HTTPException(status_code=404, detail=f"Unknown ship {ship_id}")
        if not (principal.is_command or principal.can_drive(ship_id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' cannot file distress for {ship_id}",
            )
        t0 = time.perf_counter()
        log, fired, suggestion = await engine.handle_distress(ship_id, payload.text, payload.source)
        await publish(request, fired, [])
        return {
            "log_id": log.id,
            "shipId": ship_id,
            "source": payload.source,
            "provider": engine.nlp.describe(),
            # ---- the exact contract from the spec ----
            "severity": log.report.severity,
            "issue_summary": log.report.issue_summary,
            "quantifiable_impact": log.report.quantifiable_impact,
            "recommended_action": log.report.recommended_action,
            "meta": log.report.meta,
            # ---- extras for the dashboard ----
            "report": render_report(log.report),
            "alerts": fired,
            "suggested_directive": suggestion,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    # -- test / demo control -----------------------------------------------
    @app.post("/api/ships/{ship_id}/position", tags=["control"])
    async def api_drill_position(ship_id: str, request: Request) -> Dict[str, Any]:
        """DEMO/DRILL ONLY: teleport a ship (Command role) to force a proximity
        or geofence event deterministically without waiting for the transit."""
        engine: SimulationEngine = request.app.state.engine
        principal = request.app.state.auth.from_request(request, required=True)
        if "ship:override" not in principal.permissions:
            raise HTTPException(status_code=403, detail="Command role required")
        ship = engine.ships.get(ship_id.upper())
        if ship is None:
            raise HTTPException(status_code=404, detail=f"Unknown ship {ship_id}")
        body: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        position = body.get("position")
        if not (isinstance(position, (list, tuple)) and len(position) == 2):
            raise HTTPException(status_code=400, detail="position must be [lat, lng]")
        previous = list(ship.position)
        ship.position = [float(position[0]), float(position[1])]
        engine.schedule_route(
            ship.shipId,
            ship.destination_position,
            label=ship.destination_label,
            port=ship.destination,
            reason="drill:position",
        )
        return {
            "shipId": ship.shipId,
            "previous": previous,
            "position": ship.position,
            "note": "Drill reposition applied; proximity/geofence checks run on the next tick.",
        }

    @app.post("/api/sim/tick", tags=["control"])
    async def api_force_tick(request: Request) -> Dict[str, Any]:
        """Run one tick immediately (Command only) - handy for timing demos."""
        engine: SimulationEngine = request.app.state.engine
        principal = request.app.state.auth.from_request(request, required=True)
        if "sim:tick" not in principal.permissions:
            raise HTTPException(status_code=403, detail="Command role required")
        t0 = time.perf_counter()
        fired = engine.tick()
        events = await engine.process_pending_routes()
        await engine.broadcast_tick(fired, events)
        return {
            "tick": engine.tick_count,
            "sim": engine.sim_block(),
            "alerts": fired,
            "route_events": len(events),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tick_ms": engine.metrics["tick_ms_last"],
            "broadcast_ms": engine.metrics["broadcast_ms_last"],
        }

    # -- WebSocket feed ----------------------------------------------------
    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket, token: Optional[str] = Query(None)
    ) -> None:
        auth: AuthService = websocket.app.state.auth
        manager: ConnectionManager = websocket.app.state.ws
        engine: SimulationEngine = websocket.app.state.engine
        principal = auth.resolve_token_for_api(token or "")
        if token and principal is None:
            await websocket.close(code=1008, reason="invalid token")
            return
        principal = principal or Principal()
        client_id = await manager.connect(websocket, principal)
        try:
            snapshot = engine.snapshot()
            snapshot["you"] = principal.to_dict()
            snapshot["clientId"] = client_id
            snapshot["server_time"] = time.time()
            await manager.send(websocket, snapshot)
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = message.get("type")
                if kind == "ping":
                    await manager.send(
                        websocket,
                        {
                            "type": "pong",
                            "clientTime": message.get("t"),
                            "server_time": time.time(),
                            "sim": engine.sim_block(),
                        },
                    )
                elif kind == "fleet":
                    # On-demand full state frame (used by the dashboard when it
                    # needs the fleet list before the next scheduled broadcast).
                    frame = engine.snapshot()
                    frame["server_time"] = time.time()
                    await manager.send(websocket, frame)
                elif kind == "hello":
                    await manager.send(
                        websocket,
                        {
                            "type": "welcome",
                            "clientId": client_id,
                            "you": principal.to_dict(),
                            "sim": engine.sim_block(),
                        },
                    )
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - a bad client must not kill the fan-out
            pass
        finally:
            manager.disconnect(client_id)

    # -- static dashboard (single-process local development) ---------------
    if config.FRONTEND_DIR.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(config.FRONTEND_DIR), html=True),
            name="frontend",
        )

    return app


app = create_app()