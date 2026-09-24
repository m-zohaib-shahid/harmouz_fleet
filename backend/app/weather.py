"""Weather service: Open-Meteo (free, no API key) with a synthetic fallback.

Two hard requirements shape this module:

1. The 1 Hz tick must never block on the network -> the tick only ever *reads*
   a pre-warmed cache (``sample``), while a background task refreshes the grid
   every ``WEATHER_TTL_S`` (15 min, matching Open-Meteo's model cadence).
2. ``docker compose up`` must work with zero cloud access -> if Open-Meteo is
   unreachable (or AEGIS_OFFLINE=1) a deterministic synthetic storm model
   produces plausible wind/wave fields, so the +30% fuel-burn penalty path is
   always demonstrable.

Open-Meteo is queried with multi-location requests: one call to the forecast
endpoint for wind and one to the marine endpoint for waves, covering every
occupied grid cell at once.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from . import config
from .geo import haversine_km

ADVERSE_MULTIPLIER = 1.0 + config.WEATHER_BURN_PENALTY


class WeatherService:
    def __init__(self, offline: bool = config.OFFLINE) -> None:
        self.offline = offline
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self.last_refresh_at: float = 0.0
        self.last_refresh_ms: float = 0.0
        self.source: str = "synthetic" if offline else "uninitialised"
        self.errors: int = 0
        self.refresh_count: int = 0
        self.last_error: str = ""

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def cell_key(lat: float, lng: float) -> str:
        g = config.WEATHER_GRID_DEG
        return f"{round(lat / g) * g:.2f},{round(lng / g) * g:.2f}"

    @staticmethod
    def is_adverse(wind_kn: float, wave_m: float) -> bool:
        return wind_kn >= config.WEATHER_ADVERSE_WIND_KN or wave_m >= config.WEATHER_ADVERSE_WAVE_M

    @staticmethod
    def synthetic(lat: float, lng: float, now: Optional[float] = None) -> Dict[str, Any]:
        """Deterministic moving-storm model (used offline / on cache miss)."""
        now = time.time() if now is None else now
        phase = now / 1200.0  # a full storm cycle every 20 minutes
        storm_lat = 26.3 + 1.6 * math.sin(phase * 0.7)
        storm_lng = 54.2 + 3.2 * math.cos(phase * 0.5)
        d = haversine_km((lat, lng), (storm_lat, storm_lng))
        wind = 9.0 + 26.0 * math.exp(-((d / 70.0) ** 2)) + 3.0 * math.sin(phase * 1.3 + lat * 0.4)
        wave = 1.0 + 5.5 * math.exp(-((d / 60.0) ** 2)) + 0.4 * math.sin(phase + 1.1)
        wind = max(2.0, min(55.0, wind))
        wave = max(0.2, min(9.5, wave))
        adverse = WeatherService.is_adverse(wind, wave)
        return {
            "cell": WeatherService.cell_key(lat, lng),
            "wind_kn": round(wind, 1),
            "gust_kn": round(wind * 1.35, 1),
            "wave_m": round(wave, 2),
            "swell_m": round(wave * 0.72, 2),
            "wave_period_s": round(4.0 + wave * 1.1, 1),
            "adverse": adverse,
            "burn_multiplier": round(ADVERSE_MULTIPLIER if adverse else 1.0, 2),
            "source": "synthetic",
            "fetched_at": now,
            "storm": {"lat": round(storm_lat, 3), "lng": round(storm_lng, 3)},
        }

    def sample(self, lat: float, lng: float) -> Dict[str, Any]:
        """Cache-first read used by the tick loop. Never blocks, never raises."""
        key = self.cell_key(lat, lng)
        entry = self._cache.get(key)
        now = time.time()
        if entry and (now - entry["fetched_at"]) <= config.WEATHER_TTL_S:
            return entry
        fresh = self.synthetic(lat, lng, now)
        if entry is None:
            self._cache[key] = fresh
        return fresh

    # -- Open-Meteo --------------------------------------------------------
    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=config.WEATHER_TIMEOUT_S)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def refresh(self, points: Iterable[Sequence[float]]) -> Dict[str, Any]:
        """Fetch wind + wave data for the distinct weather cells of `points`."""
        if self.offline:
            self.source = "synthetic (offline mode)"
            for p in points:
                key = self.cell_key(p[0], p[1])
                self._cache[key] = self.synthetic(p[0], p[1])
            return self.describe()

        cells: Dict[str, Tuple[float, float]] = {}
        for p in points:
            key = self.cell_key(p[0], p[1])
            cells.setdefault(key, (float(p[0]), float(p[1])))
        if not cells:
            return self.describe()

        lats = ",".join(f"{p[0]:.3f}" for p in cells.values())
        lngs = ",".join(f"{p[1]:.3f}" for p in cells.values())
        t0 = time.perf_counter()
        client = await self._client_get()
        try:
            wind_resp = await client.get(
                config.OPEN_METEO_FORECAST,
                params={
                    "latitude": lats,
                    "longitude": lngs,
                    "current": "wind_speed_10m,wind_gusts_10m",
                    "wind_speed_unit": "kn",
                    "timezone": "UTC",
                },
            )
            wind_resp.raise_for_status()
            wind_data = wind_resp.json()
            marine_resp = await client.get(
                config.OPEN_METEO_MARINE,
                params={
                    "latitude": lats,
                    "longitude": lngs,
                    "current": "wave_height,swell_wave_height,wave_period",
                    "timezone": "UTC",
                },
            )
            marine_resp.raise_for_status()
            marine_data = marine_resp.json()
        except Exception as exc:  # noqa: BLE001 - offline resilience is intentional
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.source = "synthetic (open-meteo unreachable)"
            for key, (lat, lng) in cells.items():
                self._cache[key] = self.synthetic(lat, lng)
            return self.describe()

        wind_list = wind_data if isinstance(wind_data, list) else [wind_data]
        marine_list = marine_data if isinstance(marine_data, list) else [marine_data]
        now = time.time()
        for i, (key, (lat, lng)) in enumerate(cells.items()):
            cur_w = (wind_list[i] if i < len(wind_list) else {}).get("current", {}) or {}
            cur_m = (marine_list[i] if i < len(marine_list) else {}).get("current", {}) or {}
            wind = float(cur_w.get("wind_speed_10m") or 0.0)
            gust = float(cur_w.get("wind_gusts_10m") or 0.0)
            wave = float(cur_m.get("wave_height") or 0.0)
            swell = float(cur_m.get("swell_wave_height") or 0.0)
            period = float(cur_m.get("wave_period") or 0.0)
            fallback = self.synthetic(lat, lng, now)
            if wind <= 0.0:
                wind, gust = fallback["wind_kn"], fallback["gust_kn"]
            if wave <= 0.0:
                wave = fallback["wave_m"]
                swell = fallback["swell_m"]
                period = fallback["wave_period_s"]
            adverse = self.is_adverse(wind, wave)
            self._cache[key] = {
                "cell": key,
                "wind_kn": round(wind, 1),
                "gust_kn": round(gust, 1),
                "wave_m": round(wave, 2),
                "swell_m": round(swell, 2),
                "wave_period_s": round(period, 1),
                "adverse": adverse,
                "burn_multiplier": round(ADVERSE_MULTIPLIER if adverse else 1.0, 2),
                "source": "open-meteo",
                "fetched_at": now,
            }
        self.refresh_count += 1
        self.last_refresh_at = now
        self.last_refresh_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        self.source = "open-meteo"
        self.last_error = ""
        return self.describe()

    async def run_forever(self, provider, interval: Optional[float] = None) -> None:
        """Background refresher: `provider()` returns the points to keep warm."""
        interval = interval or config.WEATHER_TTL_S
        while True:
            try:
                points = provider()
                await self.refresh(points)
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(interval)

    # -- introspection -----------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "offline": self.offline,
            "cached_cells": len(self._cache),
            "error_count": self.errors,
            "last_error": self.last_error,
            "refresh_count": self.refresh_count,
            "last_refresh_ms": self.last_refresh_ms,
            "provider": "Open-Meteo forecast + marine API (free, no API key)",
            "adverse_thresholds": {
                "wind_kn": config.WEATHER_ADVERSE_WIND_KN,
                "wave_m": config.WEATHER_ADVERSE_WAVE_M,
            },
            "burn_penalty_pct": int(config.WEATHER_BURN_PENALTY * 100),
        }

    def snapshot_grid(self) -> List[Dict[str, Any]]:
        return list(self._cache.values())
