"""Outbound-only remote access tunnel for ParkingAI Desktop.

Architecture (see REMOTE_ACCESS.md for the full rationale and deployment
guide):

    Android / PWA --HTTPS--> Relay/Gateway (public VPS, real TLS cert)
                                   ^
                                   | outbound WSS (initiated by Desktop)
                                   |
                              ParkingAI Desktop (town hall PC, no inbound port)

ParkingAI Desktop never accepts inbound connections from the internet. It
dials out to a relay/gateway over a TLS WebSocket and registers itself under
a stable ``instance_id``. The relay exposes a public HTTPS surface at
``/instances/{instance_id}/...`` and forwards each HTTP request over that
WebSocket tunnel; this module executes the forwarded request in-process
against the very same FastAPI app that already enforces the existing JWT
authentication, roles, rate limiting, and input validation. The relay itself
never sees camera credentials, RTSP URLs, or plaintext passwords -- it only
relays opaque, already-authenticated HTTPS traffic.

Token rotation model: the relay trusts the first token it sees for a given
``instance_id`` (trust-on-first-use). To rotate the token afterwards (the
"REGENERARE CREDENȚIALE" button), the desktop proves it already held the
previous token; the relay only accepts the new token if the previous one
matches what it has on file. This lets an operator rotate credentials from
the desktop UI without a separate relay-side admin step, while still making
it impossible for an attacker who does not already hold the current token to
hijack an instance_id.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets as secrets_module
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .settings import Settings, _restrict_file_permissions

logger = logging.getLogger("parkingai.remote_access")

REMOTE_ACCESS_FILENAME = "remote_access.json"
PROTOCOL_VERSION = 1
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
WS_MAX_FRAME_BYTES = 8 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024
RECONNECT_MIN_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 60.0
HELLO_TIMEOUT_SECONDS = 10.0


def _new_instance_id() -> str:
    # A random UUID, never derived from the local IP/hostname, so it stays
    # stable regardless of network changes (satisfies "nu folosi IP-ul local
    # ca identificator permanent").
    return f"parkingai-{uuid.uuid4()}"


@dataclass
class RemoteAccessStore:
    """Persists remote-access identity/configuration next to the local database.

    Contains no camera credentials. ``tunnel_token`` authenticates this
    desktop instance to the relay; it is not a user password and is never
    shown in full by the API (only a redacted preview).
    """

    path: Path
    instance_id: str
    tunnel_token: str
    label: str
    gateway_url: str
    enabled: bool

    @classmethod
    def load_or_create(cls, data_dir: Path) -> "RemoteAccessStore":
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / REMOTE_ACCESS_FILENAME
        values: dict[str, Any] = {}
        if path.exists():
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                values = {}

        changed = False
        instance_id = values.get("instance_id") or ""
        if not instance_id:
            instance_id = _new_instance_id()
            changed = True
        tunnel_token = values.get("tunnel_token") or ""
        if len(tunnel_token) < 32:
            tunnel_token = secrets_module.token_urlsafe(32)
            changed = True

        store = cls(
            path=path,
            instance_id=instance_id,
            tunnel_token=tunnel_token,
            label=values.get("label") or "",
            gateway_url=values.get("gateway_url") or "",
            enabled=bool(values.get("enabled", False)),
        )
        if changed:
            store.save()
        return store

    def save(self) -> None:
        payload = {
            "instance_id": self.instance_id,
            "tunnel_token": self.tunnel_token,
            "label": self.label,
            "gateway_url": self.gateway_url,
            "enabled": self.enabled,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _restrict_file_permissions(self.path)

    def token_preview(self) -> str:
        if len(self.tunnel_token) <= 8:
            return "********"
        return f"{self.tunnel_token[:4]}...{self.tunnel_token[-4:]}"


def _tunnel_ws_url(gateway_url: str, instance_id: str) -> str:
    parsed = urlsplit(gateway_url)
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        raise ValueError("Gateway URL must use http(s):// or ws(s)://.")
    path = f"/tunnel/{instance_id}"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


class RemoteAccessManager:
    """Owns the outbound tunnel connection lifecycle and its live status."""

    def __init__(self, settings: Settings, asgi_app: Any):
        data_dir = settings.data_dir or settings.database_path.parent
        self.store = RemoteAccessStore.load_or_create(data_dir)
        self._asgi_app = asgi_app
        self._task: asyncio.Task | None = None
        self._connected = False
        self._last_error: str | None = None
        self._last_connected_at: str | None = None
        self._pending_previous_token: str | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.store.gateway_url:
            self._last_error = "No gateway URL configured."
            return
        self._task = asyncio.create_task(self._run_loop(), name="parkingai-remote-tunnel")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._connected = False

    async def apply_config(self, *, gateway_url: str | None, enabled: bool | None, label: str | None) -> None:
        async with self._lock:
            if gateway_url is not None:
                self.store.gateway_url = gateway_url.strip()
            if label is not None:
                self.store.label = label.strip()
            if enabled is not None:
                self.store.enabled = enabled
            self.store.save()
        self.stop()
        if self.store.enabled:
            self.start()

    async def regenerate_token(self) -> str:
        async with self._lock:
            new_token = secrets_module.token_urlsafe(32)
            self._pending_previous_token = self.store.tunnel_token
            self.store.tunnel_token = new_token
            self.store.save()
        # Reconnect so the relay sees the rotation (proving the previous token).
        self.stop()
        if self.store.enabled:
            self.start()
        return new_token

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.store.enabled,
            "connected": self._connected,
            "instance_id": self.store.instance_id,
            "label": self.store.label,
            "gateway_url": self.store.gateway_url,
            "tunnel_token_preview": self.store.token_preview(),
            "last_connected_at": self._last_connected_at,
            "last_error": self._last_error,
        }

    async def test_connection(self, gateway_url: str, timeout: float = HELLO_TIMEOUT_SECONDS) -> dict[str, Any]:
        """One-shot handshake test used by the "TESTEAZĂ CONEXIUNEA" button.

        Does not persist the tunnel and does not affect the live connection.
        """
        try:
            ws_url = _tunnel_ws_url(gateway_url, self.store.instance_id)
        except ValueError as exc:
            return {"ok": False, "detail": str(exc)}
        try:
            async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=2, max_size=WS_MAX_FRAME_BYTES) as connection:
                await connection.send(json.dumps({
                    "type": "hello",
                    "proto": PROTOCOL_VERSION,
                    "instance_id": self.store.instance_id,
                    "token": self.store.tunnel_token,
                    "previous_token": None,
                }))
                reply = json.loads(await asyncio.wait_for(connection.recv(), timeout=timeout))
                if reply.get("type") == "hello_ok":
                    return {"ok": True, "detail": "Gateway reachable and instance recognized."}
                return {"ok": False, "detail": reply.get("detail", "Gateway rejected the handshake.")}
        except asyncio.TimeoutError:
            return {"ok": False, "detail": "Timed out waiting for the gateway."}
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            return {"ok": False, "detail": f"Could not reach gateway: {exc}"}

    # -- connection loop -------------------------------------------------

    async def _run_loop(self) -> None:
        backoff = RECONNECT_MIN_SECONDS
        while True:
            try:
                ws_url = _tunnel_ws_url(self.store.gateway_url, self.store.instance_id)
            except ValueError as exc:
                self._last_error = str(exc)
                await asyncio.sleep(RECONNECT_MAX_SECONDS)
                continue
            try:
                async with websockets.connect(ws_url, open_timeout=HELLO_TIMEOUT_SECONDS, max_size=WS_MAX_FRAME_BYTES) as connection:
                    await self._handshake(connection)
                    self._connected = True
                    self._last_error = None
                    self._last_connected_at = _iso_now()
                    backoff = RECONNECT_MIN_SECONDS
                    logger.info("Remote access tunnel connected to %s", self.store.gateway_url)
                    await self._serve(connection)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.exceptions.WebSocketException, RuntimeError) as exc:
                self._last_error = str(exc)
                logger.warning("Remote access tunnel disconnected: %s", exc)
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _handshake(self, connection: ClientConnection) -> None:
        previous_token = getattr(self, "_pending_previous_token", None)
        await connection.send(json.dumps({
            "type": "hello",
            "proto": PROTOCOL_VERSION,
            "instance_id": self.store.instance_id,
            "token": self.store.tunnel_token,
            "previous_token": previous_token,
        }))
        self._pending_previous_token = None
        reply = json.loads(await asyncio.wait_for(connection.recv(), timeout=HELLO_TIMEOUT_SECONDS))
        if reply.get("type") != "hello_ok":
            raise RuntimeError(reply.get("detail", "Gateway rejected the handshake."))

    async def _serve(self, connection: ClientConnection) -> None:
        send_lock = asyncio.Lock()
        pending: set[asyncio.Task] = set()
        try:
            async for raw in connection:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if frame.get("type") != "request":
                    continue
                task = asyncio.create_task(self._handle_request(connection, send_lock, frame))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            for task in list(pending):
                task.cancel()

    async def _handle_request(self, connection: ClientConnection, send_lock: asyncio.Lock, frame: dict[str, Any]) -> None:
        request_id = frame.get("id", "")
        method = frame.get("method", "GET")
        path = frame.get("path", "/")
        query = frame.get("query", "")
        headers = {str(k): str(v) for k, v in (frame.get("headers") or {}).items()}
        headers.pop("host", None)
        headers.pop("content-length", None)
        body_b64 = frame.get("body_b64")
        body = base64.b64decode(body_b64) if body_b64 else b""

        url = path + (f"?{query}" if query else "")
        try:
            transport = httpx.ASGITransport(app=self._asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://parkingai.internal", timeout=30.0) as client:
                async with client.stream(method, url, headers=headers, content=body) as response:
                    await _send_frame(connection, send_lock, {
                        "type": "response_head",
                        "id": request_id,
                        "status": response.status_code,
                        "headers": dict(response.headers),
                    })
                    async for chunk in response.aiter_bytes(chunk_size=RESPONSE_CHUNK_BYTES):
                        if not chunk:
                            continue
                        await _send_frame(connection, send_lock, {
                            "type": "response_chunk",
                            "id": request_id,
                            "data_b64": base64.b64encode(chunk).decode("ascii"),
                        })
            await _send_frame(connection, send_lock, {"type": "response_end", "id": request_id})
        except Exception as exc:  # noqa: BLE001 - must not crash the tunnel loop
            logger.exception("Remote access tunnel failed to serve a forwarded request")
            await _send_frame(connection, send_lock, {
                "type": "response_error",
                "id": request_id,
                "detail": "Internal error while serving the forwarded request.",
            })


async def _send_frame(connection: ClientConnection, lock: asyncio.Lock, frame: dict[str, Any]) -> None:
    async with lock:
        await connection.send(json.dumps(frame))


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
