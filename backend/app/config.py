"""Central configuration.

Every tunable knob of the simulation lives here and can be overridden with an
environment variable, which keeps the Docker image and the local dev run in sync.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent  # .../backend
PROJECT_ROOT = BASE_DIR.parent  # repo root

DATA_DIR = Path(os.getenv("AEGIS_DATA_DIR", str(BASE_DIR / "data")))
FLEET_FILE = Path(os.getenv("AEGIS_FLEET_FILE", str(DATA_DIR / "fleet.json")))
FRONTEND_DIR = Path(os.getenv("AEGIS_FRONTEND_DIR", str(PROJECT_ROOT / "frontend")))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Simulation clock
# ---------------------------------------------------------------------------
# The HTTP/WS contract is a hard 1 Hz tick. TIME_SCALE maps one real second of
# the tick to simulated seconds of ship motion so that a long transit becomes
# visible inside a hackathon demo instead of taking hours of wall-clock time.
TICK_SECONDS: float = _env_float("AEGIS_TICK_SECONDS", 1.0)
TIME_SCALE: float = _env_float("AEGIS_TIME_SCALE", 60.0)  # 1 tick == 1 simulated minute
SIM_SECONDS_PER_TICK: float = TICK_SECONDS * TIME_SCALE
SIM_HOURS_PER_TICK: float = SIM_SECONDS_PER_TICK / 3600.0

# ---------------------------------------------------------------------------
# Units / geospatial constants
# ---------------------------------------------------------------------------
KM_PER_DEG_LAT: float = 111.32
NM_PER_KM: float = 0.5399568
KM_PER_NM: float = 1.852

# ---------------------------------------------------------------------------
# Alert thresholds (timing spec)
# ---------------------------------------------------------------------------
PROXIMITY_KM: float = _env_float("AEGIS_PROXIMITY_KM", 2.0)
PROXIMITY_COOLDOWN_S: float = _env_float("AEGIS_PROXIMITY_COOLDOWN_S", 60.0)
ARRIVAL_KM: float = _env_float("AEGIS_ARRIVAL_KM", 5.0)
ZONE_APPROACH_KM: float = _env_float("AEGIS_ZONE_APPROACH_KM", 5.0)
ZONE_SAFETY_KM: float = _env_float("AEGIS_ZONE_SAFETY_KM", 2.0)

# ---------------------------------------------------------------------------
# Fuel model
# ---------------------------------------------------------------------------
# burn(tons/hour) = FUEL_BASE_COEFF * speed_knots^2 * weather_multiplier
# "Crisis-accelerated depletion": the coefficient is intentionally high so the
# fuel state machine (insufficient_fuel -> out_of_fuel -> stranded) is reachable
# inside a demo window. MV-7 (750 t @ 14 kn) runs dry in ~15 real minutes.
FUEL_BASE_COEFF: float = _env_float("AEGIS_FUEL_COEFF", 0.25)
FUEL_BURN_MULTIPLIER: float = _env_float("AEGIS_FUEL_MULTIPLIER", 1.0)
WEATHER_BURN_PENALTY: float = _env_float("AEGIS_WEATHER_FUEL_PENALTY", 0.30)  # +30%
OUT_OF_FUEL_DRIFT_KN: float = _env_float("AEGIS_DRIFT_SPEED_KN", 1.5)
DISTRESS_SPEED_FACTOR: float = _env_float("AEGIS_DISTRESS_SPEED_FACTOR", 0.4)
STRANDED_AFTER_SIM_HOURS: float = _env_float("AEGIS_STRANDED_AFTER_HOURS", 2.0)

# ---------------------------------------------------------------------------
# Weather (Open-Meteo, free, no API key)
# ---------------------------------------------------------------------------
OFFLINE: bool = _env_bool("AEGIS_OFFLINE", False)
WEATHER_TTL_S: float = _env_float("AEGIS_WEATHER_TTL_S", 900.0)  # Open-Meteo: 15 min cadence
WEATHER_GRID_DEG: float = _env_float("AEGIS_WEATHER_GRID_DEG", 0.5)
WEATHER_ADVERSE_WIND_KN: float = _env_float("AEGIS_ADVERSE_WIND_KN", 25.0)
WEATHER_ADVERSE_WAVE_M: float = _env_float("AEGIS_ADVERSE_WAVE_M", 3.5)
OPEN_METEO_FORECAST: str = os.getenv(
    "AEGIS_OPEN_METEO_FORECAST", "https://api.open-meteo.com/v1/forecast"
)
OPEN_METEO_MARINE: str = os.getenv(
    "AEGIS_OPEN_METEO_MARINE", "https://marine-api.open-meteo.com/v1/marine"
)
WEATHER_TIMEOUT_S: float = _env_float("AEGIS_WEATHER_TIMEOUT_S", 6.0)

# ---------------------------------------------------------------------------
# Pathfinding (A* over a lat/lng grid clipped by the navigable water polygon)
# ---------------------------------------------------------------------------
GRID_CELL_DEG: float = _env_float("AEGIS_GRID_CELL_DEG", 0.03)  # ~3.3 km cells
CLEARANCE_WEIGHT: float = _env_float("AEGIS_CLEARANCE_WEIGHT", 0.55)
MAX_CLEARANCE_CELLS: int = _env_int("AEGIS_MAX_CLEARANCE_CELLS", 10)
MIN_CLEARANCE_CELLS: int = _env_int("AEGIS_MIN_CLEARANCE_CELLS", 0)
ASTAR_MAX_EXPANSIONS: int = _env_int("AEGIS_ASTAR_MAX_EXPANSIONS", 260000)
ASTAR_HEURISTIC_WEIGHT: float = _env_float("AEGIS_ASTAR_HEURISTIC_WEIGHT", 1.3)
# How many queued A* re-plans are drained per 1 Hz cycle. Re-planning is
# CPU-bound pure python, so the batch is bounded to protect the tick cadence.
ROUTES_PER_TICK: int = _env_int("AEGIS_ROUTES_PER_TICK", 2)

# ---------------------------------------------------------------------------
# AI distress parsing (Groq free tier - OpenAI compatible endpoint)
# ---------------------------------------------------------------------------
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL: str = os.getenv("AEGIS_GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL: str = os.getenv(
    "AEGIS_GROQ_URL", "https://api.groq.com/openai/v1/chat/completions"
)
GROQ_TIMEOUT_S: float = _env_float("AEGIS_GROQ_TIMEOUT_S", 12.0)

# ---------------------------------------------------------------------------
# Roles / auth
# ---------------------------------------------------------------------------
# Default demo tokens. Override with AEGIS_TOKENS='{"token": {"role": "..."}}'.
# Dynamic tokens of the form captain-mv7 / captain-mv13 are always accepted for
# any ship present in fleet.json, which keeps the demo friction-free.
DEFAULT_TOKENS: Dict[str, Dict[str, Any]] = {
    "command-alpha": {"role": "command", "label": "Fleet Command HQ"},
    "observer-demo": {"role": "observer", "label": "Observer / Read-only"},
}


def load_tokens() -> Dict[str, Dict[str, Any]]:
    """Merge the built-in demo tokens with AEGIS_TOKENS from the environment."""
    raw = os.getenv("AEGIS_TOKENS", "").strip()
    tokens: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_TOKENS.items()}
    if raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError:
            extra = None
        if isinstance(extra, dict):
            for key, value in extra.items():
                tokens[str(key)] = value if isinstance(value, dict) else {"role": str(value)}
    return tokens


# ---------------------------------------------------------------------------
# Buffers / limits / demo helpers
# ---------------------------------------------------------------------------
ALERT_BUFFER_SIZE: int = _env_int("AEGIS_ALERT_BUFFER", 400)
DISTRESS_LOG_SIZE: int = _env_int("AEGIS_DISTRESS_LOG", 100)
CORS_ORIGINS: str = os.getenv("AEGIS_CORS_ORIGINS", "*")

# Scripted restricted zone over the Strait of Hormuz narrows (demo button).
DEMO_STRAIT_ZONE: Dict[str, Any] = {
    "name": "Strait of Hormuz - Naval Exclusion (demo)",
    "severity": "CRITICAL",
    "polygon": [
        [26.95, 56.10],
        [26.95, 56.85],
        [25.80, 56.85],
        [25.80, 56.10],
    ],
}
