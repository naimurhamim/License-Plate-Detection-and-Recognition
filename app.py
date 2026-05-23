# app.py
import os
import cv2
import csv
import json
import asyncio
import numpy as np
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import aiofiles
import easyocr
from ultralytics import YOLO

# ── Directory Setup ───────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
UPLOAD_DIR      = BASE_DIR / "uploads"
OUTPUT_DIR      = BASE_DIR / "output" / "plates"
DATA_DIR        = BASE_DIR / "data"
TEMPLATES_DIR   = BASE_DIR / "templates"
MODEL_PATH      = BASE_DIR / "models" / "best.pt"

for d in [UPLOAD_DIR, OUTPUT_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

CSV_FILE = DATA_DIR / "detections.csv"

# ── CSV Init ──────────────────────────────────────────────────────────
def init_csv():
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "plate_text", "confidence",
                "source", "frame_num",
                "plate_img", "timestamp"
            ])

def save_to_csv(plate_text, confidence, source, frame_num, plate_img):
    existing = []
    if CSV_FILE.exists():
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            existing = list(csv.reader(f))
    
    next_id = len(existing)  # header + rows
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            next_id, plate_text, round(confidence, 3),
            source, frame_num, plate_img, timestamp
        ])
    return next_id

def search_csv(query: str = "", source: str = ""):
    if not CSV_FILE.exists():
        return []
    results = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            match_query  = query.lower()  in row["plate_text"].lower()  if query  else True
            match_source = source.lower() in row["source"].lower()      if source else True
            if match_query and match_source:
                results.append(row)
    return results[::-1]  # newest first

# ── App Init ──────────────────────────────────────────────────────────
app       = FastAPI(title="LPR System")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/output",   StaticFiles(directory=str(BASE_DIR / "output")),  name="output")
app.mount("/uploads",  StaticFiles(directory=str(UPLOAD_DIR)),           name="uploads")

# ── Load Models ───────────────────────────────────────────────────────
print("[INFO] Loading YOLO model...")
detector = YOLO(str(MODEL_PATH))
print("[INFO] Loading EasyOCR...")
ocr_reader = easyocr.Reader(["en"], gpu=True, verbose=False)
print("[INFO] Models ready!")

init_csv()

# ── Routes ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload/{source}")
async def upload_video(source: str, file: UploadFile = File(...)):
    """Upload video for a specific source (cctv1, cctv2, video)."""
    allowed = {".mp4", ".avi", ".mov", ".mkv"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        return {"error": f"Unsupported format: {ext}"}

    # Save with source prefix to avoid name conflicts
    save_name = f"{source}_{file.filename}"
    save_path = UPLOAD_DIR / save_name
    async with aiofiles.open(save_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    return {"message": "Upload successful", "filename": save_name, "source": source}

@app.get("/videos/{source}")
async def list_videos(source: str):
    """List all uploaded videos for a source."""
    videos = [
        f.name for f in UPLOAD_DIR.iterdir()
        if f.is_file() and f.name.startswith(f"{source}_")
    ]
    return {"videos": videos}

@app.get("/search")
async def search(query: str = "", source: str = ""):
    """Search detections by plate text or source."""
    results = search_csv(query=query, source=source)
    return {"results": results, "count": len(results)}

@app.get("/all-detections")
async def all_detections():
    results = search_csv()
    return {"results": results, "count": len(results)}

# ── WebSocket Processing ──────────────────────────────────────────────

@app.websocket("/ws/process")
async def process_video(websocket: WebSocket):
    await websocket.accept()

    try:
        data     = await websocket.receive_text()
        payload  = json.loads(data)
        filename = payload.get("filename", "")
        source   = payload.get("source", "video")

        video_path = UPLOAD_DIR / filename
        if not video_path.exists():
            await websocket.send_text(json.dumps({
                "type": "error", "message": f"File not found: {filename}"
            }))
            return

        cap          = cv2.VideoCapture(str(video_path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps          = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_num    = 0
        seen_plates  = set()

        await websocket.send_text(json.dumps({
            "type": "started", "total_frames": total_frames, "fps": fps
        }))

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame_num += 1

            # Process every 3rd frame
            if frame_num % 3 != 0:
                continue

            # ── Detect plates ─────────────────────────────────────
            results = detector(frame, conf=0.4, verbose=False)

            for result in results:
                for box in result.boxes:
                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                    conf            = float(box.conf[0])

                    # Crop plate
                    pad       = 4
                    h, w      = frame.shape[:2]
                    px1, py1  = max(0, x1-pad), max(0, y1-pad)
                    px2, py2  = min(w, x2+pad), min(h, y2+pad)
                    plate_img = frame[py1:py2, px1:px2]

                    if plate_img.size == 0:
                        continue

                    # ── OCR ───────────────────────────────────────
                    try:
                        ocr_results = ocr_reader.readtext(plate_img)
                        plate_text  = " ".join([r[1] for r in ocr_results]).strip().upper()
                        plate_text  = "".join(c for c in plate_text if c.isalnum() or c in " -")
                    except:
                        plate_text = ""

                    if not plate_text or len(plate_text.replace(" ", "")) < 3:
                        continue

                    # Skip duplicate plates in same video
                    if plate_text in seen_plates:
                        continue
                    seen_plates.add(plate_text)

                    # ── Save plate image ──────────────────────────
                    ts        = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    img_name  = f"{source}_{ts}.jpg"
                    img_path  = OUTPUT_DIR / img_name
                    cv2.imwrite(str(img_path), plate_img)

                    # ── Draw on frame ─────────────────────────────
                    cv2.rectangle(frame, (x1,y1), (x2,y2), (0,200,0), 2)
                    cv2.putText(frame, plate_text, (x1, y1-8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,200,0), 2)

                    # ── Save to CSV ───────────────────────────────
                    save_to_csv(
                        plate_text = plate_text,
                        confidence = conf,
                        source     = source,
                        frame_num  = frame_num,
                        plate_img  = img_name
                    )

                    # ── Send detection to frontend ────────────────
                    await websocket.send_text(json.dumps({
                        "type":       "detection",
                        "plate_text": plate_text,
                        "confidence": round(conf * 100, 1),
                        "source":     source,
                        "frame_num":  frame_num,
                        "plate_img":  img_name,
                        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "bbox":       [x1, y1, x2, y2]
                    }))

            # ── Send frame to browser ─────────────────────────────
            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            await websocket.send_bytes(jpeg.tobytes())

            # Progress every 30 frames
            if frame_num % 30 == 0:
                progress = round((frame_num / total_frames) * 100, 1)
                await websocket.send_text(json.dumps({
                    "type": "progress", "progress": progress, "frame": frame_num
                }))

            await asyncio.sleep(0)

        cap.release()
        await websocket.send_text(json.dumps({
            "type": "completed", "message": "Processing complete",
            "total_detections": len(seen_plates)
        }))

    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS ERROR] {e}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)