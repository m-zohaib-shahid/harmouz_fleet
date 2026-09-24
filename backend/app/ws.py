"""WebSocket fan-out.

The whole point of this class is the timing contract: one ``json.dumps`` per
message, then a concurrent ``send_text`` to every socket, with dead sockets
pruned and the elapsed time reported back so the tick loop can publish it in
``/api/metrics`` (broadcast_ms) and in every state frame.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any, Dict, List

from fastapi import WebSocket

from .auth import Principal


class ConnectionManager:
    def __init__(self, send_timeout_s: float = 2.0) -> None:
        self._clients: Dict[int, Dict[str, Any]] = {}
        self._counter = itertools.count(1)
        self.send_timeout_s = send_timeout_s
        self.total_connections = 0
        self.total_disconnects = 0
        self.total_messages_sent = 0
        self.send_failures = 0
        self.last_broadcast_ms = 0.0

    # -- lifecycle ---------------------------------------------------------
    async def connect(self, websocket: WebSocket, principal: Principal) -> int:
        await websocket.accept()
        client_id = next(self._counter)
        self._clients[client_id] = {
            "id": client_id,
            "ws": websocket,
            "role": principal.role,
            "shipId": principal.shipId,
            "label": principal.label,
            "connected_at": time.time(),
            "messages": 0,
        }
        self.total_connections += 1
        return client_id

    def disconnect(self, client_id: int) -> None:
        if self._clients.pop(client_id, None) is not None:
            self.total_disconnects += 1

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def roster(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [
            {
                "id": c["id"],
                "role": c["role"],
                "shipId": c["shipId"],
                "label": c["label"],
                "connected_s": round(now - c["connected_at"], 1),
                "messages": c["messages"],
            }
            for c in self._clients.values()
        ]

    # -- send --------------------------------------------------------------
    async def send(self, websocket: WebSocket, payload: Dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload, default=str, separators=(",", ":")))

    async def send_to(self, client_id: int, payload: Dict[str, Any]) -> bool:
        client = self._clients.get(client_id)
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client["ws"].send_text(
                    json.dumps(payload, default=str, separators=(",", ":"))
                ),
                timeout=self.send_timeout_s,
            )
            client["messages"] += 1
            self.total_messages_sent += 1
            return True
        except Exception:  # noqa: BLE001 - a dead client must not break the tick
            self.send_failures += 1
            self.disconnect(client_id)
            return False

    async def broadcast(self, payload: Dict[str, Any]) -> int:
        """Serialize once, then fan out concurrently. Returns the client count."""
        if not self._clients:
            self.last_broadcast_ms = 0.0
            return 0
        t0 = time.perf_counter()
        text = json.dumps(payload, default=str, separators=(",", ":"))
        targets = list(self._clients.items())
        results = await asyncio.gather(
            *(self._send_text(cid, client["ws"], text) for cid, client in targets),
            return_exceptions=True,
        )
        for (cid, _), result in zip(targets, results):
            if result is True:
                self._clients[cid]["messages"] += 1
                self.total_messages_sent += 1
            else:
                self.send_failures += 1
                self.disconnect(cid)
        self.last_broadcast_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return len(self._clients)

    async def _send_text(self, client_id: int, websocket: WebSocket, text: str) -> bool:
        try:
            await asyncio.wait_for(
                websocket.send_text(text), timeout=self.send_timeout_s
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            "clients": self.client_count,
            "roster": self.roster(),
            "total_connections": self.total_connections,
            "total_disconnects": self.total_disconnects,
            "total_messages_sent": self.total_messages_sent,
            "send_failures": self.send_failures,
            "last_broadcast_ms": self.last_broadcast_ms,
        }


def clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop internal keys before sending a route/directive event to clients."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}
