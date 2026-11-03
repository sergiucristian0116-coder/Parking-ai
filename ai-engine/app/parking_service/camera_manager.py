from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import numpy as np
import yaml
from cryptography.fernet import InvalidToken
from onvif import ONVIFCamera
from ultralytics.engine.results import Boxes
from ultralytics import YOLO
from ultralytics.trackers.byte_tracker import BYTETracker

from .database import Database, utc_now
from .security import decrypt_secret
from .settings import Settings, PROJECT_DIR

logger = logging.getLogger("parkingai.cameras")


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, subscriber in subscribers:
            if loop.is_closed():
                continue
            loop.call_soon_threadsafe(self._enqueue, subscriber, event)

    @staticmethod
    def _enqueue(subscriber: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if subscriber.full():
            try:
                subscriber.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            subscriber.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        subscriber: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add((asyncio.get_running_loop(), subscriber))
        return subscriber

    def unsubscribe(self, subscriber: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = {item for item in self._subscribers if item[1] is not subscriber}


class InferenceEngine:
    def __init__(self) -> None:
        model_path = PROJECT_DIR / "yolo11n.pt"
        plate_model_path = Path(__file__).resolve().parents[2] / "license-plate-finetune-v1n.pt"
        if not model_path.is_file() or not plate_model_path.is_file():
            raise RuntimeError("YOLO or license-plate model file is missing.")

        import torch
        import easyocr

        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.vehicle_model = YOLO(str(model_path)).to(self.device)
        self.plate_model = YOLO(str(plate_model_path)).to(self.device)
        self.reader = easyocr.Reader(["en"], gpu=self.device.startswith("cuda"), verbose=False)
        self.lock = threading.Lock()
        import ultralytics

        tracker_config = Path(ultralytics.__file__).resolve().parent / "cfg" / "trackers" / "bytetrack.yaml"
        with tracker_config.open("r", encoding="utf-8") as file:
            self.tracker_options = yaml.safe_load(file)

    def create_tracker(self) -> BYTETracker:
        return BYTETracker(SimpleNamespace(**self.tracker_options))

    def process(self, frame: np.ndarray, tracker: BYTETracker, frame_index: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            result = self.vehicle_model.predict(
                frame,
                classes=[2, 3, 5, 7],
                conf=0.35,
                verbose=False,
            )[0]
            if result.boxes is None or len(result.boxes) == 0:
                detections = Boxes(np.empty((0, 6), dtype=np.float32), frame.shape[:2])
                tracks = tracker.update(detections, frame)
                return self._format_tracks(tracks, frame, frame_index)

            boxes = result.boxes.cpu()
            tracks = tracker.update(boxes, frame)
            formatted = self._format_tracks(tracks, frame, frame_index)
            # Stagger OCR by tracker_id so each vehicle is still read ~once every 15
            # frames, but not all vehicles in the same frame: a busy scene with many
            # vehicles previously ran every plate crop through the OCR back-to-back on
            # a single frame (measured ~650ms stall), starving the capture queue and
            # collapsing end-to-end FPS. Spreading the same total OCR work evenly
            # keeps per-frame cost bounded without changing OCR detection logic.
            for vehicle in formatted:
                if vehicle["tracker_id"] % 15 != frame_index % 15:
                    continue
                plate, confidence = self._read_plate(frame, vehicle)
                if plate:
                    vehicle["plate"] = plate
                    vehicle["confidence"] = confidence
            return formatted


    @staticmethod
    def _format_tracks(tracks: np.ndarray, frame: np.ndarray, frame_index: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for track in tracks:
            x1, y1, x2, y2, tracker_id = track[:5]
            output.append(
                {
                    "tracker_id": int(tracker_id),
                    "x1": max(0, int(x1)),
                    "y1": max(0, int(y1)),
                    "x2": min(frame.shape[1], int(x2)),
                    "y2": min(frame.shape[0], int(y2)),
                    "center": ((float(x1) + float(x2)) / 2.0, (float(y1) + float(y2)) / 2.0),
                    "plate": None,
                    "confidence": 0.0,
                    "frame_index": frame_index,
                }
            )
        return output

    def _read_plate(self, frame: np.ndarray, vehicle: dict[str, Any]) -> tuple[str | None, float]:
        x1, y1, x2, y2 = vehicle["x1"], vehicle["y1"], vehicle["x2"], vehicle["y2"]
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None, 0.0

        plate_result = self.plate_model.predict(crop, conf=0.2, verbose=False)[0]
        plate_box_count = 0 if plate_result.boxes is None else len(plate_result.boxes)
        logger.debug(
            "Plate OCR attempt: tracker_id=%s vehicle_crop=%dx%d plate_boxes=%d",
            vehicle.get("tracker_id"), crop.shape[1], crop.shape[0], plate_box_count,
        )
        if plate_result.boxes is None:
            return None, 0.0

        best: tuple[str | None, float] = (None, 0.0)
        for plate_box in plate_result.boxes:
            px1, py1, px2, py2 = map(int, plate_box.xyxy[0].cpu().tolist())
            plate_crop = crop[max(0, py1):py2, max(0, px1):px2]
            if plate_crop.size == 0:
                continue
            reads = self.reader.readtext(
                cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY),
                detail=1,
                paragraph=False,
                allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            )
            logger.debug("Plate OCR raw reads for tracker_id=%s: %s", vehicle.get("tracker_id"), [(text, round(float(score), 3)) for _, text, score in reads])
            for _, text, score in reads:
                normalized = "".join(character for character in text.upper() if character.isalnum())
                if len(normalized) >= 4 and float(score) >= 0.45 and float(score) > best[1]:
                    best = normalized, float(score)
        return best


class CameraWorker:
    def __init__(
        self,
        camera: dict[str, Any],
        settings: Settings,
        database: Database,
        events: EventHub,
        engine: InferenceEngine,
    ) -> None:
        self.camera = camera
        self.settings = settings
        self.database = database
        self.events = events
        self.engine = engine
        self.stop_event = threading.Event()
        self.frames: queue.Queue[tuple[np.ndarray, float, str]] = queue.Queue(maxsize=1)
        self.capture_thread = threading.Thread(target=self._capture_loop, name=f"camera-read-{camera['id']}", daemon=True)
        self.process_thread = threading.Thread(target=self._process_loop, name=f"camera-ai-{camera['id']}", daemon=True)
        self.tracker = engine.create_tracker()
        self.status_lock = threading.Lock()
        self.status = "OFFLINE"
        self.last_error: str | None = None
        self.last_frame_at: str | None = None
        self.last_reconnect_at: str | None = None
        self.processed_frames = 0
        self._fps_samples: deque[float] = deque(maxlen=30)
        self._inference_ms_samples: deque[float] = deque(maxlen=30)
        self._capture_timestamps: deque[float] = deque(maxlen=60)
        self._source_fps = 0.0
        self._latest_jpeg: bytes | None = None
        self._latest_jpeg_lock = threading.Lock()
        self._vehicle_states: dict[int, dict[str, Any]] = {}
        self._last_observation_write: dict[int, float] = {}

    def start(self) -> None:
        self.capture_thread.start()
        self.process_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.capture_thread.join(timeout=3)
        self.process_thread.join(timeout=3)

    def snapshot(self) -> dict[str, Any]:
        with self.status_lock:
            fps = len(self._fps_samples) / max(0.1, sum(self._fps_samples)) if self._fps_samples else 0.0
            capture_timestamps = list(self._capture_timestamps)
            capture_fps = 0.0
            if len(capture_timestamps) >= 2:
                elapsed = capture_timestamps[-1] - capture_timestamps[0]
                if elapsed > 0:
                    capture_fps = (len(capture_timestamps) - 1) / elapsed
            avg_inference_ms = 1000 * (sum(self._inference_ms_samples) / len(self._inference_ms_samples)) if self._inference_ms_samples else 0.0
            return {
                "camera_id": self.camera["id"],
                "name": self.camera["name"],
                "status": self.status,
                "connected": self.status == "ONLINE",
                "fps": round(fps, 2),
                "source_fps": round(self._source_fps, 2),
                "capture_fps": round(capture_fps, 2),
                "queue_depth": self.frames.qsize(),
                "avg_inference_ms": round(avg_inference_ms, 1),
                "last_reconnect_at": self.last_reconnect_at,
                "last_error": self.last_error,
                "last_frame_at": self.last_frame_at,
                "processed_frames": self.processed_frames,
            }

    def jpeg(self) -> bytes | None:
        with self._latest_jpeg_lock:
            return self._latest_jpeg

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self.status_lock:
            changed = self.status != status or self.last_error != error
            self.status = status
            self.last_error = error
            if status == "RECONNECTING":
                self.last_reconnect_at = utc_now()
        if changed:
            self.events.publish({"type": "camera_status", "camera": self.snapshot()})

    def _capture_loop(self) -> None:
        backoff = 0.5
        while not self.stop_event.is_set():
            self._set_status("RECONNECTING")
            try:
                source_url = CameraManager._resolve_source(self.camera)
                capture = cv2.VideoCapture(
                    source_url,
                    cv2.CAP_FFMPEG,
                    [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                        5000,
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                        5000,
                    ],
                )
            except Exception:
                logger.exception("Could not resolve stream for camera %s", self.camera["id"])
                self._set_status("OFFLINE", "Could not resolve stream; verify ONVIF settings and network.")
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2.0)
                continue
            if not capture.isOpened():
                capture.release()
                self._set_status("OFFLINE", "Stream unavailable; check network, credentials and stream profile.")
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2.0)
                continue
            # Minimize FFmpeg's internal frame buffering so reads reflect the freshest
            # available frame instead of an older queued one; must be set after open()
            # since the FFmpeg backend rejects CAP_PROP_BUFFERSIZE as an open-time param.
            try:
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            self._set_status("ONLINE")
            backoff = 0.5
            try:
                reported_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            except Exception:
                reported_fps = 0.0
            with self.status_lock:
                self._source_fps = reported_fps
                self._capture_timestamps.clear()
            while not self.stop_event.is_set():
                success, frame = capture.read()
                if not success or frame is None:
                    break
                observed_at = utc_now()
                captured_monotonic = time.monotonic()
                with self.status_lock:
                    self.last_frame_at = observed_at
                    self._capture_timestamps.append(captured_monotonic)
                if self.frames.full():
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self.frames.put_nowait((frame, captured_monotonic, observed_at))
                except queue.Full:
                    pass
            capture.release()
            if not self.stop_event.is_set():
                self._set_status("OFFLINE", "Stream disconnected.")
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2.0)
        self._set_status("OFFLINE")

    def _process_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame, captured_at, observed_at = self.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                inference_started = time.monotonic()
                vehicles = self.engine.process(frame, self.tracker, frame_index=self.processed_frames + 1)
                inference_elapsed = time.monotonic() - inference_started
                self._update_vehicle_state(frame, vehicles, observed_at)
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    with self._latest_jpeg_lock:
                        self._latest_jpeg = encoded.tobytes()
                now = time.monotonic()
                with self.status_lock:
                    self.processed_frames += 1
                    self._fps_samples.append(max(0.001, now - started))
                    self._inference_ms_samples.append(inference_elapsed)
                if self.processed_frames % 30 == 0:
                    metrics = self.snapshot()
                    logger.debug(
                        "Camera %s metrics: source_fps=%.2f capture_fps=%.2f processing_fps=%.2f "
                        "avg_inference_ms=%.1f queue_depth=%d processed_frames=%d",
                        self.camera["id"], metrics["source_fps"], metrics["capture_fps"], metrics["fps"],
                        metrics["avg_inference_ms"], metrics["queue_depth"], metrics["processed_frames"],
                    )
                self.events.publish(
                    {
                        "type": "detections",
                        "camera_id": self.camera["id"],
                        "observed_at": observed_at,
                        "latency_ms": round((now - captured_at) * 1000, 1),
                        "vehicles": [
                            {
                                "vehicle_id": f"{self.camera['id']}:{item['tracker_id']}",
                                "plate": self._vehicle_states.get(item["tracker_id"], {}).get("plate"),
                                "confidence": self._vehicle_states.get(item["tracker_id"], {}).get("plate_confidence", 0.0),
                                "status": self._vehicle_states.get(item["tracker_id"], {}).get("status", "MOVING"),
                                "duration_seconds": self._vehicle_states.get(item["tracker_id"], {}).get("duration_seconds", 0.0),
                                "box": {key: item[key] for key in ("x1", "y1", "x2", "y2")},
                            }
                            for item in vehicles
                        ],
                    }
                )
            except Exception:
                logger.exception("Frame processing failed for camera %s", self.camera["id"])
                self._set_status("DEGRADED", "AI frame processing failed; see server logs.")

    def _in_zone(self, point: tuple[float, float]) -> bool:
        zones = self.camera["zones"]
        if not zones:
            return False
        for zone in zones:
            polygon = np.asarray(zone, dtype=np.float32)
            if cv2.pointPolygonTest(polygon, point, False) >= 0:
                return True
        return False

    def _update_vehicle_state(
        self,
        frame: np.ndarray,
        vehicles: list[dict[str, Any]],
        observed_at: str,
    ) -> None:
        observed_seconds = datetime.fromisoformat(observed_at).timestamp()
        seen_ids: set[int] = set()
        for vehicle in vehicles:
            tracker_id = vehicle["tracker_id"]
            seen_ids.add(tracker_id)
            state = self._vehicle_states.get(tracker_id)
            if state is None:
                state = {
                    "previous_center": vehicle["center"],
                    "current_center": vehicle["center"],
                    "stationary_start": None,
                    "stationary_count": 0,
                    "duration_seconds": 0.0,
                    "status": "MOVING",
                    "first_seen": observed_at,
                    "last_seen": observed_at,
                    "plate": None,
                    "plate_confidence": 0.0,
                    "plate_votes": {},
                }
                self._vehicle_states[tracker_id] = state

            previous_center = state["current_center"]
            current_center = vehicle["center"]
            movement = float(np.hypot(current_center[0] - previous_center[0], current_center[1] - previous_center[1]))
            state["previous_center"] = previous_center
            state["current_center"] = current_center
            state["last_seen"] = observed_at
            if vehicle.get("plate"):
                plate = vehicle["plate"]
                score = float(vehicle.get("confidence", 0.0))
                candidate = state["plate_votes"].setdefault(plate, {"count": 0, "score": 0.0})
                candidate["count"] += 1
                candidate["score"] += score
                if candidate["count"] >= 2 or score >= 0.85:
                    best_plate, best_candidate = max(
                        state["plate_votes"].items(),
                        key=lambda item: (item[1]["count"], item[1]["score"]),
                    )
                    state["plate"] = best_plate
                    state["plate_confidence"] = best_candidate["score"] / best_candidate["count"]

            in_zone = self._in_zone((float(current_center[0]), float(vehicle["y2"])))
            if not in_zone or movement > 12.0:
                state["stationary_count"] = 0
                state["stationary_start"] = None
                state["duration_seconds"] = 0.0
                state["status"] = "MOVING"
            else:
                state["stationary_count"] += 1
                if state["stationary_count"] >= 5:
                    if state["stationary_start"] is None:
                        state["stationary_start"] = observed_seconds
                    state["duration_seconds"] = max(0.0, observed_seconds - state["stationary_start"])
                    state["status"] = "OVER_LIMIT" if state["duration_seconds"] >= self.camera["parking_limit_seconds"] else "STATIONARY"

            vehicle_id = f"{self.camera['id']}:{tracker_id}"
            self._persist_observation(tracker_id, vehicle_id, vehicle, state, observed_at)
            self._persist_session(tracker_id, vehicle_id, state)
            if state["status"] == "OVER_LIMIT" and not state.get("incident_created"):
                self._create_incident(frame, vehicle_id, vehicle, state, observed_at)
                state["incident_created"] = True

        for tracker_id, state in list(self._vehicle_states.items()):
            if tracker_id not in seen_ids and time.time() - datetime.fromisoformat(state["last_seen"]).timestamp() > 120:
                self._vehicle_states.pop(tracker_id, None)

    def _persist_observation(
        self,
        tracker_id: int,
        vehicle_id: str,
        vehicle: dict[str, Any],
        state: dict[str, Any],
        observed_at: str,
    ) -> None:
        now = time.monotonic()
        if now - self._last_observation_write.get(tracker_id, 0.0) < 1.0:
            return
        self._last_observation_write[tracker_id] = now
        self.database.execute(
            """INSERT INTO vehicle_observations(id, camera_id, vehicle_id, plate, confidence, observed_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                self.camera["id"],
                vehicle_id,
                state.get("plate"),
                float(vehicle.get("confidence", 0.0)),
                observed_at,
                json.dumps(
                    {
                        "status": state["status"],
                        "duration_seconds": state["duration_seconds"],
                        "box": {key: vehicle[key] for key in ("x1", "y1", "x2", "y2")},
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        self.events.publish(
            {
                "type": "vehicle_observation",
                "camera_id": self.camera["id"],
                "vehicle_id": vehicle_id,
                "plate": state.get("plate"),
                "confidence": float(vehicle.get("confidence", 0.0)),
                "timestamp": observed_at,
            }
        )

    def _persist_session(self, tracker_id: int, vehicle_id: str, state: dict[str, Any]) -> None:
        self.database.execute(
            """INSERT INTO parking_sessions(id, camera_id, vehicle_id, plate, first_seen, last_seen, duration_seconds, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(camera_id, vehicle_id) DO UPDATE SET
                 plate=excluded.plate, last_seen=excluded.last_seen,
                 duration_seconds=excluded.duration_seconds, status=excluded.status""",
            (
                f"{self.camera['id']}:{tracker_id}",
                self.camera["id"],
                vehicle_id,
                state.get("plate"),
                state["first_seen"],
                state["last_seen"],
                state["duration_seconds"],
                state["status"],
            ),
        )

    def _create_incident(
        self,
        frame: np.ndarray,
        vehicle_id: str,
        vehicle: dict[str, Any],
        state: dict[str, Any],
        observed_at: str,
    ) -> None:
        incident_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())
        evidence_path = self.settings.evidence_dir / f"{evidence_id}.jpg"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        x1, y1, x2, y2 = vehicle["x1"], vehicle["y1"], vehicle["x2"], vehicle["y2"]
        evidence_frame = frame.copy()
        cv2.rectangle(evidence_frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        if not cv2.imwrite(str(evidence_path), evidence_frame):
            logger.error("Failed to store evidence for camera %s", self.camera["id"])
            evidence_id = None
        else:
            self.database.execute(
                "INSERT INTO evidence(id, camera_id, path, content_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (evidence_id, self.camera["id"], str(evidence_path), "image/jpeg", observed_at),
            )

        self.database.execute(
            """INSERT INTO incidents(id, camera_id, vehicle_id, plate, timestamp, duration_seconds,
               rule, confidence, status, evidence_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                incident_id,
                self.camera["id"],
                vehicle_id,
                state.get("plate"),
                observed_at,
                state["duration_seconds"],
                "PARKING_DURATION_LIMIT",
                float(vehicle.get("confidence", 0.0)),
                evidence_id,
                observed_at,
                observed_at,
            ),
        )
        self.events.publish(
            {
                "type": "incident",
                "incident_id": incident_id,
                "camera_id": self.camera["id"],
                "vehicle_id": vehicle_id,
                "timestamp": observed_at,
                "status": "PENDING",
            }
        )


class CameraManager:
    def __init__(self, settings: Settings, database: Database, events: EventHub) -> None:
        self.settings = settings
        self.database = database
        self.events = events
        self._lock = threading.RLock()
        self._workers: dict[str, CameraWorker] = {}
        self._runtime_configs: dict[str, dict[str, Any]] = {}
        self._engine: InferenceEngine | None = None
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        self._ensure_engine()
        with self._lock:
            if self._running:
                return
            self._running = True
            camera_rows = self.database.fetch_all("SELECT * FROM cameras WHERE enabled = 1 ORDER BY created_at")
        for row in camera_rows:
            try:
                self._start_worker(self._runtime_config(row))
            except Exception:
                logger.exception("Could not start camera worker %s", row["id"])
        self.events.publish({"type": "system_status", "running": True})

    def stop(self) -> None:
        with self._lock:
            self._running = False
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()
        self.events.publish({"type": "system_status", "running": False})

    def reload_camera(self, camera_id: str) -> None:
        row = self.database.fetch_one("SELECT * FROM cameras WHERE id = ?", (camera_id,))
        with self._lock:
            old_worker = self._workers.pop(camera_id, None)
            self._runtime_configs.pop(camera_id, None)
            running = self._running
        if old_worker:
            old_worker.stop()
        if row and row["enabled"] and running:
            self._start_worker(self._runtime_config(row))

    def remove_camera(self, camera_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
            self._runtime_configs.pop(camera_id, None)
        if worker:
            worker.stop()

    def camera_statuses(self) -> list[dict[str, Any]]:
        cameras = self.database.fetch_all("SELECT id, name, source_type, enabled FROM cameras ORDER BY created_at")
        with self._lock:
            workers = dict(self._workers)
        result: list[dict[str, Any]] = []
        for camera in cameras:
            worker = workers.get(camera["id"])
            result.append(
                worker.snapshot()
                if worker
                else {
                    "camera_id": camera["id"],
                    "name": camera["name"],
                    "status": "STOPPED" if not self.running else "OFFLINE",
                    "connected": False,
                    "fps": 0.0,
                    "last_reconnect_at": None,
                    "last_error": "Camera disabled." if not camera["enabled"] else None,
                    "last_frame_at": None,
                    "processed_frames": 0,
                }
            )
        return result

    def latest_jpeg(self, camera_id: str) -> bytes | None:
        with self._lock:
            worker = self._workers.get(camera_id)
        return worker.jpeg() if worker else None

    def _ensure_engine(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
        engine = InferenceEngine()
        with self._lock:
            if self._engine is None:
                self._engine = engine

    def _start_worker(self, camera: dict[str, Any]) -> None:
        self._ensure_engine()
        worker = CameraWorker(camera, self.settings, self.database, self.events, self._engine)
        with self._lock:
            self._workers[camera["id"]] = worker
            self._runtime_configs[camera["id"]] = camera
        worker.start()

    def _runtime_config(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "source_type": row["source_type"],
            "url": row["url"],
            "host": row["host"],
            "port": row["port"],
            "profile": row["profile"],
            "username": decrypt_secret(row["username_enc"], self.settings.camera_encryption_key),
            "password": decrypt_secret(row["password_enc"], self.settings.camera_encryption_key),
            "zones": json.loads(row["zones_json"]),
            "parking_limit_seconds": row["parking_limit_seconds"],
        }

    @staticmethod
    def _resolve_source(camera: dict[str, Any]) -> str:
        if camera["source_type"] != "onvif":
            return _add_credentials(camera["url"], camera["username"], camera["password"])

        client = ONVIFCamera(
            camera["host"],
            camera["port"] or 80,
            camera["username"],
            camera["password"],
            timeout=5,
        )
        media = client.create_media_service()
        profiles = media.GetProfiles()
        selected = next(
            (item for item in profiles if item.token == camera["profile"] or getattr(item, "Name", "") == camera["profile"]),
            profiles[0] if profiles else None,
        )
        if selected is None:
            raise RuntimeError("The ONVIF device has no media profiles.")
        request = media.create_type("GetStreamUri")
        request.ProfileToken = selected.token
        request.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        response = media.GetStreamUri(request)
        return _add_credentials(response.Uri, camera["username"], camera["password"])


def _add_credentials(url: str, username: str, password: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"rtsp", "rtsps", "http", "https"}:
        raise ValueError("Camera URL scheme must be RTSP, RTSPS, HTTP or HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("Store camera credentials in their dedicated fields, not in the URL.")
    if not username and not password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def test_camera_connection(camera: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        url = CameraManager._resolve_source(camera)
        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            return {"ok": False, "code": "UNAVAILABLE", "message": "Stream unavailable; verify address, credentials and network."}
        success, frame = capture.read()
        capture.release()
        if not success or frame is None:
            return {"ok": False, "code": "FORMAT_INCOMPATIBLE", "message": "Connected, but no decodable video frame was received."}
        return {
            "ok": True,
            "code": "OK",
            "message": "Camera stream connected and a frame was decoded.",
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except (ValueError, InvalidToken) as exc:
        return {"ok": False, "code": "URL_INVALID", "message": str(exc)}
    except TimeoutError:
        return {"ok": False, "code": "TIMEOUT", "message": "Camera connection timed out."}
    except Exception:
        logger.exception("Camera connection test failed")
        return {"ok": False, "code": "UNAVAILABLE", "message": "Could not connect; verify ONVIF settings, credentials and network."}
