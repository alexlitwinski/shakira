"""Cliente WebSocket do Home Assistant para eventos state_changed em tempo real."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from websockets.asyncio.client import ClientConnection, connect

from app.config import AppSettings

log = logging.getLogger(__name__)

StateChangedCallback = Callable[[str, str | None, str, dict[str, Any]], Awaitable[None]]

MIN_RECONNECT_SEC = 1.0
MAX_RECONNECT_SEC = 30.0


def ha_websocket_url(ha_url: str) -> str:
    """Converte ha_url REST (http/https) para URL WebSocket."""
    parsed = urlparse(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if parsed.scheme in ("http", "https", "ws", "wss"):
        netloc = parsed.netloc or parsed.path
        path = parsed.path if parsed.netloc else ""
    else:
        netloc = parsed.path or "supervisor/core"
        path = ""
    base_path = path.rstrip("/")
    ws_path = f"{base_path}/api/websocket"
    return urlunparse((scheme, netloc, ws_path, "", "", ""))


@dataclass
class HaWebSocketListener:
    settings: AppSettings
    on_state_changed: StateChangedCallback
    entity_ids: set[str] = field(default_factory=set)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _connected: bool = False
    _reconnect_attempts: int = 0
    _last_event_at: str | None = None
    _msg_id: int = 0

    def is_connected(self) -> bool:
        return self._connected

    def update_entity_ids(self, entity_ids: set[str]) -> None:
        self.entity_ids = set(entity_ids)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="shakira-ha-ws")
        log.info(
            "WebSocket HA iniciado url=%s entidades=%s",
            ha_websocket_url(self.settings.ha_url),
            len(self.entity_ids),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._connected = False
        log.info("WebSocket HA parado")

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "reconnect_attempts": self._reconnect_attempts,
            "last_event_at": self._last_event_at,
            "subscribed_entities": sorted(self.entity_ids),
        }

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _run_loop(self) -> None:
        backoff = MIN_RECONNECT_SEC
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = MIN_RECONNECT_SEC
                self._reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                self._reconnect_attempts += 1
                log.warning(
                    "WebSocket HA desconectado (tentativa %s): %s",
                    self._reconnect_attempts,
                    e,
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_RECONNECT_SEC)

    async def _session(self) -> None:
        url = ha_websocket_url(self.settings.ha_url)
        token = self.settings.supervisor_token
        if not token:
            raise RuntimeError("Token HA ausente para WebSocket")

        async with connect(url, open_timeout=15, close_timeout=5) as ws:
            try:
                await self._authenticate(ws, token)
                await self._subscribe_state_changed(ws)
                self._connected = True
                log.info("WebSocket HA conectado e subscrito a state_changed")
                await self._read_events(ws)
            finally:
                self._connected = False

    async def _authenticate(self, ws: ClientConnection, token: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Esperado auth_required, recebido: {msg.get('type')}")

        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"Autenticacao HA falhou: {msg.get('type')}")

    async def _subscribe_state_changed(self, ws: ClientConnection) -> None:
        req_id = self._next_id()
        await ws.send(
            json.dumps(
                {
                    "id": req_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        if msg.get("type") == "result" and not msg.get("success"):
            raise RuntimeError(f"subscribe_events falhou: {msg}")

    async def _read_events(self, ws: ClientConnection) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "event":
                continue
            event = msg.get("event") or {}
            if event.get("event_type") != "state_changed":
                continue
            data = event.get("data") or {}
            entity_id = str(data.get("entity_id") or "")
            if not entity_id or entity_id not in self.entity_ids:
                continue

            old_state_obj = data.get("old_state") or {}
            new_state_obj = data.get("new_state") or {}
            old_state = (
                str(old_state_obj.get("state"))
                if isinstance(old_state_obj, dict) and old_state_obj.get("state") is not None
                else None
            )
            new_state = str(new_state_obj.get("state") or "")

            self._last_event_at = datetime.now(timezone.utc).isoformat()
            try:
                await self.on_state_changed(entity_id, old_state, new_state, data)
            except Exception:
                log.exception(
                    "Erro no callback state_changed entity=%s",
                    entity_id,
                )
