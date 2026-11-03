from ultralytics import YOLO
import cv2
from pathlib import Path

# Căile proiectului
BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_VIDEO = BASE_DIR / "media" / "test.mp4"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_VIDEO = OUTPUT_DIR / "tracked_vehicles.mp4"

# Creăm folderul output dacă nu există
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Încărcăm modelul YOLO
model = YOLO("yolo11n.pt")

# Deschidem videoclipul
cap = cv2.VideoCapture(str(INPUT_VIDEO))

if not cap.isOpened():
    raise RuntimeError(f"Nu pot deschide videoclipul: {INPUT_VIDEO}")

# Informații despre video
fps = cap.get(cv2.CAP_PROP_FPS)
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

print(f"Video: {width}x{height}")
print(f"FPS: {fps}")

# Video de ieșire
fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out = cv2.VideoWriter(
    str(OUTPUT_VIDEO),
    fourcc,
    fps,
    (width, height)
)

# Clasele vehiculelor COCO
# 2 = car
# 3 = motorcycle
# 5 = bus
# 7 = truck
vehicle_classes = [2, 3, 5, 7]

frame_number = 0

while True:
    success, frame = cap.read()

    if not success:
        break

    frame_number += 1

    # Tracking cu ID persistent
    results = model.track(
        frame,
        persist=True,
        tracker="bytetrack.yaml",
        classes=vehicle_classes,
        conf=0.35,
        verbose=False
    )

    # Desenăm detectările + ID-urile
    annotated_frame = results[0].plot()

    # Salvăm cadrul
    out.write(annotated_frame)

    if frame_number % 30 == 0:
        print(f"Procesat: {frame_number} cadre")

# Închidem videoclipul
cap.release()
out.release()

print()
print("TRACKING GATA!")
print(f"Videoclip rezultat: {OUTPUT_VIDEO}")