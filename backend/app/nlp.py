"""AI / NLP distress-call processor.

Primary path: Groq's free tier (Llama-3 8B, OpenAI-compatible endpoint) asked to
emit a strict JSON object through ``response_format={'type': 'json_object'}``.

Fallback path: a deterministic lexicon/regex parser that produces the same
contract. This guarantees the endpoint works with **no API key and no internet**
(`docker compose up` with an empty .env), which is a hard requirement here.
Both paths return the exact schema from the spec:

    {"severity", "issue_summary", "quantifiable_impact", "recommended_action"}

with an extra ``meta`` block describing provenance (model, latency, fallback).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from . import config
from .models import DistressReport, Severity

SYSTEM_PROMPT = (
    "You are the emergency distress-message parser of AegisFleet, a maritime crisis "
    "operations centre monitoring 15 commercial vessels in the Strait of Hormuz. "
    "You receive raw VHF/MF-HF/voice-transcribed distress traffic from ship masters. "
    "Extract ONLY what the message supports; never invent facts.\n"
    "Reply with a single JSON object using exactly these keys:\n"
    "{\n"
    '  "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",\n'
    '  "issue_summary": "short noun phrase, max 8 words",\n'
    '  "quantifiable_impact": {\n'
    '     "injured_crew": <integer>,\n'
    '     "flooding_risk": "HIGH" | "MEDIUM" | "LOW" | "NONE",\n'
    '     "cargo_risk": <boolean>,\n'
    '     "fire_onboard": <boolean>,\n'
    '     "propulsion": "FAILED" | "DEGRADED" | "OK" | "UNKNOWN",\n'
    '     "assistance_requested": <boolean>\n'
    "  },\n"
    '  "recommended_action": "one imperative sentence for Fleet Command"\n'
    "}\n"
    "Severity guide: CRITICAL = immediate threat to life/hull (fire, flooding, "
    "abandon ship, casualties, attack). HIGH = serious capability loss (engine "
    "failure, medical emergency, piracy approach). MEDIUM = degraded but stable. "
    "LOW = routine/advisory traffic. Reply with JSON only."
)

# ---------------------------------------------------------------------------
# Deterministic fallback lexicon (offline, zero-key, no network)
# ---------------------------------------------------------------------------
_CRITICAL_HINTS: Tuple[str, ...] = (
    "fire",
    "explosion",
    "flooding",
    "flood",
    "sinking",
    "sink",
    "abandon ship",
    "abandoning",
    "capsiz",
    "taking water",
    "taking on water",
    "casualt",
    "dead",
    "fatalit",
    "missile",
    "attack",
    "boarded",
    "hijack",
    "collision",
    "aground",
    "piracy boarding",
)
_HIGH_HINTS: Tuple[str, ...] = (
    "engine failure",
    "engine room",
    "engine breakdown",
    "lost propulsion",
    "propulsion",
    "medical emergency",
    "injured",
    "injury",
    "steering",
    "rudder",
    "list",
    "smoke",
    "pirate",
    "skiffs",
    "hull crack",
    "structural",
    "blackout",
    "generator failure",
    "medevac",
)
_MEDIUM_HINTS: Tuple[str, ...] = (
    "fuel leak",
    "oil leak",
    "leak",
    "cargo shift",
    "navigation failure",
    "radar",
    "gps",
    "electrical",
    "degraded",
    "reduced speed",
    "minor",
    "water ingress",
)
_LOW_HINTS: Tuple[str, ...] = (
    "routine",
    "drill",
    "test",
    "advisory",
    "weather report",
    "requesting medical advice",
    "delay",
    "position report",
)

_WORD_NUMBERS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "single": 1, "couple": 2, "several": 3, "multiple": 3, "dozen": 12,
}


class DistressProcessor:
    """Turns free-form distress traffic into structured, actionable JSON."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = (api_key if api_key is not None else config.GROQ_API_KEY).strip()
        self.model = config.GROQ_MODEL
        self._client: Optional[httpx.AsyncClient] = None
        self.calls = 0
        self.failures = 0
        self.heuristic_calls = 0
        self.last_latency_ms = 0.0
        self.last_source = "heuristic"
        self.last_error = ""

    @property
    def llm_enabled(self) -> bool:
        return bool(self.api_key)

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=config.GROQ_TIMEOUT_S)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- public API --------------------------------------------------------
    async def parse(
        self,
        text: str,
        *,
        ship_id: Optional[str] = None,
        nearest_assist: Optional[Dict[str, Any]] = None,
    ) -> DistressReport:
        """LLM-first, heuristic-second. Always returns a valid report."""
        t0 = time.perf_counter()
        raw: Optional[Dict[str, Any]] = None
        source = "heuristic"
        if self.llm_enabled and not config.OFFLINE:
            raw = await self._call_groq(text)
            if raw is not None:
                source = f"groq:{self.model}"
        report = None
        if raw is not None:
            report = self._coerce(raw, source)
        if report is None:
            self.heuristic_calls += 1
            report = self._heuristic(text, source="heuristic")
        if nearest_assist and report.severity in {"CRITICAL", "HIGH"}:
            report.recommended_action = self._with_assist(report, nearest_assist)
        report.meta.update(
            {
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "llm_enabled": self.llm_enabled,
                "model": self.model if source.startswith("groq") else "lexicon-v1",
                "shipId": ship_id,
                "nearest_assist": nearest_assist,
            }
        )
        self.last_latency_ms = report.meta["latency_ms"]
        self.last_source = str(source)
        return report

    # -- Groq / Llama-3 ----------------------------------------------------
    async def _call_groq(self, text: str) -> Optional[Dict[str, Any]]:
        client = await self._client_get()
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"DISTRESS TRAFFIC:\n{text}"},
            ],
        }
        for attempt in (1, 2):
            try:
                self.calls += 1
                resp = await client.post(
                    config.GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                if attempt == 2:
                    payload["messages"].append(
                        {
                            "role": "user",
                            "content": "That reply was invalid. Reply with the exact JSON object only.",
                        }
                    )
                if isinstance(parsed, dict) and "severity" in parsed:
                    return parsed
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 2:
                    return None
            if attempt == 1:
                payload["messages"] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"DISTRESS TRAFFIC:\n{text}\n\n"
                            "Reply with ONLY the JSON object described in the system prompt."
                        ),
                    },
                ]
        return None

    # -- validation --------------------------------------------------------
    def _coerce(self, raw: Dict[str, Any], source: str) -> Optional[DistressReport]:
        """Validate an LLM reply against the contract; None when unusable."""
        try:
            severity = str(raw.get("severity", "")).strip().upper()
            if severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                return None
            summary = str(raw.get("issue_summary") or raw.get("summary") or "").strip()
            action = str(raw.get("recommended_action") or raw.get("action") or "").strip()
            impact = raw.get("quantifiable_impact") or raw.get("impact") or {}
            if not isinstance(impact, dict):
                impact = {}
            if not summary:
                return None
            return DistressReport(
                severity=severity,  # type: ignore[arg-type]
                issue_summary=summary[:160],
                quantifiable_impact=self._normalise_impact(impact),
                recommended_action=action[:280] or self._default_action(severity),
                meta={"source": source, "raw_model_output": raw},
            )
        except Exception:  # noqa: BLE001 - a malformed reply just falls back
            return None

    @staticmethod
    def _normalise_impact(impact: Dict[str, Any]) -> Dict[str, Any]:
        def as_int(value: Any) -> int:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return int(value)
            text = str(value or "").strip().lower()
            if text.isdigit():
                return int(text)
            return _WORD_NUMBERS.get(text, 0)

        def as_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            return str(value or "").strip().lower() in {"true", "yes", "1", "high", "likely"}

        flooding = str(impact.get("flooding_risk", "NONE")).strip().upper()
        if flooding not in {"HIGH", "MEDIUM", "LOW", "NONE"}:
            flooding = "MEDIUM" if as_bool(flooding) else "NONE"
        propulsion = str(impact.get("propulsion", "UNKNOWN")).strip().upper()
        if propulsion not in {"FAILED", "DEGRADED", "OK", "UNKNOWN"}:
            propulsion = "UNKNOWN"
        return {
            "injured_crew": as_int(impact.get("injured_crew", 0)),
            "flooding_risk": flooding,
            "cargo_risk": as_bool(impact.get("cargo_risk", False)),
            "fire_onboard": as_bool(impact.get("fire_onboard", impact.get("fire", False))),
            "propulsion": propulsion,
            "assistance_requested": as_bool(impact.get("assistance_requested", False)),
        }

    @staticmethod
    def _default_action(severity: str) -> str:
        return {
            "CRITICAL": "Task the nearest available vessel for immediate assistance and alert SAR.",
            "HIGH": "Divert the closest capable vessel for support and monitor continuously.",
            "MEDIUM": "Log the defect, keep the vessel under watch and continue the transit.",
            "LOW": "Acknowledge the report; no operational change required.",
        }.get(severity, "Monitor the vessel.")

    # -- deterministic fallback --------------------------------------------
    def _heuristic(self, text: str, source: str = "heuristic") -> DistressReport:
        low = f" {text.lower()} "
        hits: Dict[str, List[str]] = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
        for level, hints in (
            ("CRITICAL", _CRITICAL_HINTS),
            ("HIGH", _HIGH_HINTS),
            ("MEDIUM", _MEDIUM_HINTS),
            ("LOW", _LOW_HINTS),
        ):
            for hint in hints:
                if hint in low:
                    hits[level].append(hint)

        if hits["CRITICAL"]:
            severity: Severity = "CRITICAL"
        elif hits["HIGH"]:
            severity = "HIGH"
        elif hits["MEDIUM"]:
            severity = "MEDIUM"
        elif hits["LOW"]:
            severity = "LOW"
        else:
            severity = "MEDIUM"

        injured = self._extract_injured(low)
        flooding_risk = "NONE"
        if any(k in low for k in ("flooding", "sinking", "taking water", "taking on water")):
            flooding_risk = "HIGH"
        elif any(k in low for k in ("water ingress", "hull crack", "structural", "leak")):
            flooding_risk = "MEDIUM"
        fire = any(k in low for k in ("fire", "explosion", "smoke", "burning"))
        cargo_risk = any(
            k in low
            for k in ("cargo", "crude", "oil", "lng", "chemical", "container", "spill", "dangerous goods")
        ) and (fire or flooding_risk != "NONE" or severity in {"CRITICAL", "HIGH"})
        propulsion = "UNKNOWN"
        if any(k in low for k in ("engine failure", "lost propulsion", "propulsion failure", "engine room", "main engine")):
            propulsion = "FAILED" if any(k in low for k in ("failure", "failed", "lost", "dead")) else "DEGRADED"
        elif any(k in low for k in ("engine", "propulsion", "reduced speed")):
            propulsion = "DEGRADED"
        assistance = (
            any(
                k in low
                for k in (
                    "assistance", "help", "assist", "evacuation", "evac", "medevac",
                    "rescue", "support", "mayday", "pan pan",
                )
            )
            or severity == "CRITICAL"
        )
        if injured and severity in {"MEDIUM", "LOW"}:
            severity = "HIGH"

        return DistressReport(
            severity=severity,
            issue_summary=self._summarise(hits),
            quantifiable_impact={
                "injured_crew": injured,
                "flooding_risk": flooding_risk,
                "cargo_risk": cargo_risk,
                "fire_onboard": fire,
                "propulsion": propulsion,
                "assistance_requested": assistance,
            },
            recommended_action=self._action_for(severity, fire, flooding_risk, injured, propulsion, assistance),
            meta={"source": source, "matched_keywords": sorted({h for v in hits.values() for h in v})},
        )

    @staticmethod
    def _extract_injured(low: str) -> int:
        for pattern in (
            r"(\d+)\s*(?:crew|sailors?|seafarers?|persons?|people|injured|casualt\w*|dead|fatalit\w*)",
            r"(?:injured|casualt\w*|wounded)\D{0,12}(\d+)",
        ):
            m = re.search(pattern, low)
            if m:
                return int(m.group(1))
        for word, value in _WORD_NUMBERS.items():
            if re.search(rf"\b{word}\b\s*(?:crew|sailors?|persons?|injured|casualt)", low):
                return value
        return 1 if "casualt" in low else 0

    @staticmethod
    def _summarise(hits: Dict[str, List[str]]) -> str:
        priority = hits["CRITICAL"] + hits["HIGH"] + hits["MEDIUM"] + hits["LOW"]
        mapping = (
            (("fire", "explosion", "smoke", "burning"), "Onboard fire"),
            (("flooding", "taking water", "taking on water", "sinking", "sink "), "Flooding"),
            (("casualt", "dead", "fatalit", "injured", "injury"), "Crew casualties"),
            (("engine", "propulsion"), "Propulsion failure"),
            (("medical", "medevac"), "Medical emergency"),
            (("pirate", "attack", "boarded", "skiffs", "hijack"), "Security incident"),
            (("collision",), "Collision"),
            (("aground",), "Grounding"),
            (("leak", "spill"), "Leak / pollution"),
            (("steering", "rudder"), "Steering casualty"),
            (("blackout", "generator", "electrical"), "Power failure"),
            (("cargo", "list"), "Cargo / stability issue"),
        )
        issues: List[str] = []
        for keys, label in mapping:
            if any(k in priority for k in keys) and label not in issues:
                issues.append(label)
            if len(issues) == 3:
                break
        if not issues:
            return "Unspecified distress report" if priority else "Routine report"
        return " & ".join(issues[:3])

    @staticmethod
    def _action_for(
        severity: str,
        fire: bool,
        flooding: str,
        injured: int,
        propulsion: str,
        assistance: bool,
    ) -> str:
        parts: List[str] = []
        if severity == "CRITICAL":
            parts.append("declare an emergency and warn all traffic in the area")
        if fire:
            parts.append("divert the nearest vessel with firefighting capability")
        if flooding == "HIGH":
            parts.append("order damage-control stations and prepare abandon-ship")
        if injured > 0:
            parts.append(f"task medical evacuation for {injured} casualty(ies)")
        if propulsion == "FAILED":
            parts.append("arrange tow / tug support")
        if not parts:
            if severity == "HIGH":
                parts.append("divert the closest capable vessel for support")
            elif severity == "MEDIUM":
                parts.append("log the defect and keep the vessel under watch")
            else:
                parts.append("acknowledge the report; no operational change required")
        if assistance:
            parts.append("alert SAR authorities")
        sentence = "; ".join(dict.fromkeys(parts))
        return sentence[0].upper() + sentence[1:] + "."

    @staticmethod
    def _with_assist(report: DistressReport, assist: Dict[str, Any]) -> str:
        base = report.recommended_action.rstrip(".")
        sid = assist.get("shipId", "unknown")
        detail = f"Nearest capable asset {sid}"
        dist = assist.get("distance_km")
        eta = assist.get("eta_hours")
        if isinstance(dist, (int, float)):
            detail += f" is {dist:.1f} km away"
        if isinstance(eta, (int, float)):
            detail += f" (ETA {eta:.1f} h)"
        return f"{base}. {detail} - tasking recommended."

    # -- introspection -----------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "provider": "Groq Llama-3 (free tier)" if self.llm_enabled else "Offline lexicon NLP",
            "model": self.model if self.llm_enabled else "lexicon-v1",
            "llm_enabled": self.llm_enabled,
            "offline_mode": config.OFFLINE,
            "llm_calls": self.calls,
            "llm_failures": self.failures,
            "heuristic_calls": self.heuristic_calls,
            "last_latency_ms": self.last_latency_ms,
            "last_source": self.last_source,
            "last_error": self.last_error,
        }


def render_report(report: DistressReport) -> Dict[str, Any]:
    """Ordered dict matching the spec exactly (severity, impact, action first)."""
    return {
        "severity": report.severity,
        "issue_summary": report.issue_summary,
        "quantifiable_impact": report.quantifiable_impact,
        "recommended_action": report.recommended_action,
        "meta": report.meta,
    }
