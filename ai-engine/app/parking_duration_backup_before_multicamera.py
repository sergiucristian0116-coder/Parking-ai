
from ultralytics import YOLO
import cv2 
import json
import os
import numpy as np
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import easyocr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None


# ============================================================
# 1. CĂILE PROIECTULUI
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

LIVE_STREAM_URL = "https://s5.webcamromania.ro/piataromanab/tracks-v1/mono.m3u8"
INPUT_VIDEO = LIVE_STREAM_URL
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_VIDEO = OUTPUT_DIR / "parking_ai_test_strada.mp4"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. SETĂRI
# ============================================================

# Pentru test folosim 30 secunde.
# În sistemul final va fi:
# PARKING_LIMIT_SECONDS = 15 * 60
PARKING_LIMIT_SECONDS = 15

VEHICLE_CLASSES = [2, 3, 5, 7]
PLATE_MODEL_PATH = BASE_DIR / "license-plate-finetune-v1n.pt"

# 2 = car
# 3 = motorcycle
# 5 = bus
# 7 = truck


# ============================================================
# 3. MODELE YOLO
# ============================================================

MODEL_PATH = PROJECT_DIR / "yolo11n.pt"
YOLO_DEVICE = "cuda:0"

model = YOLO(str(MODEL_PATH)).to(YOLO_DEVICE)
plate_model = YOLO(str(PLATE_MODEL_PATH)).to(YOLO_DEVICE)

try:
    plate_reader = easyocr.Reader(["en"], gpu=True)
except Exception as exc:
    plate_reader = None
    print(f"[WARN] EasyOCR nu a putut fi inițializat: {exc}")


# ============================================================
# 4. VIDEO
# ============================================================

cap = cv2.VideoCapture(str(INPUT_VIDEO))

if not cap.isOpened():
    raise RuntimeError(
        f"Nu pot deschide videoclipul: {INPUT_VIDEO}"
    )

fps = cap.get(cv2.CAP_PROP_FPS)

if fps <= 0:
    fps = 30.0

width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

print(f"Video: {width}x{height}")
print(f"FPS: {fps}")
print(f"Limita test: {PARKING_LIMIT_SECONDS} secunde")


display_root = None
display_label = None
display_photo = None
stop_requested = False
DISPLAY_UPDATE_INTERVAL_FRAMES = 3
last_display_update_frame = 0
last_display_update_time = 0.0
DISPLAY_UPDATE_INTERVAL_SECONDS = 0.25
DISPLAY_REFRESH_INTERVAL_MS = 250
latest_display_frame = None
latest_display_frame_lock = threading.Lock()
LIVE_PREVIEW_PORT = 8765
LIVE_PREVIEW_MAX_FPS = 18.0
live_preview_lock = threading.Lock()
last_live_preview_frame = None
last_live_preview_frame_time = 0.0
live_preview_server = None
DISPLAY_ENABLED = os.environ.get("PARKINGAI_HEADLESS", "0") != "1"


class LivePreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        requested_path = self.path.split("?", 1)[0]
        if requested_path not in ("/", "/snapshot"):
            self.send_response(404)
            self.end_headers()
            return

        with live_preview_lock:
            frame_bytes = last_live_preview_frame

        if frame_bytes is None:
            self.send_response(503)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(frame_bytes)))
        self.end_headers()
        self.wfile.write(frame_bytes)
        self.wfile.flush()

    def log_message(self, format, *args):
        print(f"[LIVE_PREVIEW] {format % args}", file=sys.stderr)


def start_live_preview_server():
    global live_preview_server

    if live_preview_server is not None:
        return

    try:
        live_preview_server = ThreadingHTTPServer(("127.0.0.1", LIVE_PREVIEW_PORT), LivePreviewHandler)
        preview_thread = threading.Thread(target=live_preview_server.serve_forever, daemon=True)
        preview_thread.start()
    except Exception as exc:
        print(f"[WARN] Live preview stream unavailable: {exc}", file=sys.stderr)
        live_preview_server = None


def publish_live_preview_frame(frame):
    global last_live_preview_frame, last_live_preview_frame_time

    if frame is None:
        return

    now = time.perf_counter()
    if now - last_live_preview_frame_time < (1.0 / LIVE_PREVIEW_MAX_FPS):
        return

    last_live_preview_frame_time = now
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        return

    with live_preview_lock:
        last_live_preview_frame = encoded.tobytes()


def request_stop():
    global stop_requested
    stop_requested = True
    if display_root is not None and threading.current_thread() is threading.main_thread():
        try:
            display_root.quit()
            display_root.destroy()
        except Exception:
            pass


def queue_display_frame(frame):
    global latest_display_frame

    if frame is None:
        return

    with latest_display_frame_lock:
        latest_display_frame = frame


def refresh_display_tick():
    global latest_display_frame

    if display_root is None or display_label is None:
        return

    with latest_display_frame_lock:
        frame = latest_display_frame

    if frame is not None:
        update_display_window(frame)

    if display_root is not None:
        display_root.after(DISPLAY_REFRESH_INTERVAL_MS, refresh_display_tick)


def setup_display_window():
    global display_root, display_label, display_photo

    if display_root is not None:
        return

    try:
        display_root = tk.Tk()
        display_root.title("ParkingAI")
        display_root.geometry(f"{width}x{height}")
        display_root.bind("<KeyPress-q>", lambda event: request_stop())
        display_root.bind("<KeyPress-Q>", lambda event: request_stop())
        display_root.protocol("WM_DELETE_WINDOW", request_stop)

        display_label = tk.Label(display_root)
        display_label.pack()
        display_photo = tk.PhotoImage(width=width, height=height)
        display_label.configure(image=display_photo)
        display_label.image = display_photo
        display_root.after(DISPLAY_REFRESH_INTERVAL_MS, refresh_display_tick)
    except Exception as exc:
        print(f"[WARN] Fereastra Tkinter nu este disponibilă: {exc}")
        display_root = None
        display_label = None
        display_photo = None


def update_display_window(frame):
    global display_root, display_label, display_photo

    if display_root is None or display_label is None:
        return

    try:
        target_width = 1280
        target_height = 720
        display_scale = min(1.0, target_width / max(1, frame.shape[1]), target_height / max(1, frame.shape[0]))

        if display_scale < 1.0:
            display_w = max(1, int(frame.shape[1] * display_scale))
            display_h = max(1, int(frame.shape[0] * display_scale))
            display_frame = cv2.resize(frame, (display_w, display_h), interpolation=cv2.INTER_AREA)
        else:
            display_frame = frame

        rgb_frame = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)

        if Image is not None and ImageTk is not None:
            pil_image = Image.fromarray(rgb_frame)
            new_photo = ImageTk.PhotoImage(image=pil_image)
        else:
            new_photo = tk.PhotoImage(data=cv2.imencode('.png', rgb_frame)[1].tobytes())

        display_label.configure(image=new_photo)
        display_label.image = new_photo
        display_photo = new_photo
    except Exception as exc:
        print(f"[WARN] Actualizarea ferestrei Tkinter a eșuat: {exc}")
        display_root = None
        display_label = None
        display_photo = None


# ============================================================
# 5. ZONA DE PARCARE
# ============================================================

PARKING_ZONE = np.array([
    [1750, 470],
    [2550, 520],
    [2550, 1050],
    [1650, 850]
], dtype=np.int32)


# ============================================================
# 6. FUNCȚIE - PUNCT ÎN ZONĂ
# ============================================================

def point_inside_zone(x, y):
    result = cv2.pointPolygonTest(
        PARKING_ZONE,
        (float(x), float(y)),
        False
    )

    return result >= 0


# ============================================================
# 7. VIDEO OUTPUT
# ============================================================

out = None


# ============================================================
# 8. TIMER PENTRU FIECARE VEHICUL
# ============================================================

parking_start = {}
reported_ids = set()
vehicle_plate_history = {}
vehicle_plate_cache = {}
vehicle_last_position = {}
parking_events = []
vehicle_memory = {}
raw_tracking_to_vehicle = {}
next_vehicle_id = 1
MAX_STALE_FRAMES = 600
STATIONARY_DISTANCE_THRESHOLD_PIXELS = 12.0
STATIONARY_CONFIRM_FRAMES = 5
STATIONARY_MOVEMENT_THRESHOLD = 10.0
OCR_INTERVAL_FRAMES = 10
PLATE_SCAN_INTERVAL_FRAMES = 15
PLATE_RETRY_INTERVAL_FRAMES = 60
PROFILE_WINDOW_SECONDS = 3.0
last_vehicle_json_emit_time = 0.0
last_history_json_emit_time = 0.0


def normalize_export_status(status_text):
    if status_text == "DEPĂȘIRE LIMITĂ":
        return "OVER_LIMIT"
    if status_text == "STAȚIONAR":
        return "STATIONARY"
    return "MOVING"


def format_history_timestamp(seconds_value):
    if seconds_value is None:
        return None

    try:
        total_seconds = max(0.0, float(seconds_value))
    except (TypeError, ValueError):
        return None

    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    secs = int(total_seconds % 60)

    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_vehicle_history_snapshot(stable_id, memory):
    if not isinstance(memory, dict):
        return None

    plate_value = memory.get("plate") or memory.get("last_valid_plate")
    first_seen_frame = memory.get("first_seen_frame")
    last_seen_frame = memory.get("last_seen_frame")
    first_seen = None if first_seen_frame is None else float(first_seen_frame) / max(1.0, fps)
    last_seen = None if last_seen_frame is None else float(last_seen_frame) / max(1.0, fps)
    stationary_duration = memory.get("duration_seconds")
    over_limit_at = memory.get("over_limit_at")
    status_value = memory.get("status")
    status_label = "DEPĂȘIRE LIMITĂ" if status_value == "DEPĂȘIRE LIMITĂ" else "ÎN LIMITĂ"
    observation_count = int(memory.get("observation_count") or 0)

    return {
        "vehicle_id": int(stable_id),
        "plate": plate_value or None,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "stationary_duration": stationary_duration,
        "over_limit_at": over_limit_at,
        "status": status_label,
        "observation_count": observation_count,
    }


def emit_vehicle_history_snapshot():
    global last_history_json_emit_time

    now = time.perf_counter()
    if now - last_history_json_emit_time < 0.5:
        return

    last_history_json_emit_time = now
    snapshot = []

    for stable_id, memory in vehicle_memory.items():
        history_entry = build_vehicle_history_snapshot(stable_id, memory)
        if history_entry is not None:
            snapshot.append(history_entry)

    if not snapshot:
        return

    payload = {"type": "history", "history": snapshot}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_active_vehicle_snapshot(vehicle_boxes_for_ui):
    global last_vehicle_json_emit_time

    if not vehicle_boxes_for_ui:
        return

    now = time.perf_counter()
    if now - last_vehicle_json_emit_time < 0.25:
        return

    last_vehicle_json_emit_time = now
    for _, _, _, _, vehicle_id, duration, status_text, plate_text in vehicle_boxes_for_ui:
        if vehicle_id is None:
            continue

        plate_value = plate_text if plate_text and plate_text != "NECITIT" else "N/A"
        sanitized_plate = sanitize_plate_text(plate_value)
        if sanitized_plate:
            plate_value = sanitized_plate
        else:
            plate_value = "N/A"

        payload = {
            "id": int(vehicle_id),
            "plate": plate_value,
            "timer": format_time(float(duration)),
            "status": normalize_export_status(status_text),
        }

        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def emit_active_vehicle_boxes(vehicle_boxes_for_ui):
    if not vehicle_boxes_for_ui:
        return

    boxes = []
    for x1, y1, x2, y2, vehicle_id, _, status_text, _ in vehicle_boxes_for_ui:
        if vehicle_id is None:
            continue
        boxes.append({
            "id": int(vehicle_id),
            "x1": int(x1),
            "y1": int(y1),
            "x2": int(x2),
            "y2": int(y2),
            "status": normalize_export_status(status_text),
        })

    if not boxes:
        return

    payload = {"type": "boxes", "boxes": boxes}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()
profile_window_start = time.perf_counter()
profile_totals = {
    "yolo_tracking": 0.0,
    "plate_detector": 0.0,
    "easyocr": 0.0,
    "process_display": 0.0,
    "frame_read": 0.0,
    "loop_total": 0.0,
    "wait_block": 0.0,
}
profile_counts = {
    "yolo_tracking": 0,
    "plate_detector": 0,
    "easyocr": 0,
    "process_display": 0,
    "frame_read": 0,
    "loop_total": 0,
    "wait_block": 0,
}


# ============================================================
# 9. FUNCȚII PENTRU OCR PLĂCUȚĂ
# ============================================================

def sanitize_plate_text(raw_text):
    if raw_text is None:
        return ""

    cleaned = "".join(ch for ch in str(raw_text).strip() if ch.isalnum())
    cleaned = cleaned.upper()

    if len(cleaned) < 4:
        return ""

    return cleaned


def read_plate_crop(plate_crop):
    if plate_crop is None or plate_crop.size == 0:
        return "NECITIT", 0.0

    if plate_reader is None:
        return "NECITIT", 0.0

    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
    if gray.shape[1] < 80:
        gray = cv2.resize(
            gray,
            None,
            fx=2.5,
            fy=2.5,
            interpolation=cv2.INTER_CUBIC
        )

    ocr_start = time.perf_counter()
    results = plate_reader.readtext(
        gray,
        detail=1,
        paragraph=False,
        allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    profile_totals["easyocr"] += time.perf_counter() - ocr_start
    profile_counts["easyocr"] += 1

    if not results:
        return "NECITIT", 0.0

    best_text = "NECITIT"
    best_confidence = 0.0

    for _, text, confidence in results:
        cleaned_text = sanitize_plate_text(text)
        if not cleaned_text:
            continue

        confidence_value = float(confidence)
        if confidence_value > best_confidence:
            best_text = cleaned_text
            best_confidence = confidence_value

    if best_confidence >= 0.28 and len(best_text) >= 4:
        return best_text, best_confidence

    return "NECITIT", best_confidence


def stabilize_plate_reading(vehicle_id, plate_text, confidence):
    history = vehicle_plate_history.setdefault(vehicle_id, [])

    if plate_text is None or plate_text == "NECITIT":
        history.append(("NECITIT", 0.0))
    else:
        history.append((plate_text, float(confidence)))

    if len(history) > 12:
        history.pop(0)

    valid_reads = [
        (text, value)
        for text, value in history
        if text and text != "NECITIT"
    ]

    if valid_reads:
        vote = {}

        for text, value in valid_reads:
            if text not in vote:
                vote[text] = {"count": 0, "score": 0.0}

            vote[text]["count"] += 1
            vote[text]["score"] += float(value)

        best_plate, best_stats = max(
            vote.items(),
            key=lambda item: (item[1]["count"], item[1]["score"])
        )

        average_confidence = best_stats["score"] / best_stats["count"]

        if best_stats["count"] >= 2 or average_confidence >= 0.55:
            vehicle_plate_cache[vehicle_id] = best_plate
            return best_plate, average_confidence

    if vehicle_id in vehicle_plate_cache:
        cached_plate = vehicle_plate_cache[vehicle_id]
        return cached_plate, float(confidence)

    return "NECITIT", float(confidence)


def detect_plate_for_vehicle(frame, vehicle_box):
    x1, y1, x2, y2 = map(int, vehicle_box)

    padding = 40
    crop_x1 = max(0, x1 - padding)
    crop_y1 = max(0, y1 - padding)
    crop_x2 = min(frame.shape[1], x2 + padding)
    crop_y2 = min(frame.shape[0], y2 + padding)

    vehicle_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]

    if vehicle_crop.size == 0:
        return "NECITIT", 0.0

    try:
        plate_start = time.perf_counter()
        plate_results = plate_model(
            vehicle_crop,
            conf=0.20,
            verbose=False
        )
        profile_totals["plate_detector"] += time.perf_counter() - plate_start
        profile_counts["plate_detector"] += 1
    except Exception:
        return "NECITIT", 0.0

    best_plate = "NECITIT"
    best_confidence = 0.0

    for plate_result in plate_results:
        if plate_result.boxes is None:
            continue

        for plate_box in plate_result.boxes.xyxy.cpu().tolist():
            px1, py1, px2, py2 = map(int, plate_box)

            if px2 <= px1 or py2 <= py1:
                continue

            plate_crop = vehicle_crop[py1:py2, px1:px2]

            if plate_crop.size == 0:
                continue

            candidate_text, candidate_conf = read_plate_crop(plate_crop)

            if candidate_text != "NECITIT" and candidate_conf > best_confidence:
                best_plate = candidate_text
                best_confidence = candidate_conf

    return best_plate, best_confidence


# ============================================================
# 10. FUNCȚII PENTRU INTERFAȚĂ
# ============================================================

def draw_panel(
    image,
    x1,
    y1,
    x2,
    y2,
    title,
    lines
):
    """
    Desenează un panou negru semi-transparent.
    """

    overlay = image.copy()

    cv2.rectangle(
        overlay,
        (x1, y1),
        (x2, y2),
        (20, 20, 20),
        -1
    )

    image[:] = cv2.addWeighted(
        overlay,
        0.82,
        image,
        0.18,
        0
    )

    cv2.rectangle(
        image,
        (x1, y1),
        (x2, y2),
        (255, 255, 255),
        2
    )

    cv2.putText(
        image,
        title,
        (x1 + 20, y1 + 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2
    )

    y = y1 + 78

    for text, color in lines:

        cv2.putText(
            image,
            text,
            (x1 + 20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            color,
            2
        )

        y += 34


def format_time(seconds):
    """
    Transformă secundele în MM:SS.
    """

    minutes = int(seconds // 60)
    secs = int(seconds % 60)

    return f"{minutes:02d}:{secs:02d}"


def format_plate_display(plate_text):
    """Formatează un număr de mașină ca exemplu: CB 5438 XP."""
    if plate_text is None:
        return "NECITIT"

    clean = sanitize_plate_text(plate_text)
    if not clean:
        return "NECITIT"

    if len(clean) >= 8:
        match = re.match(r"^([A-Z]{2})([0-9]{4})([A-Z]{2,3})$", clean)
        if match:
            return f"{match.group(1)} {match.group(2)} {match.group(3)}"

        match = re.match(r"^([A-Z]{2})([0-9]{3,4})([A-Z]{2,3})$", clean)
        if match:
            return f"{match.group(1)} {match.group(2)} {match.group(3)}"

    if len(clean) >= 6:
        prefix = clean[:2]
        middle = clean[2:-2]
        suffix = clean[-2:]
        if middle.isdigit() or suffix.isalpha():
            return f"{prefix} {middle} {suffix}"

    return clean[:2] + " " + clean[2:]


def compute_visual_signature(frame, box):
    x1, y1, x2, y2 = map(int, box)
    roi = frame[max(0, y1):min(frame.shape[0], y2), max(0, x1):min(frame.shape[1], x2)]

    if roi.size == 0:
        return {"area": 0.0, "aspect": 1.0, "color": (0, 0, 0)}

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mean_color = tuple(np.round(np.mean(hsv, axis=(0, 1))[:3]).astype(float))
    area = float((x2 - x1) * (y2 - y1))
    aspect = float((x2 - x1) / max(1, y2 - y1))

    return {"area": area, "aspect": aspect, "color": mean_color}


def visual_similarity(signature_a, signature_b):
    if signature_a is None or signature_b is None:
        return 1e9

    color_d = np.linalg.norm(np.array(signature_a["color"]) - np.array(signature_b["color"]))
    area_d = abs(signature_a["area"] - signature_b["area"]) / max(1.0, max(signature_a["area"], signature_b["area"]))
    aspect_d = abs(signature_a["aspect"] - signature_b["aspect"])

    return float(color_d * 0.02 + area_d * 20.0 + aspect_d * 5.0)


def match_vehicle_to_memory(raw_id, box, frame, plate_text):
    global next_vehicle_id

    plate = plate_text if plate_text and plate_text != "NECITIT" else None

    try:
        raw_id_value = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        raw_id_value = None

    center_x = float((box[0] + box[2]) / 2.0)
    center_y = float((box[1] + box[3]) / 2.0)
    current_center = (center_x, center_y)

    if raw_id_value is not None:
        existing_stable_id = raw_tracking_to_vehicle.get(raw_id_value)
        if existing_stable_id is not None and existing_stable_id in vehicle_memory:
            memory = vehicle_memory[existing_stable_id]
            memory["raw_id"] = raw_id_value
            memory["raw_ids"] = {raw_id_value}
            memory["last_seen_frame"] = frame_number
            memory["last_center"] = current_center
            memory["previous_center"] = memory.get("last_center", current_center)
            memory["last_position"] = current_center
            if plate:
                memory["last_valid_plate"] = plate
            return existing_stable_id

    stable_id = next_vehicle_id
    next_vehicle_id += 1

    if raw_id_value is not None:
        raw_tracking_to_vehicle[raw_id_value] = stable_id

    vehicle_memory[stable_id] = {
        "raw_id": raw_id_value,
        "raw_ids": {raw_id_value} if raw_id_value is not None else set(),
        "plate": plate,
        "last_valid_plate": plate,
        "status": "MIȘCARE",
        "is_stationary": False,
        "stationary_confirm_count": 0,
        "stationary_confirm_frames": 0,
        "stationary_start_frame": None,
        "duration_seconds": 0.0,
        "first_seen_frame": frame_number,
        "last_seen_frame": frame_number,
        "last_center": current_center,
        "previous_center": current_center,
        "last_position": current_center,
        "signature": compute_visual_signature(frame, box),
        "exceeded_limit": False,
        "over_limit_at": None,
        "observation_count": 0,
        "last_ocr_frame": None,
        "last_plate_scan_frame": None,
    }

    return stable_id


def get_safe_panel_rect(x1, y1, x2, y2, occupied_rects, panel_width=255, panel_height=118):
    """Alege o poziție pentru panoul de informații fără suprapunere."""
    margin = 10
    box_w = max(40, x2 - x1)
    box_h = max(40, y2 - y1)

    candidates = []
    offsets = [
        (-panel_width - 12, -panel_height - 12),
        (box_w + 12, -panel_height - 12),
        (-panel_width - 12, box_h + 12),
        (box_w + 12, box_h + 12),
        (0, -panel_height - 12),
        (0, box_h + 12),
        (-panel_width - 12, 0),
        (box_w + 12, 0),
        (int((box_w - panel_width) / 2), -panel_height - 12),
        (int((box_w - panel_width) / 2), box_h + 12),
    ]

    for dx, dy in offsets:
        px1 = x1 + dx
        py1 = y1 + dy
        px2 = px1 + panel_width
        py2 = py1 + panel_height

        if px1 < 10:
            px1 = 10
        if py1 < 10:
            py1 = 10
        if px2 > width - 10:
            px2 = width - 10
            px1 = max(10, px2 - panel_width)
        if py2 > height - 10:
            py2 = height - 10
            py1 = max(10, py2 - panel_height)

        candidates.append((int(px1), int(py1), int(px2), int(py2)))

    for px1, py1, px2, py2 in candidates:
        fits = True
        for bx1, by1, bx2, by2 in occupied_rects:
            if bx1 < px2 - margin and bx2 > px1 + margin and by1 < py2 - margin and by2 > py1 + margin:
                fits = False
                break

        if fits:
            return px1, py1, px2, py2

    return int(max(10, x1)), int(max(10, y1 - panel_height)), int(min(width - 10, x1 + panel_width)), int(min(height - 10, y1))


def log_profile_summary():
    global profile_window_start

    now = time.perf_counter()
    if now - profile_window_start < PROFILE_WINDOW_SECONDS:
        return

    print("[PROFILE] Average over last %.1f s" % PROFILE_WINDOW_SECONDS)
    for name in ("frame_read", "loop_total", "wait_block", "yolo_tracking", "plate_detector", "easyocr", "process_display"):
        total = profile_totals[name]
        count = profile_counts[name]
        avg = (total / count) if count else 0.0
        print(f"  - {name}: avg={avg:.4f}s, count={count}")

    profile_window_start = now
    for key in profile_totals:
        profile_totals[key] = 0.0
    for key in profile_counts:
        profile_counts[key] = 0


def draw_vehicle_box(
    image,
    x1,
    y1,
    x2,
    y2,
    vehicle_id,
    duration,
    status_text,
    plate_text="NECITIT",
    occupied_rects=None
):
    """Desenează informația compactă pentru un singur vehicul."""

    if occupied_rects is None:
        occupied_rects = []

    if status_text == "DEPĂȘIRE LIMITĂ":
        box_color = (0, 0, 255)
        status_color = (0, 0, 255)
    elif status_text == "STAȚIONAR":
        box_color = (0, 255, 0)
        status_color = (0, 255, 0)
    else:
        box_color = (255, 180, 0)
        status_color = (255, 180, 0)

    cv2.rectangle(
        image,
        (x1, y1),
        (x2, y2),
        box_color,
        4
    )

    panel_width = 255
    panel_height = 118
    panel_x1, panel_y1, panel_x2, panel_y2 = get_safe_panel_rect(
        x1,
        y1,
        x2,
        y2,
        occupied_rects,
        panel_width,
        panel_height
    )

    cv2.rectangle(
        image,
        (panel_x1, panel_y1),
        (panel_x2, panel_y2),
        (10, 10, 10),
        -1
    )

    cv2.rectangle(
        image,
        (panel_x1, panel_y1),
        (panel_x2, panel_y2),
        box_color,
        3
    )

    cv2.putText(
        image,
        f"ID {vehicle_id}",
        (panel_x1 + 12, panel_y1 + 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2
    )

    display_plate = plate_text if plate_text and plate_text != "NECITIT" else "NECITIT"
    displayed_plate = format_plate_display(display_plate)
    plate_color = (255, 255, 255) if display_plate != "NECITIT" else (255, 120, 120)
    cv2.putText(
        image,
        f"PLACA: {displayed_plate}",
        (panel_x1 + 12, panel_y1 + 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.46,
        plate_color,
        2
    )

    cv2.putText(
        image,
        format_time(duration),
        (panel_x1 + 12, panel_y1 + 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.86,
        box_color,
        2
    )

    if status_text == "DEPĂȘIRE LIMITĂ":
        cv2.putText(
            image,
            status_text,
            (panel_x1 + 115, panel_y1 + 78),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            status_color,
            2
        )

    return (panel_x1, panel_y1, panel_x2, panel_y2)


# ============================================================
# 11. PROCESAREA VIDEO
# ============================================================

def run_ai_loop():
    global frame_number, fps_frames, fps_last_log_time

    frame_number = 0
    fps_frames = 0
    fps_last_log_time = time.perf_counter()

    while True:

        loop_start = time.perf_counter()

        if stop_requested:
            break

        frame_read_start = time.perf_counter()
        success, frame = cap.read()
        profile_totals["frame_read"] += time.perf_counter() - frame_read_start
        profile_counts["frame_read"] += 1

        if not success:
            break

        frame_number += 1
        fps_frames += 1

        now = time.perf_counter()
        if now - fps_last_log_time >= 1.0:
            fps_value = fps_frames / (now - fps_last_log_time)
            print(f"[FPS] {fps_value:.2f} frames/s")
            fps_frames = 0
            fps_last_log_time = now

        yolo_start = time.perf_counter()
        results = model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=VEHICLE_CLASSES,
            conf=0.35,
            verbose=False
        )
        profile_totals["yolo_tracking"] += time.perf_counter() - yolo_start
        profile_counts["yolo_tracking"] += 1

        annotated_frame = frame.copy()
        active_vehicle_panels = []
        vehicle_boxes_for_ui = []

        if results[0].boxes is not None and results[0].boxes.id is not None:
            ids = results[0].boxes.id.int().cpu().tolist()
            coordinates = results[0].boxes.xyxy.cpu().tolist()

            for raw_id, box in zip(ids, coordinates):
                x1, y1, x2, y2 = map(int, box)
                center_x = int((x1 + x2) / 2)
                center_y = int((y1 + y2) / 2)

                detected_plate, confidence = "NECITIT", 0.0
                stable_id = match_vehicle_to_memory(raw_id, box, frame, detected_plate)

                memory = vehicle_memory.get(stable_id)
                if memory is None:
                    memory = {
                        "plate": None,
                        "last_valid_plate": None,
                        "raw_ids": set(),
                        "current_raw_id": raw_id,
                        "status": "MIȘCARE",
                        "stationary_start_frame": None,
                        "duration_seconds": 0.0,
                        "first_seen_frame": frame_number,
                        "last_seen_frame": frame_number,
                        "last_center": (center_x, center_y),
                        "previous_center": (center_x, center_y),
                        "last_position": (center_x, center_y),
                        "signature": compute_visual_signature(frame, box),
                        "exceeded_limit": False,
                        "stationary_confirm_frames": 0,
                        "last_ocr_frame": None,
                        "last_plate_scan_frame": None,
                    }
                    vehicle_memory[stable_id] = memory

                memory["raw_ids"].add(raw_id)
                memory["current_raw_id"] = raw_id
                memory["last_seen_frame"] = frame_number
                memory["signature"] = compute_visual_signature(frame, box)

                last_valid_plate = memory.get("last_valid_plate")
                last_plate_scan_frame = memory.get("last_plate_scan_frame")
                should_run_plate_detector = (
                    last_plate_scan_frame is None
                    or frame_number - last_plate_scan_frame >= (
                        PLATE_RETRY_INTERVAL_FRAMES if not last_valid_plate else PLATE_SCAN_INTERVAL_FRAMES
                    )
                )

                if should_run_plate_detector:
                    detected_plate, confidence = detect_plate_for_vehicle(frame, box)
                    memory["last_plate_scan_frame"] = frame_number

                plate_text = memory.get("last_valid_plate") or memory.get("plate") or "NECITIT"
                should_run_ocr = (
                    memory.get("last_ocr_frame") is None
                    or frame_number - memory["last_ocr_frame"] >= OCR_INTERVAL_FRAMES
                ) and (
                    memory.get("last_valid_plate") is None
                    or memory.get("plate") is None
                    or memory.get("status") == "STAȚIONAR"
                )

                if should_run_ocr:
                    if should_run_plate_detector:
                        detected_plate, confidence = detect_plate_for_vehicle(frame, box)
                        memory["last_plate_scan_frame"] = frame_number
                    else:
                        detected_plate, confidence = (memory.get("plate") or "NECITIT", 0.0)
                    plate_text, _ = stabilize_plate_reading(stable_id, detected_plate, confidence)
                    memory["last_ocr_frame"] = frame_number
                    if plate_text and plate_text != "NECITIT":
                        memory["plate"] = plate_text
                        memory["last_valid_plate"] = plate_text
                    elif memory.get("last_valid_plate"):
                        plate_text = memory["last_valid_plate"]
                        memory["plate"] = plate_text
                    else:
                        plate_text = "NECITIT"
                        memory["plate"] = None
                else:
                    if memory.get("last_valid_plate"):
                        plate_text = memory["last_valid_plate"]
                        memory["plate"] = plate_text
                    else:
                        plate_text = "NECITIT"
                        memory["plate"] = None

                previous_position = memory.get("previous_center") or memory.get("last_center") or memory.get("last_position")
                if previous_position is not None:
                    movement = float(np.hypot(center_x - previous_position[0], center_y - previous_position[1]))
                    if movement <= STATIONARY_DISTANCE_THRESHOLD_PIXELS:
                        memory["stationary_confirm_count"] += 1
                    else:
                        memory["stationary_confirm_count"] = 0
                        memory["is_stationary"] = False
                        memory["stationary_start_frame"] = None
                        memory["duration_seconds"] = 0.0
                        memory["status"] = "MIȘCARE"
                else:
                    memory["stationary_confirm_count"] = 0
                    memory["is_stationary"] = False
                    memory["stationary_start_frame"] = None
                    memory["duration_seconds"] = 0.0
                    memory["status"] = "MIȘCARE"

                memory["stationary_confirm_frames"] = memory.get("stationary_confirm_count", 0)

                if memory["stationary_confirm_count"] >= STATIONARY_CONFIRM_FRAMES:
                    memory["is_stationary"] = True
                    if memory.get("stationary_start_frame") is None:
                        memory["stationary_start_frame"] = frame_number
                    memory["duration_seconds"] = max(0.0, (frame_number - memory["stationary_start_frame"]) / fps)
                    if memory["duration_seconds"] >= PARKING_LIMIT_SECONDS:
                        memory["status"] = "DEPĂȘIRE LIMITĂ"
                        if not memory.get("exceeded_limit", False):
                            memory["exceeded_limit"] = True
                            memory["over_limit_at"] = frame_number / max(fps, 1.0)
                            parking_events.append({
                                "vehicle_id": stable_id,
                                "plate": memory.get("plate", "NECITIT"),
                                "timestamp": frame_number / fps,
                                "duration": memory["duration_seconds"],
                                "status": "DEPĂȘIRE LIMITĂ"
                            })
                    else:
                        memory["status"] = "STAȚIONAR"
                else:
                    memory["is_stationary"] = False
                    memory["stationary_start_frame"] = None
                    memory["duration_seconds"] = 0.0
                    memory["status"] = "MIȘCARE"

                memory["previous_center"] = memory.get("last_center", (center_x, center_y))
                memory["last_center"] = (center_x, center_y)
                memory["last_position"] = (center_x, center_y)

                if memory["status"] == "DEPĂȘIRE LIMITĂ" and memory.get("plate"):
                    plate_text = memory["plate"]

                memory["observation_count"] = int(memory.get("observation_count", 0)) + 1
                memory["last_seen_frame"] = frame_number

                vehicle_boxes_for_ui.append((
                    x1,
                    y1,
                    x2,
                    y2,
                    stable_id,
                    memory["duration_seconds"],
                    memory["status"],
                    plate_text,
                ))

        active_stable_ids = {item[4] for item in vehicle_boxes_for_ui}
        for stable_id in list(vehicle_memory.keys()):
            if stable_id not in active_stable_ids and frame_number - vehicle_memory[stable_id].get("last_seen_frame", 0) > MAX_STALE_FRAMES:
                vehicle_memory.pop(stable_id, None)
                vehicle_plate_history.pop(stable_id, None)
                vehicle_plate_cache.pop(stable_id, None)
                for raw_id_key, mapped_stable_id in list(raw_tracking_to_vehicle.items()):
                    if mapped_stable_id == stable_id:
                        raw_tracking_to_vehicle.pop(raw_id_key, None)

        emit_active_vehicle_snapshot(vehicle_boxes_for_ui)
        emit_active_vehicle_boxes(vehicle_boxes_for_ui)
        emit_vehicle_history_snapshot()

        for x1, y1, x2, y2, vehicle_id, duration, status_text, plate_text in vehicle_boxes_for_ui:
            panel_rect = draw_vehicle_box(
                annotated_frame,
                x1,
                y1,
                x2,
                y2,
                vehicle_id,
                duration,
                status_text,
                plate_text,
                active_vehicle_panels
            )
            active_vehicle_panels.append(panel_rect)

        if out is not None:
            out.write(annotated_frame)

        process_display_start = time.perf_counter()
        queue_display_frame(annotated_frame)
        publish_live_preview_frame(annotated_frame)
        profile_totals["process_display"] += time.perf_counter() - process_display_start
        profile_counts["process_display"] += 1

        loop_total_elapsed = time.perf_counter() - loop_start
        profile_totals["loop_total"] += loop_total_elapsed
        profile_counts["loop_total"] += 1

        wait_block_elapsed = max(0.0, loop_total_elapsed - (
            profile_totals["frame_read"] / max(1, profile_counts["frame_read"]) +
            profile_totals["yolo_tracking"] / max(1, profile_counts["yolo_tracking"]) +
            profile_totals["plate_detector"] / max(1, profile_counts["plate_detector"]) +
            profile_totals["easyocr"] / max(1, profile_counts["easyocr"]) +
            profile_totals["process_display"] / max(1, profile_counts["process_display"])
        ))
        profile_totals["wait_block"] += wait_block_elapsed
        profile_counts["wait_block"] += 1

        log_profile_summary()

        if stop_requested:
            break


start_live_preview_server()
if DISPLAY_ENABLED:
    setup_display_window()
if display_root is not None:
    ai_thread = threading.Thread(target=run_ai_loop, daemon=True)
    ai_thread.start()
    display_root.after(DISPLAY_REFRESH_INTERVAL_MS, refresh_display_tick)
    display_root.mainloop()
else:
    ai_thread = threading.Thread(target=run_ai_loop, daemon=True)
    ai_thread.start()
    ai_thread.join()

# ============================================================
# 12. FINAL
# ============================================================

if display_root is not None:
    try:
        display_root.update_idletasks()
        display_root.quit()
        display_root.destroy()
    except Exception:
        pass

cap.release()
if out is not None:
    out.release()
cv2.destroyAllWindows()

print()
print("======================================")
print("PARKING AI TEST GATA!")
print("======================================")
print(f"Videoclip rezultat:")
print(OUTPUT_VIDEO)
print()