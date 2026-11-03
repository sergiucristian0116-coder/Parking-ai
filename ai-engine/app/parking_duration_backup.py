from ultralytics import YOLO
import cv2
import numpy as np
from pathlib import Path


# ============================================================
# 1. CĂILE PROIECTULUI
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

INPUT_VIDEO = BASE_DIR / "media" / "test.mp4"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_VIDEO = OUTPUT_DIR / "parking_ai_test.mp4"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. SETĂRI
# ============================================================

# Pentru test folosim 30 secunde.
# În sistemul final va fi:
# PARKING_LIMIT_SECONDS = 15 * 60
PARKING_LIMIT_SECONDS = 30

VEHICLE_CLASSES = [2, 3, 5, 7]

# 2 = car
# 3 = motorcycle
# 5 = bus
# 7 = truck


# ============================================================
# 3. MODEL YOLO
# ============================================================

MODEL_PATH = PROJECT_DIR / "yolo11n.pt"

model = YOLO(str(MODEL_PATH))


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


# ============================================================
# 5. ZONA DE PARCARE
# ============================================================

PARKING_ZONE = np.array([
    [1650, 650],
    [2550, 650],
    [2550, 1430],
    [1650, 1430]
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

fourcc = cv2.VideoWriter_fourcc(*"mp4v")

out = cv2.VideoWriter(
    str(OUTPUT_VIDEO),
    fourcc,
    fps,
    (width, height)
)


# ============================================================
# 8. TIMER PENTRU FIECARE VEHICUL
# ============================================================

parking_start = {}

reported_ids = set()


# ============================================================
# 9. FUNCȚII PENTRU INTERFAȚĂ
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


def draw_vehicle_box(
    image,
    x1,
    y1,
    x2,
    y2,
    vehicle_id,
    duration,
    inside_zone
):
    """
    Desenează chenarul + timerul pentru un vehicul.
    """

    if inside_zone:

        if duration >= PARKING_LIMIT_SECONDS:
            box_color = (0, 0, 255)
            status = "DEPASIRE LIMITA"
            status_color = (0, 0, 255)
        else:
            box_color = (0, 255, 0)
            status = "IN ZONA"
            status_color = (0, 255, 0)

    else:

        box_color = (255, 180, 0)
        status = "IN AFARA ZONEI"
        status_color = (255, 180, 0)

    # Chenarul mașinii
    cv2.rectangle(
        image,
        (x1, y1),
        (x2, y2),
        box_color,
        4
    )

    # Dimensiunea panoului de informații
    panel_width = 360
    panel_height = 120

    panel_x1 = x1
    panel_y2 = y1
    panel_y1 = max(10, y1 - panel_height)

    # Dacă nu avem loc deasupra,
    # punem panoul în interiorul imaginii.
    if panel_y2 - panel_y1 < panel_height:
        panel_y1 = 10
        panel_y2 = panel_y1 + panel_height

    panel_x2 = min(
        width - 10,
        panel_x1 + panel_width
    )

    # Fundalul panoului
    overlay = image.copy()

    cv2.rectangle(
        overlay,
        (panel_x1, panel_y1),
        (panel_x2, panel_y2),
        (10, 10, 10),
        -1
    )

    image[:] = cv2.addWeighted(
        overlay,
        0.88,
        image,
        0.12,
        0
    )

    # Marginea panoului
    cv2.rectangle(
        image,
        (panel_x1, panel_y1),
        (panel_x2, panel_y2),
        box_color,
        3
    )

    # ID vehicul
    cv2.putText(
        image,
        f"ID {vehicle_id}",
        (panel_x1 + 15, panel_y1 + 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (255, 255, 255),
        2
    )

    # Timer
    cv2.putText(
        image,
        format_time(duration),
        (panel_x1 + 15, panel_y1 + 68),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        box_color,
        3
    )

    # Status
    cv2.putText(
        image,
        status,
        (panel_x1 + 125, panel_y1 + 68),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        status_color,
        2
    )

    # Linie mică explicativă
    cv2.putText(
        image,
        "PARCARE MONITORIZATA",
        (panel_x1 + 15, panel_y1 + 102),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (220, 220, 220),
        1
    )


# ============================================================
# 10. PROCESAREA VIDEO
# ============================================================

frame_number = 0

while True:

    success, frame = cap.read()

    if not success:
        break

    frame_number += 1

    video_time = frame_number / fps

    # --------------------------------------------------------
    # Tracking
    # --------------------------------------------------------

    results = model.track(
        frame,
        persist=True,
        tracker="bytetrack.yaml",
        classes=VEHICLE_CLASSES,
        conf=0.35,
        verbose=False
    )

    annotated_frame = frame.copy()

    # --------------------------------------------------------
    # ZONA DE PARCARE
    # --------------------------------------------------------

    cv2.polylines(
        annotated_frame,
        [PARKING_ZONE],
        True,
        (0, 255, 255),
        5
    )

    # Eticheta zonei
    zone_label_x = 1750
    zone_label_y = 690

    cv2.rectangle(
        annotated_frame,
        (zone_label_x - 10, zone_label_y - 35),
        (zone_label_x + 330, zone_label_y + 10),
        (0, 0, 0),
        -1
    )

    cv2.putText(
        annotated_frame,
        "ZONA DE PARCARE",
        (zone_label_x, zone_label_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (0, 255, 255),
        3
    )

    # --------------------------------------------------------
    # NUMĂR VEHICULE
    # --------------------------------------------------------

    vehicle_count = 0
    parking_count = 0
    warning_count = 0

    # --------------------------------------------------------
    # VEHICULE
    # --------------------------------------------------------

    if results[0].boxes is not None:

        boxes = results[0].boxes

        if boxes.id is not None:

            ids = boxes.id.int().cpu().tolist()
            coordinates = boxes.xyxy.cpu().tolist()
            classes = boxes.cls.int().cpu().tolist()

            vehicle_count = len(ids)

            for vehicle_id, box, class_id in zip(
                ids,
                coordinates,
                classes
            ):

                x1, y1, x2, y2 = map(int, box)

                # Centrul de jos al vehiculului
                center_x = int((x1 + x2) / 2)
                center_y = int(y2)

                inside_zone = point_inside_zone(
                    center_x,
                    center_y
                )

                # ------------------------------------------------
                # Dacă vehiculul intră în zona de parcare
                # ------------------------------------------------

                if inside_zone:

                    parking_count += 1

                    if vehicle_id not in parking_start:

                        parking_start[vehicle_id] = video_time

                    duration = (
                        video_time
                        - parking_start[vehicle_id]
                    )

                    if duration >= PARKING_LIMIT_SECONDS:

                        warning_count += 1

                        if vehicle_id not in reported_ids:

                            reported_ids.add(vehicle_id)

                else:

                    duration = 0

                # ------------------------------------------------
                # Desenăm vehiculul
                # ------------------------------------------------

                draw_vehicle_box(
                    annotated_frame,
                    x1,
                    y1,
                    x2,
                    y2,
                    vehicle_id,
                    duration,
                    inside_zone
                )

    # ========================================================
    # 11. PANOU PARKING AI
    # ========================================================

    draw_panel(
        annotated_frame,
        25,
        25,
        410,
        205,
        "PARKING AI",
        [
            (
                f"Vehicule detectate: {vehicle_count}",
                (255, 255, 255)
            ),
            (
                f"In zona de parcare: {parking_count}",
                (255, 255, 255)
            ),
            (
                f"Depasiri limita: {warning_count}",
                (0, 0, 255) if warning_count > 0
                else (0, 255, 0)
            ),
            (
                f"Limita test: {PARKING_LIMIT_SECONDS}s",
                (0, 255, 0)
            )
        ]
    )

    # ========================================================
    # 12. PANOU CAMERA
    # ========================================================

    current_time = format_time(video_time)

    draw_panel(
        annotated_frame,
        width - 420,
        25,
        width - 25,
        120,
        "CAMERA 01",
        [
            (
                f"Timp video: {current_time}",
                (255, 255, 255)
            )
        ]
    )

    # ========================================================
    # 13. BARĂ INFERIOARĂ
    # ========================================================

    overlay = annotated_frame.copy()

    cv2.rectangle(
        overlay,
        (0, height - 55),
        (width, height),
        (15, 15, 15),
        -1
    )

    annotated_frame[:] = cv2.addWeighted(
        overlay,
        0.80,
        annotated_frame,
        0.20,
        0
    )

    cv2.putText(
        annotated_frame,
        "PARKING AI  |  MONITORIZARE PARCARE",
        (25, height - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2
    )

    cv2.putText(
        annotated_frame,
        "YOLO + TRACKING",
        (width - 270, height - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (200, 200, 200),
        2
    )

    # ========================================================
    # 14. SALVĂM CADRUL
    # ========================================================

    out.write(annotated_frame)

    if frame_number % 30 == 0:
        print(f"Procesat: {frame_number} cadre")


# ============================================================
# 15. FINAL
# ============================================================

cap.release()
out.release()

print()
print("======================================")
print("PARKING AI TEST GATA!")
print("======================================")
print(f"Videoclip rezultat:")
print(OUTPUT_VIDEO)
print()