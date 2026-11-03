from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from parking_service.api import (
    FIRST_RUN_CREDENTIALS_FILENAME,
    create_app,
    ensure_bootstrap_admin_with_generated_password,
)
from parking_service.camera_manager import CameraWorker, EventHub
from parking_service.database import Database, utc_now
from parking_service.settings import Settings


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            host="127.0.0.1",
            port=8766,
            database_path=root / "test.sqlite3",
            evidence_dir=root / "evidence",
            access_token_secret="test-secret-value-with-at-least-32-characters",
            camera_encryption_key=Fernet.generate_key(),
            bootstrap_admin_password="a-safe-test-password",
            cors_origins=("http://localhost:1420",),
            max_cameras=20,
            parking_limit_seconds=900,
        )
        self.database = Database(self.settings.database_path)
        self.app = create_app(self.settings, self.database)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a-safe-test-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.access_token = response.json()["access_token"]
        self.refresh_token = response.json()["refresh_token"]
        self.headers = {"Authorization": f"Bearer {self.access_token}"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_protected_routes_and_refresh_rotation(self) -> None:
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers).status_code, 200)
        rotated = self.client.post("/api/v1/auth/refresh", json={"refresh_token": self.refresh_token})
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.json()["refresh_token"], self.refresh_token)
        self.assertEqual(
            self.client.post("/api/v1/auth/refresh", json={"refresh_token": self.refresh_token}).status_code,
            401,
        )

    def test_camera_limit_and_credentials_are_not_returned(self) -> None:
        def create_camera(camera_id: str):
            return self.client.post(
                "/api/v1/cameras",
                headers=self.headers,
                json={
                    "id": camera_id,
                    "name": f"Camera {camera_id}",
                    "source_type": "rtsp",
                    "url": "rtsp://192.0.2.10:554/stream",
                    "username": "camera-user",
                    "password": "camera-password",
                    "zones": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                },
            )

        first = create_camera("cam-01")
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["credentials_configured"])
        self.assertNotIn("camera-password", first.text)
        self.assertNotIn("camera-user", first.text)
        self.assertEqual(create_camera("cam-01").status_code, 409)

        stored = self.database.fetch_one("SELECT username_enc, password_enc FROM cameras WHERE id='cam-01'")
        self.assertNotEqual(stored["password_enc"], b"camera-password")
        self.assertNotIn(b"camera-password", stored["password_enc"])

    def test_twenty_camera_limit(self) -> None:
        for index in range(1, 21):
            response = self.client.post(
                "/api/v1/cameras",
                headers=self.headers,
                json={
                    "id": f"camera-{index:02d}",
                    "name": f"Camera {index:02d}",
                    "source_type": "rtsp",
                    "url": f"rtsp://192.0.2.{index}/stream",
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
        overflow = self.client.post(
            "/api/v1/cameras",
            headers=self.headers,
            json={"id": "camera-21", "name": "Camera 21", "source_type": "rtsp", "url": "rtsp://192.0.2.21/stream"},
        )
        self.assertEqual(overflow.status_code, 409)
        self.assertEqual(len(self.client.get("/api/v1/cameras", headers=self.headers).json()), 20)

    def test_decision_is_persisted_and_audited(self) -> None:
        camera_id = "cam-01"
        self.database.execute(
            """INSERT INTO cameras(id, name, source_type, enabled, zones_json, parking_limit_seconds, created_at, updated_at)
               VALUES (?, ?, 'rtsp', 1, '[]', 900, ?, ?)""",
            (camera_id, "Test camera", utc_now(), utc_now()),
        )
        incident_id = "incident-1"
        self.database.execute(
            """INSERT INTO incidents(id, camera_id, vehicle_id, timestamp, duration_seconds, rule, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 901, 'PARKING_DURATION_LIMIT', 'PENDING', ?, ?)""",
            (incident_id, camera_id, f"{camera_id}:1", utc_now(), utc_now(), utc_now()),
        )

        response = self.client.post(
            f"/api/v1/incidents/{incident_id}/decision",
            headers=self.headers,
            json={"decision": "confirmed", "reason_code": "VALID", "comment": "Verified"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "CONFIRMED")
        self.assertEqual(len(self.database.fetch_all("SELECT id FROM audit_log")), 2)
        self.assertEqual(
            self.client.post(
                f"/api/v1/incidents/{incident_id}/decision",
                headers=self.headers,
                json={"decision": "rejected", "reason_code": "INVALID"},
            ).status_code,
            409,
        )

    def test_invalid_urls_and_zone_polygons_are_rejected(self) -> None:
        invalid_url = self.client.post(
            "/api/v1/cameras",
            headers=self.headers,
            json={"id": "bad-url", "name": "Bad URL", "source_type": "rtsp", "url": "file:///etc/passwd"},
        )
        self.assertEqual(invalid_url.status_code, 422)
        invalid_zone = self.client.post(
            "/api/v1/cameras",
            headers=self.headers,
            json={
                "id": "bad-zone",
                "name": "Bad zone",
                "source_type": "rtsp",
                "url": "rtsp://192.0.2.10/stream",
                "zones": [[[0, 0], [10, 10]]],
            },
        )
        self.assertEqual(invalid_zone.status_code, 422)

    def test_hls_camera_accepted_but_mismatched_source_type_rejected_with_clear_message(self) -> None:
        # Regression test for the Desktop "Error: [object Object]" report: the backend
        # already returns a clear validation message; the bug was the frontend collapsing
        # FastAPI's structured `detail` (a list of Pydantic error objects) via String().
        # This confirms the backend contract the frontend fix relies on stays correct:
        # a real HLS URL is accepted when source_type=hls, and otherwise rejected with
        # a human-readable `msg`, not silently or with an opaque object.
        accepted = self.client.post(
            "/api/v1/cameras",
            headers=self.headers,
            json={
                "id": "camera-piata-romana",
                "name": "Piata Romana Bucuresti",
                "source_type": "hls",
                "url": "https://s5.webcamromania.ro/piataromanab/tracks-v1/mono.m3u8",
            },
        )
        self.assertEqual(accepted.status_code, 201)

        rejected = self.client.post(
            "/api/v1/cameras",
            headers=self.headers,
            json={
                "id": "camera-piata-romana-wrong-type",
                "name": "Piata Romana Bucuresti",
                "source_type": "rtsp",
                "url": "https://s5.webcamromania.ro/piataromanab/tracks-v1/mono.m3u8",
            },
        )
        self.assertEqual(rejected.status_code, 422)
        detail = rejected.json()["detail"]
        self.assertIsInstance(detail, list)
        self.assertIn("matching its selected source type", detail[0]["msg"])

    def test_stationary_duration_uses_distinct_positions_and_resets_on_motion(self) -> None:
        camera_id = "timer-camera"
        self.database.execute(
            """INSERT INTO cameras(id, name, source_type, enabled, zones_json, parking_limit_seconds, created_at, updated_at)
               VALUES (?, ?, 'rtsp', 1, '[]', 900, ?, ?)""",
            (camera_id, "Timer camera", utc_now(), utc_now()),
        )
        worker = CameraWorker(
            {
                "id": camera_id,
                "name": "Timer camera",
                "zones": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                "parking_limit_seconds": 300,
            },
            self.settings,
            self.database,
            EventHub(),
            type("StubEngine", (), {"create_tracker": lambda _self: object()})(),
        )
        start = datetime.now(timezone.utc)

        def observation(x: int, seconds: int) -> None:
            worker._update_vehicle_state(
                np.zeros((100, 100, 3), dtype=np.uint8),
                [{"tracker_id": 1, "center": (x, 20), "x1": x, "y1": 10, "x2": x + 10, "y2": 20, "confidence": 0.9}],
                (start + timedelta(seconds=seconds)).isoformat(),
            )

        for second in range(5):
            observation(20, second)
        self.assertEqual(worker._vehicle_states[1]["status"], "STATIONARY")
        self.assertEqual(worker._vehicle_states[1]["stationary_count"], 5)

        observation(40, 5)
        self.assertEqual(worker._vehicle_states[1]["status"], "MOVING")
        self.assertEqual(worker._vehicle_states[1]["duration_seconds"], 0.0)
        self.assertEqual(worker._vehicle_states[1]["previous_center"], (20.0, 20.0))
        self.assertEqual(worker._vehicle_states[1]["current_center"], (40.0, 20.0))

    def test_camera_workers_isolate_offline_streams_and_bound_frame_buffers(self) -> None:
        camera_id_online = "online-camera"
        camera_id_offline = "offline-camera"
        for camera_id in (camera_id_online, camera_id_offline):
            self.database.execute(
                """INSERT INTO cameras(id, name, source_type, enabled, zones_json, parking_limit_seconds, created_at, updated_at)
                   VALUES (?, ?, 'rtsp', 1, '[]', 900, ?, ?)""",
                (camera_id, camera_id, utc_now(), utc_now()),
            )

        class FakeCapture:
            def __init__(self, url, *_args):
                self.online = "camera-online" in url

            def isOpened(self):
                return self.online

            def read(self):
                time.sleep(0.01)
                return (True, np.zeros((8, 8, 3), dtype=np.uint8)) if self.online else (False, None)

            def release(self):
                return None

            def get(self, _prop):
                return 30.0

        class StubEngine:
            def create_tracker(self):
                return object()

            def process(self, _frame, _tracker, frame_index: int = 0):
                return []

        def camera(camera_id: str, host: str) -> dict:
            return {
                "id": camera_id,
                "name": camera_id,
                "source_type": "rtsp",
                "url": f"rtsp://{host}/stream",
                "username": "",
                "password": "",
                "host": "",
                "port": None,
                "profile": "",
                "zones": [],
                "parking_limit_seconds": 900,
            }

        online = CameraWorker(camera(camera_id_online, "camera-online"), self.settings, self.database, EventHub(), StubEngine())
        offline = CameraWorker(camera(camera_id_offline, "camera-offline"), self.settings, self.database, EventHub(), StubEngine())
        with patch("parking_service.camera_manager.cv2.VideoCapture", FakeCapture):
            online.start()
            offline.start()
            deadline = time.monotonic() + 2
            while online.snapshot()["processed_frames"] == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.05)
            online_status = online.snapshot()
            offline_status = offline.snapshot()
            online.stop()
            offline.stop()

        self.assertEqual(online_status["status"], "ONLINE")
        self.assertGreater(online_status["processed_frames"], 0)
        self.assertEqual(offline_status["status"], "OFFLINE")
        self.assertEqual(online.frames.maxsize, 1)
        self.assertEqual(offline.frames.maxsize, 1)

    def test_inference_engine_process_attaches_frame_index_without_crashing(self) -> None:
        # Regression test for KeyError('frame_index'): process() used to read
        # vehicle["frame_index"] before CameraWorker ever set it, so any camera
        # with at least one real detection crashed with "AI frame processing failed".
        import yaml
        import ultralytics
        from ultralytics.engine.results import Boxes

        from parking_service.camera_manager import InferenceEngine

        class _FakeResult:
            def __init__(self, boxes: Boxes) -> None:
                self.boxes = boxes

        class _FakeModel:
            def __init__(self, boxes: Boxes) -> None:
                self._boxes = boxes

            def predict(self, *_args, **_kwargs):
                return [_FakeResult(self._boxes)]

        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        detection = Boxes(np.array([[10.0, 10.0, 50.0, 50.0, 0.9, 2.0]], dtype=np.float32), frame.shape[:2])
        empty = Boxes(np.empty((0, 6), dtype=np.float32), frame.shape[:2])

        engine = InferenceEngine.__new__(InferenceEngine)
        engine.lock = threading.Lock()
        engine.device = "cpu"
        engine.vehicle_model = _FakeModel(detection)
        engine.plate_model = _FakeModel(empty)
        engine.reader = None

        tracker_config = Path(ultralytics.__file__).resolve().parent / "cfg" / "trackers" / "bytetrack.yaml"
        with tracker_config.open("r", encoding="utf-8") as handle:
            engine.tracker_options = yaml.safe_load(handle)
        tracker = engine.create_tracker()

        vehicles = engine.process(frame, tracker, frame_index=15)

        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0]["frame_index"], 15)

    def test_ocr_decimation_is_staggered_across_frames_not_bursted(self) -> None:
        # Regression test for the FPS bottleneck: OCR used to run for every tracked
        # vehicle on the SAME frame once every 15 frames (measured ~650ms stall with
        # ~12 simultaneous vehicles), starving the single-slot capture queue. OCR must
        # instead be spread across frames (at most a couple of vehicles per frame)
        # while still covering every vehicle once per 15-frame window.
        import yaml
        import ultralytics
        from ultralytics.engine.results import Boxes

        from parking_service.camera_manager import InferenceEngine

        frame = np.zeros((600, 800, 3), dtype=np.uint8)
        # 10 well-separated boxes so BYTETracker assigns 10 distinct, stable tracker_ids.
        boxes_array = np.array(
            [[float(i) * 70.0, 10.0, float(i) * 70.0 + 50.0, 60.0, 0.9, 2.0] for i in range(10)],
            dtype=np.float32,
        )
        detection = Boxes(boxes_array, frame.shape[:2])
        empty = Boxes(np.empty((0, 6), dtype=np.float32), frame.shape[:2])

        class _FakeResult:
            def __init__(self, boxes: Boxes) -> None:
                self.boxes = boxes

        class _FakeModel:
            def __init__(self, boxes: Boxes) -> None:
                self._boxes = boxes

            def predict(self, *_args, **_kwargs):
                return [_FakeResult(self._boxes)]

        engine = InferenceEngine.__new__(InferenceEngine)
        engine.lock = threading.Lock()
        engine.device = "cpu"
        engine.vehicle_model = _FakeModel(detection)
        engine.plate_model = _FakeModel(empty)
        engine.reader = None

        ocr_calls_per_frame: list[int] = []
        original_read_plate = engine._read_plate

        def counting_read_plate(frame_arg, vehicle_arg):
            ocr_calls_per_frame[-1] += 1
            return original_read_plate(frame_arg, vehicle_arg)

        engine._read_plate = counting_read_plate

        tracker_config = Path(ultralytics.__file__).resolve().parent / "cfg" / "trackers" / "bytetrack.yaml"
        with tracker_config.open("r", encoding="utf-8") as handle:
            engine.tracker_options = yaml.safe_load(handle)
        tracker = engine.create_tracker()

        all_tracker_ids: set[int] = set()
        for frame_index in range(1, 16):
            ocr_calls_per_frame.append(0)
            vehicles = engine.process(frame, tracker, frame_index=frame_index)
            all_tracker_ids.update(v["tracker_id"] for v in vehicles)

        self.assertEqual(len(all_tracker_ids), 10)
        self.assertLessEqual(max(ocr_calls_per_frame), 2, f"OCR burst detected: {ocr_calls_per_frame}")
        self.assertEqual(sum(ocr_calls_per_frame), 10, "Every vehicle should be OCR'd exactly once per 15-frame window")

    def test_hls_camera_can_be_processed_through_camera_manager_pipeline(self) -> None:
        # Resolves and decodes the real public HLS stream the same way CameraManager
        # does (CameraManager._resolve_source + cv2.VideoCapture(..., CAP_FFMPEG)),
        # then runs the decoded frame through the real (non-stub) InferenceEngine,
        # matching CameraWorker's capture -> process pipeline end to end.
        from parking_service.camera_manager import CameraManager, InferenceEngine

        camera = {
            "id": "camera-piata-romana",
            "name": "Piata Romana Bucuresti",
            "source_type": "hls",
            "url": "https://s5.webcamromania.ro/piataromanab/tracks-v1/mono.m3u8",
            "username": "",
            "password": "",
            "host": "",
            "port": None,
            "profile": "",
        }

        source_url = CameraManager._resolve_source(camera)
        capture = cv2.VideoCapture(
            source_url,
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
        )
        if not capture.isOpened():
            capture.release()
            self.skipTest("HLS test stream is not reachable from this environment.")
        success, frame = capture.read()
        capture.release()
        if not success or frame is None:
            self.skipTest("HLS test stream did not return a decodable frame.")

        engine = InferenceEngine()
        tracker = engine.create_tracker()
        vehicles = engine.process(frame, tracker, frame_index=1)

        self.assertIsInstance(vehicles, list)
        for vehicle in vehicles:
            self.assertIn("frame_index", vehicle)
            self.assertIn("plate", vehicle)


class StartupProvisioningTests(unittest.TestCase):
    """Covers the packaged-app startup path: no shell-provided secrets/password."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "data"

    def _env_overrides(self) -> dict[str, str]:
        return {"PARKINGAI_DATA_DIR": str(self.data_dir)}

    def _patched_env(self, overrides: dict[str, str]):
        env = dict(os.environ)
        for key in ("PARKINGAI_JWT_SECRET", "PARKINGAI_ENCRYPTION_KEY", "PARKINGAI_ADMIN_PASSWORD"):
            env.pop(key, None)
        env.update(overrides)
        return patch.dict(os.environ, env, clear=True)

    def test_from_env_generates_and_persists_secrets_without_manual_env_vars(self) -> None:
        with self._patched_env(self._env_overrides()):
            first = Settings.from_env()
            second = Settings.from_env()

        self.assertGreaterEqual(len(first.access_token_secret), 32)
        self.assertEqual(first.access_token_secret, second.access_token_secret)
        self.assertEqual(first.camera_encryption_key, second.camera_encryption_key)
        self.assertTrue((self.data_dir / "local_secrets.json").exists())

    def test_from_env_prefers_explicit_environment_secrets(self) -> None:
        explicit_secret = "explicit-secret-value-with-32-plus-characters"
        overrides = {
            **self._env_overrides(),
            "PARKINGAI_JWT_SECRET": explicit_secret,
            "PARKINGAI_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        }
        with self._patched_env(overrides):
            settings = Settings.from_env()

        self.assertEqual(settings.access_token_secret, explicit_secret)
        self.assertFalse((self.data_dir / "local_secrets.json").exists())

    def test_bootstrap_admin_is_generated_when_no_password_and_no_users(self) -> None:
        with self._patched_env(self._env_overrides()):
            settings = Settings.from_env()
        database = Database(settings.database_path)

        ensure_bootstrap_admin_with_generated_password(database, settings)

        row = database.fetch_one("SELECT username, role FROM users LIMIT 1")
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "admin")
        self.assertEqual(row["role"], "ADMIN")

        credentials_file = self.data_dir / FIRST_RUN_CREDENTIALS_FILENAME
        self.assertTrue(credentials_file.exists())
        self.assertIn("password:", credentials_file.read_text(encoding="utf-8"))

    def test_bootstrap_admin_is_not_regenerated_when_a_user_already_exists(self) -> None:
        with self._patched_env(self._env_overrides()):
            settings = Settings.from_env()
        database = Database(settings.database_path)

        ensure_bootstrap_admin_with_generated_password(database, settings)
        first_password_file = (self.data_dir / FIRST_RUN_CREDENTIALS_FILENAME).read_text(encoding="utf-8")
        (self.data_dir / FIRST_RUN_CREDENTIALS_FILENAME).unlink()

        ensure_bootstrap_admin_with_generated_password(database, settings)

        self.assertFalse((self.data_dir / FIRST_RUN_CREDENTIALS_FILENAME).exists())
        rows = database.fetch_all("SELECT id FROM users")
        self.assertEqual(len(rows), 1)
        self.assertIn("password:", first_password_file)


if __name__ == "__main__":
    unittest.main()
