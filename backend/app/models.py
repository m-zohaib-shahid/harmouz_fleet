"""Pydantic contracts shared by the REST API, the WebSocket feed and the AI layer."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Role = Literal["command", "captain", "observer"]
ShipStatus = Literal[
    "normal",
    "rerouting",
    "distressed",
    "insufficient_fuel",
    "out_of_fuel",
    "stranded",
    "arrived",
]


# ---------------------------------------------------------------------------
# Dataset (fleet.json)
# ---------------------------------------------------------------------------
class Port(BaseModel):
    id: str
    name: str
    position: List[float]

    @field_validator("position")
    @classmethod
    def _two_coords(cls, v: List[float]) -> List[float]:
        if len(v) != 2:
            raise ValueError("position must be [lat, lng]")
        return [float(v[0]), float(v[1])]


class VesselSeed(BaseModel):
    shipId: str
    name: str
    position: List[float]
    speed: float
    heading: float
    destination: str
    fuel: float
    cargo: str
    status: str = "normal"


class ScenarioInfo(BaseModel):
    name: str = "Unnamed scenario"
    description: str = ""


class BoundingBox(BaseModel):
    north: float
    south: float
    east: float
    west: float


class FleetDataset(BaseModel):
    scenario: ScenarioInfo = Field(default_factory=ScenarioInfo)
    boundingBox: BoundingBox
    navigableWater: List[List[float]]
    ports: List[Port]
    fleet: List[VesselSeed]

    @field_validator("navigableWater")
    @classmethod
    def _polygon_ok(cls, v: List[List[float]]) -> List[List[float]]:
        if len(v) < 3:
            raise ValueError("navigableWater needs at least 3 vertices")
        return [[float(p[0]), float(p[1])] for p in v]


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
class RestrictedZone(BaseModel):
    id: str
    name: str
    polygon: List[List[float]]
    severity: Severity = "HIGH"
    active: bool = True
    note: str = ""
    created_by: str = "command"
    created_at: float = Field(default_factory=time.time)
    created_sim_time: str = ""
    affected_ships: List[str] = Field(default_factory=list)

    @field_validator("polygon")
    @classmethod
    def _min_vertices(cls, v: List[List[float]]) -> List[List[float]]:
        if len(v) < 3:
            raise ValueError("a restricted zone needs at least 3 vertices")
        return [[float(p[0]), float(p[1])] for p in v]


class Alert(BaseModel):
    id: str
    type: str
    severity: Severity
    message: str
    shipIds: List[str] = Field(default_factory=list)
    zoneId: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    sim_time: str = ""


class Directive(BaseModel):
    id: str
    shipId: str
    kind: Literal["course", "reroute", "hold", "resume", "assist"] = "course"
    destination: Optional[List[float]] = None
    destination_label: Optional[str] = None
    destination_port: Optional[str] = None
    note: str = ""
    status: Literal["pending", "accepted", "rejected", "completed", "cancelled"] = "pending"
    issued_by: str = "command"
    issued_at: float = Field(default_factory=time.time)
    resolved_at: Optional[float] = None
    response_note: str = ""
    sim_time: str = ""


class DistressReport(BaseModel):
    """AI output contract - the first four keys match the hackathon spec exactly."""

    severity: Severity
    issue_summary: str
    quantifiable_impact: Dict[str, Any]
    recommended_action: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class DistressLog(BaseModel):
    id: str
    shipId: str
    raw_text: str
    report: DistressReport
    created_at: float = Field(default_factory=time.time)
    sim_time: str = ""


class VesselView(BaseModel):
    """Compact per-ship payload broadcast over the WebSocket every tick."""

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
    status: str
    flags: List[str] = Field(default_factory=list)
    distance_remaining_nm: float = 0.0
    eta_hours: float = 0.0
    fuel_required: float = 0.0
    fuel_range_nm: float = 0.0
    inside_zones: List[str] = Field(default_factory=list)
    nearest_zone_km: Optional[float] = None
    weather: Dict[str, Any] = Field(default_factory=dict)
    path: List[List[float]] = Field(default_factory=list)
    path_index: int = 0
    distressed: bool = False
    directive_id: Optional[str] = None
    updated_at: float = 0.0


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
class ZoneCreateRequest(BaseModel):
    name: str = "Restricted zone"
    polygon: List[List[float]]
    severity: Severity = "HIGH"
    note: str = ""
    active: bool = True

    @field_validator("polygon")
    @classmethod
    def _min_vertices(cls, v: List[List[float]]) -> List[List[float]]:
        if len(v) < 3:
            raise ValueError("polygon needs at least 3 vertices")
        return [[float(p[0]), float(p[1])] for p in v]


class DirectiveCreateRequest(BaseModel):
    shipId: str
    destination: Union[str, List[float]]  # port id or [lat, lng]
    kind: Literal["course", "reroute", "hold", "resume", "assist"] = "course"
    note: str = ""


class DirectiveResponseRequest(BaseModel):
    accept: bool = True
    note: str = ""


class DistressRequest(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    shipId: Optional[str] = None
    source: Literal["text", "voice"] = "text"


class LoginRequest(BaseModel):
    token: str


class AuthInfo(BaseModel):
    role: Role
    shipId: Optional[str] = None
    label: str = ""
    token: str = ""
    permissions: List[str] = Field(default_factory=list)