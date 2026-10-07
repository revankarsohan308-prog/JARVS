"""Live device-connection registry for the voice plane.

Owns *live* WebSocket connections (which LINK is on the wire right now) and
pushes assistant states + audio to devices. Persistence and authorization live
in core/devices.py — nothing secret ever sits in this in-memory registry.

The injected ``ws`` only needs ``send_text`` / ``send_bytes`` / ``close``, so
tests can drive the manager with a recorder stub.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class DeviceConnection:
    device_id: str
    ws: object
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    state: str = "ready"


class DeviceManager:
    def __init__(self) -> None:
        self._conns: dict[str, DeviceConnection] = {}

    async def connect(self, device_id: str, ws: object) -> None:
        old = self._conns.pop(device_id, None)
        if old is not None:
            try:
                await old.ws.close(code=4000, reason="replaced by a new connection")
            except Exception:  # noqa: BLE001 - best-effort cleanup of a dead socket
                pass
        self._conns[device_id] = DeviceConnection(device_id=device_id, ws=ws)

    async def disconnect(self, device_id: str) -> None:
        self._conns.pop(device_id, None)

    def touch(self, device_id: str) -> None:
        conn = self._conns.get(device_id)
        if conn is not None:
            conn.last_seen = time.time()

    def set_state(self, device_id: str, state: str) -> None:
        conn = self._conns.get(device_id)
        if conn is not None:
            conn.state = state

    def connected_ids(self) -> list[str]:
        return sorted(self._conns)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._conns

    async def send_json(self, device_id: str, payload: dict) -> bool:
        conn = self._conns.get(device_id)
        if conn is None:
            return False
        import json

        await conn.ws.send_text(json.dumps(payload, separators=(",", ":")))
        return True

    async def send_audio(self, device_id: str, pcm16: bytes, chunk_size: int = 8000) -> bool:
        conn = self._conns.get(device_id)
        if conn is None:
            return False
        for i in range(0, len(pcm16), chunk_size):
            await conn.ws.send_bytes(pcm16[i : i + chunk_size])
        return True


device_manager = DeviceManager()