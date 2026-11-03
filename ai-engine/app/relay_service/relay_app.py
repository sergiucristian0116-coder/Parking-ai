"""FastAPI relay/gateway app: public HTTPS <-> outbound WSS tunnel bridge.

Deployment model (see ``REMOTE_ACCESS.md``):

    Android / PWA --HTTPS (real TLS cert)--> this relay --(in-process)-->
        forwards over an existing WebSocket tunnel --> ParkingAI Desktop

The relay is intentionally "dumb": it authenticates *which desktop instance*
a public request should be routed to, relays opaque HTTP request/response
frames over the tunnel, and otherwise trusts the desktop's own FastAPI app to
enforce all user-level authentication/authorization (JWT, roles). The relay
never decrypts, inspects, or stores camera credentials, plate numbers, or
ParkingAI user passwords -- it only sees whatever bytes/headers the desktop
chooses to forward (which, for public endpoints, is standard HTTPS traffic
that would otherwise cross the wire anyway).

Trust model: each desktop is identified by a random ``instance_id`` (not an
IP address, since IPs change and must not be treated as identity). The first
token a relay sees for a given ``instance_id`` is bound to it in
``relay_data/instances.json`` (trust-on-first-use). A desktop can rotate its
token later by presenting the *previous* token alongside the new one in the
hello frame; the relay only accepts the rotation if the previous token
matches what is on file, which prevents a third party from hijacking an
``instance_id`` even if they can guess or brute force it (rate-limited
regardless).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("parkingai.relay")

PROTOCOL_VERSION = 1
DEFAULT_RATE_LIMIT_PER_MINUTE = 240
DEFAULT_MAX_CONCURRENT_PER_INSTANCE = 16
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_WS_MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass
class RelaySettings:
    data_dir: Path
    admin_token: str | None
    rate_limit_per_minute: int
    max_concurrent_per_instance: int
    max_body_bytes: int
    request_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "RelaySettings":
        data_dir = Path(os.environ.get("PARKINGAI_RELAY_DATA_DIR", Path(__file__).resolve().parents[2] / "relay_data")).resolve()
        return cls(
            data_dir=data_dir,
            admin_token=os.environ.get("PARKINGAI_RELAY_ADMIN_TOKEN") or None,
            rate_limit_per_minute=int(os.environ.get("PARKINGAI_RELAY_RATE_LIMIT_PER_MINUTE", str(DEFAULT_RATE_LIMIT_PER_MINUTE))),
            max_concurrent_per_instance=int(
                os.environ.get("PARKINGAI_RELAY_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT_PER_INSTANCE))
            ),
            max_body_bytes=int(os.environ.get("PARKINGAI_RELAY_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))),
            request_timeout_seconds=float(
                os.environ.get("PARKINGAI_RELAY_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))
            ),
        )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InstanceBindings:
    """Persists the trust-on-first-use (instance_id -> token hash) bindings."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "instances.json"
        self._lock = asyncio.Lock()
        self._bindings: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                self._bindings = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._bindings = {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._bindings, indent=2), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    async def authorize(self, instance_id: str, token: str, previous_token: str | None) -> tuple[bool, str]:
        """Returns ``(ok, detail)``. Binds/rotates as a side effect when ``ok``."""
        token_hash = _hash_token(token)
        async with self._lock:
            existing = self._bindings.get(instance_id)
            if existing is None:
                self._bindings[instance_id] = {"token_hash": token_hash, "bound_at": time.time()}
                self._save()
                return True, "bound (first use)"
            if existing["token_hash"] == token_hash:
                return True, "ok"
            if previous_token is not None and existing["token_hash"] == _hash_token(previous_token):
                self._bindings[instance_id] = {"token_hash": token_hash, "bound_at": time.time()}
                self._save()
                return True, "rotated"
            return False, "instance_id is already bound to a different token"

    async def reset(self, instance_id: str) -> bool:
        async with self._lock:
            if instance_id in self._bindings:
                del self._bindings[instance_id]
                self._save()
                return True
            return False


class _RateLimiter:
    """Simple sliding-window rate limiter keyed by an arbitrary string (e.g. client IP)."""

    def __init__(self, limit_per_minute: int):
        self._limit = limit_per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            hits = [value for value in self._hits.get(key, []) if now - value < 60.0]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


@dataclass
class ConnectionState:
    websocket: WebSocket
    instance_id: str
    semaphore: asyncio.Semaphore
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, "asyncio.Queue[dict[str, Any]]"] = field(default_factory=dict)


def create_relay_app(settings: RelaySettings | None = None) -> FastAPI:
    config = settings or RelaySettings.from_env()
    bindings = InstanceBindings(config.data_dir)
    rate_limiter = _RateLimiter(config.rate_limit_per_minute)
    connections: dict[str, ConnectionState] = {}
    connections_lock = asyncio.Lock()

    app = FastAPI(title="ParkingAI Relay", version="1.0.0")
    app.state.settings = config

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/tunnel/{instance_id}")
    async def tunnel(websocket: WebSocket, instance_id: str) -> None:
        await websocket.accept()
        try:
            raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            hello = json.loads(raw_hello)
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=4400)
            return
        if hello.get("type") != "hello" or hello.get("instance_id") != instance_id:
            await websocket.close(code=4400)
            return
        token = hello.get("token") or ""
        previous_token = hello.get("previous_token")
        if len(token) < 16:
            await websocket.close(code=4401)
            return
        ok, detail = await bindings.authorize(instance_id, token, previous_token)
        if not ok:
            await websocket.send_text(json.dumps({"type": "hello_error", "detail": detail}))
            await websocket.close(code=4403)
            return

        async with connections_lock:
            stale = connections.pop(instance_id, None)
        if stale is not None:
            try:
                await stale.websocket.close(code=4409)
            except Exception:  # noqa: BLE001 - best-effort cleanup of a stale connection
                pass

        state = ConnectionState(
            websocket=websocket,
            instance_id=instance_id,
            semaphore=asyncio.Semaphore(config.max_concurrent_per_instance),
        )
        async with connections_lock:
            connections[instance_id] = state

        await websocket.send_text(json.dumps({"type": "hello_ok", "detail": detail}))
        logger.info("Instance %s connected (%s)", instance_id, detail)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                request_id = frame.get("id")
                queue = state.pending.get(request_id) if request_id else None
                if queue is not None:
                    await queue.put(frame)
        except WebSocketDisconnect:
            pass
        finally:
            async with connections_lock:
                if connections.get(instance_id) is state:
                    del connections[instance_id]
            for queue in state.pending.values():
                await queue.put({"type": "response_error", "detail": "Tunnel disconnected."})
            logger.info("Instance %s disconnected", instance_id)

    @app.api_route(
        "/instances/{instance_id}/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
    )
    async def proxy(instance_id: str, path: str, request: Request) -> Response:
        client_key = request.client.host if request.client else "unknown"
        if not await rate_limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="Too many requests.")

        state = connections.get(instance_id)
        if state is None:
            raise HTTPException(status_code=503, detail="This ParkingAI instance is not currently connected.")

        body = await request.body()
        if len(body) > config.max_body_bytes:
            raise HTTPException(status_code=413, detail="Request body too large.")

        acquired = False
        try:
            acquired = await asyncio.wait_for(state.semaphore.acquire(), timeout=config.request_timeout_seconds)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="This ParkingAI instance is too busy; try again shortly.") from None
        if not acquired:
            raise HTTPException(status_code=503, detail="This ParkingAI instance is too busy; try again shortly.")

        import base64
        import uuid as uuid_module

        request_id = uuid_module.uuid4().hex
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        state.pending[request_id] = queue

        def _cleanup() -> None:
            # Only ever called once per request: either on an early failure
            # (before the streaming response starts) or once the response
            # body generator below has fully drained/ended/errored/been
            # cancelled. Removing the pending-queue entry any earlier would
            # drop in-flight response_chunk/response_end frames that arrive
            # while Starlette is still streaming the body back to the caller.
            state.pending.pop(request_id, None)
            state.semaphore.release()

        try:
            headers = dict(request.headers)
            headers.pop("host", None)
            frame = {
                "type": "request",
                "id": request_id,
                "method": request.method,
                "path": f"/{path}",
                "query": request.url.query,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode("ascii") if body else None,
            }
            async with state.send_lock:
                await state.websocket.send_text(json.dumps(frame))

            try:
                head = await asyncio.wait_for(queue.get(), timeout=config.request_timeout_seconds)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="Timed out waiting for the ParkingAI instance.") from None
            if head.get("type") == "response_error":
                raise HTTPException(status_code=502, detail=head.get("detail", "The ParkingAI instance failed to serve the request."))
            if head.get("type") != "response_head":
                raise HTTPException(status_code=502, detail="Unexpected response from the ParkingAI instance.")
        except Exception:
            _cleanup()
            raise

        status_code = int(head.get("status", 502))
        response_headers = {
            str(k): str(v)
            for k, v in (head.get("headers") or {}).items()
            if str(k).lower() not in ("content-length", "transfer-encoding", "connection")
        }
        media_type = response_headers.pop("content-type", None)

        async def body_stream():
            try:
                while True:
                    try:
                        chunk_frame = await asyncio.wait_for(queue.get(), timeout=config.request_timeout_seconds)
                    except asyncio.TimeoutError:
                        return
                    frame_type = chunk_frame.get("type")
                    if frame_type == "response_chunk":
                        data_b64 = chunk_frame.get("data_b64")
                        if data_b64:
                            yield base64.b64decode(data_b64)
                    elif frame_type in ("response_end", "response_error"):
                        return
            finally:
                _cleanup()

        return StreamingResponse(
            body_stream(),
            status_code=status_code,
            headers=response_headers,
            media_type=media_type,
        )

    @app.post("/admin/instances/{instance_id}/reset")
    async def admin_reset(instance_id: str, x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
        if not config.admin_token:
            raise HTTPException(status_code=404, detail="Not found.")
        if not x_admin_token or x_admin_token != config.admin_token:
            raise HTTPException(status_code=403, detail="Forbidden.")
        removed = await bindings.reset(instance_id)
        return {"removed": removed}

    return app
