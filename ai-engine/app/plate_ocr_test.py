import cv2
import re
import easyocr
from ultralytics import YOLO

VIDEO = r".\ai-engine\media\test.mp4"
PLATE_MODEL = r".\ai-engine\license-plate-finetune-v1n.pt"
OUTPUT = r".\ai-engine\output\plate_ocr_test.mp4"

print("Se incarca detectorul de placute...")
plate_model = YOLO(PLATE_MODEL)

print("Se incarca EasyOCR...")
reader = easyocr.Reader(
    ["en"],
    gpu=False,
    verbose=True
)

cap = cv2.VideoCapture(VIDEO)

if not cap.isOpened():
    raise RuntimeError("Nu s-a putut deschide test.mp4")

fps = cap.get(cv2.CAP_PROP_FPS)
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out = cv2.VideoWriter(
    OUTPUT,
    fourcc,
    fps,
    (width, height)
)

frame_number = 0

def clean_plate(text):
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text

while True:
    ret, frame = cap.read()

    if not ret:
        break

    frame_number += 1

    results = plate_model.predict(
        frame,
        conf=0.25,
        imgsz=1280,
        verbose=False
    )

    for result in results:
        if result.boxes is None:
            continue

        for box in result.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            confidence = float(box.conf[0])

            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(width, x2)
            y2 = min(height, y2)

            plate = frame[y1:y2, x1:x2]

            if plate.size == 0:
                continue

            # Marim imaginea placutei pentru OCR
            plate_big = cv2.resize(
                plate,
                None,
                fx=3,
                fy=3,
                interpolation=cv2.INTER_CUBIC
            )

            ocr_results = reader.readtext(
                plate_big,
                detail=1,
                paragraph=False,
                allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )

            plate_text = ""

            if ocr_results:
                best = max(
                    ocr_results,
                    key=lambda item: item[2]
                )

                plate_text = clean_plate(best[1])
                ocr_confidence = best[2]

            if not plate_text:
                plate_text = "NECITIT"
                ocr_confidence = 0.0

            # Dreptunghi plăcuta
            cv2.rectangle(
                frame,
                (x1, y1),
                (x2, y2),
                (0, 255, 255),
                3
            )

            # Text OCR
            label = f"{plate_text}  OCR {ocr_confidence:.2f}"

            cv2.rectangle(
                frame,
                (x1, max(0, y1 - 40)),
                (x1 + max(250, len(label) * 15), y1),
                (0, 255, 255),
                -1
            )

            cv2.putText(
                frame,
                label,
                (x1 + 5, max(25, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 0),
                2,
                cv2.LINE_AA
            )

    out.write(frame)

    if frame_number % 30 == 0:
        print(f"Cadru procesat: {frame_number}")

cap.release()
out.release()

print()
print("GATA!")
print(f"Rezultat: {OUTPUT}")