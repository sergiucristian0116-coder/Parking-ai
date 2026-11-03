from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from onvif import ONVIFCamera
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .camera_manager import CameraManager, EventHub, test_camera_connection
from .database import Database, utc_now
from .remote_access import RemoteAccessManager
from .security import (
    REFRESH_TOKEN_SECONDS,
    encrypt_secret,
    hash_password,
    issue_access_token,
    token_hash,
    verify_access_token,
    verify_password,
)
from .settings import Settings

logger = logging.getLogger("parkingai.api")
bearer_scheme = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class CameraInput(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    source_type: Literal["rtsp", "hls", "url", "onvif"]
    url: str = Field(default="", max_length=2048)
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    profile: str = Field(default="", max_length=128)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=512)
    enabled: bool = True
    zones: list[list[list[float]]] = Field(default_factory=list, max_length=10)
    parking_limit_seconds: int | None = Field(default=None, ge=1, le=31_536_000)

    @field_validator("zones")
    @classmethod
    def validate_zones(cls, zones: list[list[list[float]]]) -> list[list[list[float]]]:
        for polygon in zones:
            if len(polygon) < 3:
                raise ValueError("Each parking zone polygon must contain at least three points.")
            for point in polygon:
                if len(point) != 2 or any(not math.isfinite(float(value)) or abs(float(value)) > 100000 for value in point):
                    raise ValueError("Zone points must be finite pixel coordinates [x, y].")
        return zones

    @model_validator(mode="after")
    def validate_source(self) -> "CameraInput":
        if self.source_type == "onvif":
            if not self.host or not self.port:
                raise ValueError("ONVIF requires a host and port.")
        else:
            parsed = urlsplit(self.url)
            expected = {
                "rtsp": {"rtsp", "rtsps"},
                "hls": {"http", "https"},
                "url": {"rtsp", "rtsps", "http", "https"},
            }[self.source_type]
            if parsed.scheme.lower() not in expected or not parsed.hostname:
                raise ValueError("Camera URL must be an absolute URL matching its selected source type.")
            if parsed.username or parsed.password:
                raise ValueError("Do not put credentials in the camera URL; use the username and password fields.")
        return self


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    source_type: Literal["rtsp", "hls", "url", "onvif"] | None = None
    url: str | None = Field(default=None, max_length=2048)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    profile: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    zones: list[list[list[float]]] | None = Field(default=None, max_length=10)
    parking_limit_seconds: int | None = Field(default=None, ge=1, le=31_536_000)

    @field_validator("zones")
    @classmethod
    def validate_zones(cls, zones: list[list[list[float]]] | None) -> list[list[list[float]]] | None:
        if zones is None:
            return None
        for polygon in zones:
            if len(polygon) < 3:
                raise ValueError("Each parking zone polygon must contain at least three points.")
            for point in polygon:
                if len(point) != 2 or any(not math.isfinite(float(value)) or abs(float(value)) > 100000 for value in point):
                    raise ValueError("Zone points must be finite pixel coordinates [x, y].")
        return zones


class CameraTestInput(CameraInput):
    pass


class OnvifProfilesInput(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=512)


class DecisionInput(BaseModel):
    decision: Literal["confirmed", "rejected"]
    reason_code: str = Field(min_length=1, max_length=64)
    comment: str = Field(default="", max_length=1000)


class UserInput(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["ADMIN", "OPERATOR", "VIEWER"]


class RemoteAccessConfigInput(BaseModel):
    """Non-secret remote-access settings editable from the desktop UI.

    The tunnel token itself is never accepted here -- it is only ever
    generated server-side (see ``/api/v1/remote-access/regenerate-credentials``).
    """

    gateway_url: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    label: str = Field(default="", max_length=128)

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
            raise ValueError("gateway_url must be an http(s):// or ws(s):// URL.")
        return value


class RemoteAccessTestInput(BaseModel):
    gateway_url: str = Field(min_length=1, max_length=512)

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
            raise ValueError("gateway_url must be an http(s):// or ws(s):// URL.")
        return value


class FailedLoginLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = [value for value in self._attempts.get(key, []) if now - value < 60]
            if len(attempts) >= 5:
                self._attempts[key] = attempts
                return False
            return True

    def fail(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    db = database or Database(config.database_path)
    events = EventHub()
    manager = CameraManager(config, db, events)
    limiter = FailedLoginLimiter()
    remote_access: RemoteAccessManager  # assigned below, once ``app`` exists; read lazily by lifespan.

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        config.evidence_dir.mkdir(parents=True, exist_ok=True)
        if config.bootstrap_admin_password:
            create_bootstrap_admin(db, config.bootstrap_admin_password)
        elif not db.fetch_one("SELECT id FROM users LIMIT 1"):
            ensure_bootstrap_admin_with_generated_password(db, config)
        if remote_access.store.enabled:
            remote_access.start()
        yield
        remote_access.stop()
        manager.stop()

    app = FastAPI(title="ParkingAI API", version="1.0.0", lifespan=lifespan)
    app.state.settings = config
    app.state.database = db
    app.state.camera_manager = manager
    app.state.events = events

    remote_access = RemoteAccessManager(config, app)
    app.state.remote_access = remote_access

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    def current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> dict[str, Any]:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        claims = verify_access_token(credentials.credentials, config.access_token_secret)
        if claims is None:
            raise HTTPException(status_code=401, detail="Invalid or expired access token.")
        user = db.fetch_one(
            "SELECT id, username, role, active FROM users WHERE id = ?",
            (claims["sub"],),
        )
        if not user or not user["active"]:
            raise HTTPException(status_code=401, detail="User is unavailable.")
        return user

    def require_roles(*roles: str):
        def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
            if user["role"] not in roles:
                raise HTTPException(status_code=403, detail="Insufficient role.")
            return user

        return dependency

    def request_camera(payload: CameraInput) -> dict[str, Any]:
        return payload.model_dump()

    def camera_config(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "source_type": row["source_type"],
            "url": row["url"],
            "host": row["host"],
            "port": row["port"],
            "profile": row["profile"],
            "username": _decrypt_camera_value(row["username_enc"], config),
            "password": _decrypt_camera_value(row["password_enc"], config),
            "zones": json.loads(row["zones_json"]),
            "parking_limit_seconds": row["parking_limit_seconds"],
        }

    def public_camera(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "source_type": row["source_type"],
            "enabled": bool(row["enabled"]),
            "zones": json.loads(row["zones_json"]),
            "parking_limit_seconds": row["parking_limit_seconds"],
            "credentials_configured": bool(row["username_enc"] or row["password_enc"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_camera(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
        now = utc_now()
        camera_id = payload["id"]
        values = (
            camera_id,
            payload["name"],
            payload["source_type"],
            payload.get("url", ""),
            payload.get("host", ""),
            payload.get("port"),
            payload.get("profile", ""),
            encrypt_secret(payload.get("username"), config.camera_encryption_key)
            if payload.get("username")
            else (existing["username_enc"] if existing else None),
            encrypt_secret(payload.get("password"), config.camera_encryption_key)
            if payload.get("password")
            else (existing["password_enc"] if existing else None),
            int(payload.get("enabled", True)),
            json.dumps(payload.get("zones", []), separators=(",", ":")),
            payload.get("parking_limit_seconds") or config.parking_limit_seconds,
            existing["created_at"] if existing else now,
            now,
        )
        db.execute(
            """INSERT INTO cameras(id, name, source_type, url, host, port, profile, username_enc,
               password_enc, enabled, zones_json, parking_limit_seconds, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_type=excluded.source_type,
                 url=excluded.url, host=excluded.host, port=excluded.port, profile=excluded.profile,
                 username_enc=excluded.username_enc, password_enc=excluded.password_enc,
                 enabled=excluded.enabled, zones_json=excluded.zones_json,
                 parking_limit_seconds=excluded.parking_limit_seconds, updated_at=excluded.updated_at""",
            values,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
        address = request.client.host if request.client else "unknown"
        if not limiter.allow(address):
            raise HTTPException(status_code=429, detail="Too many failed login attempts.")
        user = db.fetch_one("SELECT * FROM users WHERE username = ?", (payload.username,))
        if not user or not user["active"] or not verify_password(payload.password, user["password_hash"]):
            limiter.fail(address)
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        limiter.clear(address)
        refresh_token = secrets.token_urlsafe(48)
        expires = int(time.time()) + REFRESH_TOKEN_SECONDS
        db.execute(
            "INSERT INTO refresh_tokens(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash(refresh_token), user["id"], expires, utc_now()),
        )
        db.audit(user["id"], "auth.login", {"remote_address": address})
        return {
            "access_token": issue_access_token(user, config.access_token_secret),
            "token_type": "bearer",
            "expires_in": 900,
            "refresh_token": refresh_token,
            "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        }

    @app.post("/api/v1/auth/refresh")
    def refresh(payload: RefreshRequest) -> dict[str, Any]:
        old_hash = token_hash(payload.refresh_token)
        row = db.fetch_one(
            """SELECT r.user_id, r.expires_at, r.revoked, u.id, u.username, u.role, u.active
               FROM refresh_tokens r JOIN users u ON u.id=r.user_id WHERE r.token_hash=?""",
            (old_hash,),
        )
        if not row or row["revoked"] or row["expires_at"] <= int(time.time()) or not row["active"]:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")
        new_token = secrets.token_urlsafe(48)
        now = int(time.time())
        db.execute("UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?", (old_hash,))
        db.execute(
            "INSERT INTO refresh_tokens(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash(new_token), row["user_id"], now + REFRESH_TOKEN_SECONDS, utc_now()),
        )
        user = {"id": row["id"], "username": row["username"], "role": row["role"]}
        return {
            "access_token": issue_access_token(user, config.access_token_secret),
            "token_type": "bearer",
            "expires_in": 900,
            "refresh_token": new_token,
        }

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(
        payload: RefreshRequest,
        user: dict[str, Any] = Depends(current_user),
    ) -> Response:
        db.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE token_hash=? AND user_id=?",
            (token_hash(payload.refresh_token), user["id"]),
        )
        db.audit(user["id"], "auth.logout", {})
        return Response(status_code=204)

    @app.get("/api/v1/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return user

    @app.get("/api/v1/users")
    def list_users(_: dict[str, Any] = Depends(require_roles("ADMIN"))) -> list[dict[str, Any]]:
        return db.fetch_all("SELECT id, username, role, active, created_at FROM users ORDER BY username")

    @app.post("/api/v1/users", status_code=201)
    def create_user(
        payload: UserInput,
        admin: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO users(id, username, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (user_id, payload.username, hash_password(payload.password), payload.role, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Username already exists.") from exc
        db.audit(admin["id"], "user.created", {"user_id": user_id, "role": payload.role})
        return {"id": user_id, "username": payload.username, "role": payload.role, "active": True}

    @app.get("/api/v1/cameras")
    def list_cameras(_: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
        return [public_camera(row) for row in db.fetch_all("SELECT * FROM cameras ORDER BY created_at")]

    @app.post("/api/v1/cameras", status_code=201)
    def create_camera(
        payload: CameraInput,
        admin: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        if db.fetch_one("SELECT id FROM cameras WHERE id=?", (payload.id,)):
            raise HTTPException(status_code=409, detail="Camera ID already exists.")
        count = db.fetch_one("SELECT COUNT(*) AS count FROM cameras")
        if count and count["count"] >= config.max_cameras:
            raise HTTPException(status_code=409, detail=f"The configured limit of {config.max_cameras} cameras has been reached.")
        save_camera(request_camera(payload))
        db.audit(admin["id"], "camera.created", {"camera_id": payload.id})
        manager.reload_camera(payload.id)
        return public_camera(db.fetch_one("SELECT * FROM cameras WHERE id=?", (payload.id,)) or {})

    @app.get("/api/v1/cameras/{camera_id}")
    def get_camera(camera_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        row = db.fetch_one("SELECT * FROM cameras WHERE id=?", (camera_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Camera not found.")
        return public_camera(row)

    @app.get("/api/v1/cameras/{camera_id}/configuration")
    def get_camera_configuration(
        camera_id: str,
        _: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        row = db.fetch_one("SELECT * FROM cameras WHERE id=?", (camera_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Camera not found.")
        return {
            **public_camera(row),
            "url": row["url"],
            "host": row["host"],
            "port": row["port"],
            "profile": row["profile"],
        }

    @app.patch("/api/v1/cameras/{camera_id}")
    def update_camera(
        camera_id: str,
        payload: CameraUpdate,
        admin: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        existing = db.fetch_one("SELECT * FROM cameras WHERE id=?", (camera_id,))
        if not existing:
            raise HTTPException(status_code=404, detail="Camera not found.")
        update = payload.model_dump(exclude_unset=True)
        merged = {
            "id": camera_id,
            "name": existing["name"],
            "source_type": existing["source_type"],
            "url": existing["url"],
            "host": existing["host"],
            "port": existing["port"],
            "profile": existing["profile"],
            "enabled": bool(existing["enabled"]),
            "zones": json.loads(existing["zones_json"]),
            "parking_limit_seconds": existing["parking_limit_seconds"],
            "username": "",
            "password": "",
        }
        merged.update(update)
        if merged["source_type"] != "onvif":
            parsed = urlsplit(merged["url"])
            expected = {
                "rtsp": {"rtsp", "rtsps"},
                "hls": {"http", "https"},
                "url": {"rtsp", "rtsps", "http", "https"},
            }.get(merged["source_type"], set())
            if parsed.scheme.lower() not in expected or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(status_code=422, detail="Invalid camera URL for selected source type.")
        elif not merged["host"] or not merged["port"]:
            raise HTTPException(status_code=422, detail="ONVIF requires a host and port.")
        try:
            save_camera(merged, existing)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid camera configuration.") from exc
        db.audit(admin["id"], "camera.updated", {"camera_id": camera_id})
        manager.reload_camera(camera_id)
        return public_camera(db.fetch_one("SELECT * FROM cameras WHERE id=?", (camera_id,)) or {})

    @app.delete("/api/v1/cameras/{camera_id}", status_code=204)
    def delete_camera(
        camera_id: str,
        admin: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> Response:
        existing = db.fetch_one("SELECT id FROM cameras WHERE id=?", (camera_id,))
        if not existing:
            raise HTTPException(status_code=404, detail="Camera not found.")
        has_history = db.fetch_one(
            """SELECT id FROM incidents WHERE camera_id=?
               UNION ALL SELECT id FROM parking_sessions WHERE camera_id=?
               UNION ALL SELECT id FROM vehicle_observations WHERE camera_id=? LIMIT 1""",
            (camera_id, camera_id, camera_id),
        )
        if has_history:
            db.execute("UPDATE cameras SET enabled=0, updated_at=? WHERE id=?", (utc_now(), camera_id))
            manager.remove_camera(camera_id)
        else:
            manager.remove_camera(camera_id)
            db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
        db.audit(admin["id"], "camera.deleted_or_disabled", {"camera_id": camera_id})
        return Response(status_code=204)

    @app.post("/api/v1/cameras/test")
    async def test_camera(payload: CameraTestInput, _: dict[str, Any] = Depends(require_roles("ADMIN"))) -> dict[str, Any]:
        return await run_in_threadpool(test_camera_connection, request_camera(payload))

    @app.post("/api/v1/cameras/onvif/profiles")
    async def onvif_profiles(
        payload: OnvifProfilesInput,
        _: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        def discover_profiles() -> list[dict[str, str]]:
            client = ONVIFCamera(payload.host, payload.port, payload.username, payload.password, timeout=5)
            profiles = client.create_media_service().GetProfiles()
            return [{"token": item.token, "name": getattr(item, "Name", "")} for item in profiles]

        try:
            profiles = await run_in_threadpool(discover_profiles)
        except Exception:
            logger.warning("ONVIF profile query failed for configured host")
            raise HTTPException(status_code=503, detail="Could not query ONVIF profiles; verify host, port and credentials.")
        return {"profiles": profiles}

    @app.get("/api/v1/cameras/{camera_id}/status")
    def camera_status(camera_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        statuses = {item["camera_id"]: item for item in manager.camera_statuses()}
        if camera_id not in statuses:
            raise HTTPException(status_code=404, detail="Camera not found.")
        return statuses[camera_id]

    @app.get("/api/v1/cameras/{camera_id}/live")
    async def camera_live(
        camera_id: str,
        mode: Literal["snapshot", "stream"] = Query(default="snapshot"),
        _: dict[str, Any] = Depends(current_user),
    ) -> Response:
        if not db.fetch_one("SELECT id FROM cameras WHERE id=?", (camera_id,)):
            raise HTTPException(status_code=404, detail="Camera not found.")
        if mode == "snapshot":
            frame = manager.latest_jpeg(camera_id)
            if frame is None:
                raise HTTPException(status_code=503, detail="Camera has no current live frame.")
            return Response(content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        async def frames():
            last_frame: bytes | None = None
            while True:
                frame = manager.latest_jpeg(camera_id)
                if frame and frame != last_frame:
                    last_frame = frame
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                # Poll frequently; this only compares an in-memory bytes reference, so a
                # short sleep does not add real cost but avoids pegging this coroutine's
                # loop iteration. A 100ms sleep previously capped every client to 10 FPS
                # regardless of how fast the backend produced new frames.
                await asyncio.sleep(0.01)

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/vehicles")
    def list_vehicles(
        _: dict[str, Any] = Depends(current_user),
        camera_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        where = "WHERE camera_id=?" if camera_id else ""
        params: tuple[Any, ...] = (camera_id, limit) if camera_id else (limit,)
        return db.fetch_all(
            f"SELECT * FROM parking_sessions {where} ORDER BY last_seen DESC LIMIT ?",
            params,
        )

    @app.get("/api/v1/vehicles/{vehicle_id:path}")
    def get_vehicle(vehicle_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        result = db.fetch_one("SELECT * FROM parking_sessions WHERE vehicle_id=? ORDER BY last_seen DESC LIMIT 1", (vehicle_id,))
        if not result:
            raise HTTPException(status_code=404, detail="Vehicle not found.")
        return result

    @app.get("/api/v1/detections")
    def list_detections(
        _: dict[str, Any] = Depends(current_user),
        camera_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        if camera_id:
            return db.fetch_all(
                "SELECT * FROM vehicle_observations WHERE camera_id=? ORDER BY observed_at DESC LIMIT ?",
                (camera_id, limit),
            )
        return db.fetch_all("SELECT * FROM vehicle_observations ORDER BY observed_at DESC LIMIT ?", (limit,))

    @app.get("/api/v1/history")
    def history(
        _: dict[str, Any] = Depends(current_user),
        camera_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        if camera_id:
            return db.fetch_all(
                "SELECT * FROM parking_sessions WHERE camera_id=? ORDER BY first_seen DESC LIMIT ?",
                (camera_id, limit),
            )
        return db.fetch_all("SELECT * FROM parking_sessions ORDER BY first_seen DESC LIMIT ?", (limit,))

    @app.get("/api/v1/incidents")
    def list_incidents(
        _: dict[str, Any] = Depends(current_user),
        incident_status: Literal["PENDING", "CONFIRMED", "REJECTED"] | None = Query(default=None, alias="status"),
        camera_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if incident_status:
            clauses.append("status=?")
            values.append(incident_status)
        if camera_id:
            clauses.append("camera_id=?")
            values.append(camera_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        return db.fetch_all(f"SELECT * FROM incidents {where} ORDER BY timestamp DESC LIMIT ?", tuple(values))

    @app.get("/api/v1/incidents/{incident_id}")
    def get_incident(incident_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        incident = db.fetch_one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found.")
        return incident

    @app.post("/api/v1/incidents/{incident_id}/decision")
    def decide_incident(
        incident_id: str,
        payload: DecisionInput,
        user: dict[str, Any] = Depends(require_roles("ADMIN", "OPERATOR")),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        incident = db.fetch_one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found.")
        target_status = "CONFIRMED" if payload.decision == "confirmed" else "REJECTED"
        if incident["status"] != "PENDING":
            if incident["status"] == target_status and incident["decided_by"] == user["id"]:
                return incident
            raise HTTPException(status_code=409, detail="Incident was already decided.")
        now = utc_now()
        changed = db.execute(
            """UPDATE incidents SET status=?, updated_at=?, decided_by=?, decision_reason=?
               WHERE id=? AND status='PENDING'""",
            (target_status, now, user["id"], payload.reason_code, incident_id),
        )
        if changed != 1:
            raise HTTPException(status_code=409, detail="Incident was already decided.")
        db.audit(
            user["id"],
            "incident.decision",
            {
                "incident_id": incident_id,
                "decision": target_status,
                "reason_code": payload.reason_code,
                "comment": payload.comment,
                "idempotency_key": idempotency_key,
            },
        )
        result = db.fetch_one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        events.publish({"type": "incident_decision", "incident_id": incident_id, "status": target_status})
        return result or {}

    @app.get("/api/v1/evidence/{evidence_id}")
    def get_evidence(evidence_id: str, _: dict[str, Any] = Depends(current_user)) -> FileResponse:
        evidence = db.fetch_one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        if not evidence:
            raise HTTPException(status_code=404, detail="Evidence not found.")
        evidence_path = Path(evidence["path"]).resolve()
        try:
            evidence_path.relative_to(config.evidence_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Evidence not found.") from exc
        if not evidence_path.is_file():
            raise HTTPException(status_code=404, detail="Evidence file is unavailable.")
        return FileResponse(evidence_path, media_type=evidence["content_type"], headers={"Cache-Control": "private, no-store"})

    @app.get("/api/v1/system/status")
    def system_status(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        cameras = manager.camera_statuses()
        return {
            "api": "online",
            "ai_running": manager.running,
            "bind_host": config.host,
            "port": config.port,
            "https": False,
            "max_cameras": config.max_cameras,
            "camera_count": len(cameras),
            "online_cameras": sum(camera["connected"] for camera in cameras),
            "cameras": cameras,
            "remote_access": {
                "enabled": remote_access.store.enabled,
                "connected": remote_access.status()["connected"],
            },
            "timestamp": utc_now(),
        }

    @app.get("/api/v1/remote-access/status")
    def remote_access_status(_: dict[str, Any] = Depends(require_roles("ADMIN"))) -> dict[str, Any]:
        return remote_access.status()

    @app.post("/api/v1/remote-access/config")
    async def remote_access_config(
        payload: RemoteAccessConfigInput,
        user: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        await remote_access.apply_config(
            gateway_url=payload.gateway_url,
            enabled=payload.enabled,
            label=payload.label if payload.label else None,
        )
        db.audit(user["id"], "remote_access.config", {"enabled": remote_access.store.enabled})
        return remote_access.status()

    @app.post("/api/v1/remote-access/regenerate-credentials")
    async def remote_access_regenerate(
        user: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        await remote_access.regenerate_token()
        db.audit(user["id"], "remote_access.regenerate_credentials", {})
        return remote_access.status()

    @app.post("/api/v1/remote-access/test")
    async def remote_access_test(
        payload: RemoteAccessTestInput,
        _: dict[str, Any] = Depends(require_roles("ADMIN")),
    ) -> dict[str, Any]:
        return await remote_access.test_connection(payload.gateway_url)

    @app.get("/api/v1/dashboard")
    def dashboard(
        _: dict[str, Any] = Depends(current_user),
        camera_id: str | None = None,
    ) -> dict[str, Any]:
        statuses = manager.camera_statuses()
        if camera_id and not db.fetch_one("SELECT id FROM cameras WHERE id=?", (camera_id,)):
            raise HTTPException(status_code=404, detail="Camera not found.")
        camera_filter = " WHERE camera_id=?" if camera_id else ""
        params: tuple[Any, ...] = (camera_id, 100) if camera_id else (100,)
        sessions = db.fetch_all(
            f"SELECT * FROM parking_sessions{camera_filter} ORDER BY last_seen DESC LIMIT ?",
            params,
        )
        observations = db.fetch_all(
            f"SELECT * FROM vehicle_observations{camera_filter} ORDER BY observed_at DESC LIMIT ?",
            params,
        )
        for item in observations:
            item["payload"] = json.loads(item.pop("payload_json"))
        return {
            "ai_running": manager.running,
            "cameras": statuses,
            "vehicles": sessions,
            "detections": observations,
        }

    @app.post("/api/v1/system/start")
    async def start_system(_: dict[str, Any] = Depends(require_roles("ADMIN", "OPERATOR"))) -> dict[str, Any]:
        try:
            await run_in_threadpool(manager.start)
        except Exception as exc:
            logger.exception("Could not start AI camera manager")
            raise HTTPException(status_code=503, detail="AI engine could not start; see server logs.") from exc
        return {"running": True, "timestamp": utc_now()}

    @app.post("/api/v1/system/stop")
    async def stop_system(_: dict[str, Any] = Depends(require_roles("ADMIN", "OPERATOR"))) -> dict[str, Any]:
        await run_in_threadpool(manager.stop)
        return {"running": False, "timestamp": utc_now()}

    @app.get("/api/v1/events")
    async def live_events(
        _: dict[str, Any] = Depends(current_user),
    ) -> StreamingResponse:
        subscriber = await events.subscribe()

        async def stream():
            try:
                yield "event: ready\ndata: {}\n\n"
                while True:
                    item = await subscriber.get()
                    yield f"event: {item['type']}\ndata: {json.dumps(item, separators=(',', ':'))}\n\n"
            finally:
                events.unsubscribe(subscriber)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def create_bootstrap_admin(database: Database, password: str) -> None:
    if len(password) < 12:
        raise RuntimeError("PARKINGAI_ADMIN_PASSWORD must contain at least 12 characters.")
    if database.fetch_one("SELECT id FROM users LIMIT 1"):
        return
    database.execute(
        "INSERT INTO users(id, username, password_hash, role, active, created_at) VALUES (?, 'admin', ?, 'ADMIN', 1, ?)",
        (str(uuid.uuid4()), hash_password(password), utc_now()),
    )


FIRST_RUN_CREDENTIALS_FILENAME = "FIRST_RUN_ADMIN_PASSWORD.txt"


def ensure_bootstrap_admin_with_generated_password(database: Database, config: Settings) -> None:
    """Create the initial admin account with a generated password when none was supplied.

    This only ever runs once, the first time the database has no users at all
    (a fresh installation). ``PARKINGAI_ADMIN_PASSWORD`` remains the preferred
    way to choose the initial password explicitly; this path exists so that
    launching the packaged desktop application for the very first time -
    without a configured shell environment - does not fail to start. The
    generated password is never hardcoded, is cryptographically random, and
    is written once to a private file so the operator can log in and should
    change it immediately afterwards (for example via `POST /api/v1/users`
    followed by disabling this bootstrap account).
    """
    if database.fetch_one("SELECT id FROM users LIMIT 1"):
        return
    password = secrets.token_urlsafe(18)
    create_bootstrap_admin(database, password)

    data_dir = config.data_dir or config.database_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    credentials_path = data_dir / FIRST_RUN_CREDENTIALS_FILENAME
    credentials_path.write_text(
        "ParkingAI - initial administrator account\n"
        "username: admin\n"
        f"password: {password}\n"
        "Log in, create a named administrator account, then delete this file "
        "and disable/rotate the 'admin' account.\n",
        encoding="utf-8",
    )
    try:
        os.chmod(credentials_path, 0o600)
    except OSError:
        pass
    logger.warning(
        "No administrator account existed. A random initial admin password was generated "
        "and written to %s. Log in, then rotate this password immediately.",
        credentials_path,
    )


def _decrypt_camera_value(value: bytes | None, settings: Settings) -> str:
    from .security import decrypt_secret

    return decrypt_secret(value, settings.camera_encryption_key)
