"""Tests for the outbound remote-access tunnel and relay/gateway service.

These tests validate, end-to-end on loopback, the mechanism described in
``REMOTE_ACCESS.md``: a desktop-side tunnel client (``RemoteAccessManager``)
dials out to a relay (``relay_service``) and forwards HTTP requests received
by the relay's public surface into the desktop's own FastAPI app in-process.

This proves the *protocol and code* are correct. It intentionally does not
and cannot prove that a real public VPS with a real domain/TLS certificate is
reachable from a phone on 4G/5G -- only the user, deploying the relay for
real (see ``REMOTE_ACCESS.md``), can validate that.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
import uvicorn
from cryptography.fernet import Fernet

from parking_service.database import Database
from parking_service.api import create_app, create_bootstrap_admin
from parking_service.remote_access import RemoteAccessManager
from parking_service.settings import Settings
from relay_service.relay_app import InstanceBindings, RelaySettings, create_relay_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _BackgroundUvicorn:
    """Runs a uvicorn app on loopback in a background thread for a test's lifetime."""

    def __init__(self, app, port: int):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=8 * 1024 * 1024)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("Background uvicorn server did not start in time.")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


class RemoteAccessStoreTests(unittest.TestCase):
    def test_load_or_create_persists_generated_identity(self) -> None:
        from parking_service.remote_access import RemoteAccessStore

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = RemoteAccessStore.load_or_create(data_dir)
            self.assertTrue(store.instance_id.startswith("parkingai-"))
            self.assertGreaterEqual(len(store.tunnel_token), 32)

            reloaded = RemoteAccessStore.load_or_create(data_dir)
            self.assertEqual(reloaded.instance_id, store.instance_id)
            self.assertEqual(reloaded.tunnel_token, store.tunnel_token)

    def test_token_preview_is_redacted(self) -> None:
        from parking_service.remote_access import RemoteAccessStore

        with tempfile.TemporaryDirectory() as tmp:
            store = RemoteAccessStore.load_or_create(Path(tmp))
            preview = store.token_preview()
            self.assertNotEqual(preview, store.tunnel_token)
            self.assertIn("...", preview)


class InstanceBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_use_binds_and_second_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindings = InstanceBindings(Path(tmp))
            ok, _ = await bindings.authorize("parkingai-abc", "token-one", None)
            self.assertTrue(ok)

            ok, detail = await bindings.authorize("parkingai-abc", "different-token", None)
            self.assertFalse(ok)
            self.assertIn("already bound", detail)

    async def test_rotation_with_correct_previous_token_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindings = InstanceBindings(Path(tmp))
            await bindings.authorize("parkingai-abc", "token-one", None)

            ok, detail = await bindings.authorize("parkingai-abc", "token-two", "token-one")
            self.assertTrue(ok)
            self.assertEqual(detail, "rotated")

            # The old token must no longer be accepted after rotation.
            ok, _ = await bindings.authorize("parkingai-abc", "token-one", None)
            self.assertFalse(ok)

    async def test_reset_clears_binding_allowing_new_first_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindings = InstanceBindings(Path(tmp))
            await bindings.authorize("parkingai-abc", "token-one", None)
            removed = await bindings.reset("parkingai-abc")
            self.assertTrue(removed)

            ok, detail = await bindings.authorize("parkingai-abc", "brand-new-token", None)
            self.assertTrue(ok)
            self.assertEqual(detail, "bound (first use)")


class TunnelLoopbackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full loopback round trip: relay <-(WSS-equivalent WS)-> desktop tunnel client.

    This simulates, without any real internet/TLS, the exact frame protocol
    that would run over the public relay: a public HTTP client calls the
    relay, the relay forwards the request over a WebSocket to the connected
    desktop tunnel client, and the desktop serves it in-process against its
    real FastAPI app (so real JWT auth is exercised too).
    """

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)

        relay_port = _free_port()
        relay_settings = RelaySettings(
            data_dir=root / "relay_data",
            admin_token="test-admin-token",
            rate_limit_per_minute=240,
            max_concurrent_per_instance=16,
            max_body_bytes=4 * 1024 * 1024,
            request_timeout_seconds=10.0,
        )
        self.relay_app = create_relay_app(relay_settings)
        self.relay_server = _BackgroundUvicorn(self.relay_app, relay_port)
        self.relay_server.start()
        self.relay_base_url = f"http://127.0.0.1:{relay_port}"

        desktop_settings = Settings(
            host="127.0.0.1",
            port=8766,
            database_path=root / "desktop.sqlite3",
            evidence_dir=root / "evidence",
            access_token_secret="test-secret-value-with-at-least-32-characters",
            camera_encryption_key=Fernet.generate_key(),
            bootstrap_admin_password="a-safe-test-password",
            cors_origins=("http://localhost:1420",),
            max_cameras=20,
            parking_limit_seconds=900,
            data_dir=root,
        )
        self.database = Database(desktop_settings.database_path)
        # The tunnel client forwards requests straight into the ASGI app via
        # httpx.ASGITransport, which does not trigger FastAPI's lifespan
        # startup event (unlike a real uvicorn/TestClient run), so the
        # bootstrap admin account is created explicitly here instead.
        create_bootstrap_admin(self.database, "a-safe-test-password")
        self.desktop_app = create_app(desktop_settings, self.database)

        self.manager = RemoteAccessManager(desktop_settings, self.desktop_app)
        await self.manager.apply_config(gateway_url=self.relay_base_url, enabled=True, label="test-instance")

        deadline = asyncio.get_event_loop().time() + 5
        while not self.manager.status()["connected"] and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        self.assertTrue(self.manager.status()["connected"], self.manager.status())

    async def asyncTearDown(self) -> None:
        self.manager.stop()
        self.relay_server.stop()
        self.temp.cleanup()

    async def test_public_health_check_is_forwarded_through_the_tunnel(self) -> None:
        instance_id = self.manager.store.instance_id
        async with httpx.AsyncClient(base_url=self.relay_base_url, timeout=10.0) as client:
            response = await client.get(f"/instances/{instance_id}/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    async def test_authenticated_api_call_is_forwarded_and_enforces_real_jwt_auth(self) -> None:
        instance_id = self.manager.store.instance_id
        async with httpx.AsyncClient(base_url=self.relay_base_url, timeout=10.0) as client:
            unauthenticated = await client.get(f"/instances/{instance_id}/api/v1/me")
            self.assertEqual(unauthenticated.status_code, 401)

            login = await client.post(
                f"/instances/{instance_id}/api/v1/auth/login",
                json={"username": "admin", "password": "a-safe-test-password"},
            )
            self.assertEqual(login.status_code, 200)
            token = login.json()["access_token"]

            me = await client.get(
                f"/instances/{instance_id}/api/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["username"], "admin")

    async def test_unknown_instance_id_returns_service_unavailable(self) -> None:
        async with httpx.AsyncClient(base_url=self.relay_base_url, timeout=10.0) as client:
            response = await client.get("/instances/parkingai-does-not-exist/health")
        self.assertEqual(response.status_code, 503)

    async def test_admin_reset_requires_the_configured_admin_token(self) -> None:
        instance_id = self.manager.store.instance_id
        async with httpx.AsyncClient(base_url=self.relay_base_url, timeout=10.0) as client:
            forbidden = await client.post(f"/admin/instances/{instance_id}/reset")
            self.assertEqual(forbidden.status_code, 403)

            allowed = await client.post(
                f"/admin/instances/{instance_id}/reset",
                headers={"x-admin-token": "test-admin-token"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertTrue(allowed.json()["removed"])


if __name__ == "__main__":
    unittest.main()
