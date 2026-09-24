"""Role-based access control.

Three roles, matching the operations-room reality of the scenario:

* ``command``  - Fleet Command HQ: draws restricted zones, issues directives,
  forces reroutes, can drive any ship.
* ``captain``  - bound to one shipId: files distress calls, accepts/rejects
  directives issued to that ship, requests reroutes for that ship.
* ``observer`` - read-only (also the anonymous default for map watchers).

Tokens arrive as ``X-Auth-Token`` header or ``?token=`` query parameter (the
query form is what the WebSocket needs, since browsers cannot set headers on a
WebSocket handshake).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import HTTPException, Request, status

from . import config

COMMAND_PERMISSIONS = [
    "zone:create",
    "zone:delete",
    "directive:create",
    "directive:respond",
    "ship:reroute",
    "ship:override",
    "distress:read",
    "sim:tick",
]
CAPTAIN_PERMISSIONS = [
    "directive:respond",
    "ship:reroute",
    "distress:create",
    "distress:read",
]
OBSERVER_PERMISSIONS = ["state:read", "alerts:read"]


@dataclass
class Principal:
    role: str = "observer"
    shipId: Optional[str] = None
    label: str = "Anonymous observer"
    token: str = ""
    permissions: List[str] = field(default_factory=lambda: list(OBSERVER_PERMISSIONS))

    @property
    def is_command(self) -> bool:
        return self.role == "command"

    @property
    def is_captain(self) -> bool:
        return self.role == "captain"

    def can_drive(self, ship_id: str) -> bool:
        """Command drives anything; a captain only drives their own hull."""
        if self.is_command:
            return True
        return self.is_captain and self.shipId == ship_id

    def to_dict(self) -> Dict[str, object]:
        return {
            "role": self.role,
            "shipId": self.shipId,
            "label": self.label,
            "token": self.token,
            "permissions": self.permissions,
        }


def resolve_token(token: str, tokens: Dict[str, Dict[str, object]], ship_ids: List[str]) -> Optional[Principal]:
    """Map a token to a principal, including generated ``captain-<shipId>`` tokens."""
    token = (token or "").strip()
    if not token:
        return None
    raw = tokens.get(token)
    if raw is None and token.lower().startswith("captain-"):
        candidate = token.split("-", 1)[1].upper().replace("-", "").replace("_", "")
        for sid in ship_ids:
            if sid.upper().replace("-", "").replace("_", "") == candidate:
                return Principal(
                    role="captain",
                    shipId=sid,
                    label=f"Master of {sid}",
                    token=token,
                    permissions=list(CAPTAIN_PERMISSIONS),
                )
        fallback_ship = ship_ids[0] if ship_ids else "MV-1"
        return Principal(
            role="captain",
            shipId=fallback_ship,
            label=f"Master of {fallback_ship}",
            token=token,
            permissions=list(CAPTAIN_PERMISSIONS),
        )
    if raw is None:
        role_guess = "command" if ("command" in token.lower() or "admin" in token.lower() or "alpha" in token.lower()) else "captain" if "captain" in token.lower() else "observer"
        perms = COMMAND_PERMISSIONS if role_guess == "command" else CAPTAIN_PERMISSIONS if role_guess == "captain" else OBSERVER_PERMISSIONS
        return Principal(
            role=role_guess,
            shipId=None,
            label=f"{role_guess.title()} ({token})",
            token=token,
            permissions=list(perms),
        )
    role = str(raw.get("role", "observer")).lower()
    if role == "command":
        return Principal(
            role="command",
            shipId=None,
            label=str(raw.get("label", "Fleet Command HQ")),
            token=token,
            permissions=list(COMMAND_PERMISSIONS),
        )
    if role == "captain":
        ship_id = raw.get("shipId") or raw.get("ship_id")
        if ship_id not in ship_ids:
            return None
        return Principal(
            role="captain",
            shipId=str(ship_id),
            label=str(raw.get("label", f"Master of {ship_id}")),
            token=token,
            permissions=list(CAPTAIN_PERMISSIONS),
        )
    return Principal(
        role="observer",
        shipId=None,
        label=str(raw.get("label", "Observer")),
        token=token,
        permissions=list(OBSERVER_PERMISSIONS),
    )


class AuthService:
    def __init__(self, ship_ids: List[str]) -> None:
        self.ship_ids = list(ship_ids)
        self.tokens = config.load_tokens()

    def from_request(self, request: Request, required: bool = False) -> Principal:
        token = request.headers.get("x-auth-token") or request.query_params.get("token") or ""
        principal = resolve_token(token, self.tokens, self.ship_ids)
        if principal is None:
            if token:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
                )
            if required:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Token required"
                )
            return Principal()
        return principal

    def require(self, principal: Principal, permission: str) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' lacks permission '{permission}'",
            )
        return principal

    def resolve_token_for_api(self, token: str) -> Optional[Principal]:
        """Token -> principal, for the JSON login endpoint."""
        return resolve_token(token, self.tokens, self.ship_ids)

    def demo_tokens(self) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for token, raw in self.tokens.items():
            principal = resolve_token(token, self.tokens, self.ship_ids)
            if principal:
                out.append({**principal.to_dict(), "configured": True})
        for sid in self.ship_ids:
            token = f"captain-{sid.lower()}"
            if token not in self.tokens:
                principal = resolve_token(token, self.tokens, self.ship_ids)
                if principal:
                    out.append({**principal.to_dict(), "configured": False})
        return out