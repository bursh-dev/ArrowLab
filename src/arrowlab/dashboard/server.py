from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
ANNOTATIONS_PATH = DATA_RAW / "annotations.yaml"

app = FastAPI(title="ArrowLab annotate")
app.mount("/videos", StaticFiles(directory=str(DATA_RAW)), name="videos")
app.mount("/processed", StaticFiles(directory=str(DATA_PROCESSED)), name="processed")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _parse_fraction(s: str) -> float:
    if "/" in s:
        num, den = s.split("/")
        return float(num) / float(den) if float(den) else 0.0
    return float(s)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/view", response_class=HTMLResponse)
def view_page() -> str:
    return (STATIC_DIR / "view.html").read_text(encoding="utf-8")


@app.get("/api/view")
def view_data(video: str) -> dict:
    stem = Path(video).stem
    shot_clips = sorted((DATA_PROCESSED / "shot_clips").glob(f"{stem}_shot*.mp4")) if (DATA_PROCESSED / "shot_clips").exists() else []
    tracked = sorted((DATA_PROCESSED / "tracked").glob(f"{stem}_shot*_tracked.mp4")) if (DATA_PROCESSED / "tracked").exists() else []
    synth = sorted((DATA_PROCESSED / "synth").glob(f"{stem}_shot*_synth.mp4")) if (DATA_PROCESSED / "synth").exists() else []
    combined = DATA_PROCESSED / "synth" / f"{stem}_combined.mp4"

    def rel(p: Path) -> str:
        return "/processed/" + p.relative_to(DATA_PROCESSED).as_posix()

    return {
        "shot_clips": [rel(p) for p in shot_clips],
        "tracked": [rel(p) for p in tracked],
        "synth": [rel(p) for p in synth],
        "combined": rel(combined) if combined.exists() else None,
    }


@app.get("/api/videos")
def list_videos() -> list[str]:
    videos: list[str] = []
    for p in DATA_RAW.rglob("*.mp4"):
        videos.append(p.relative_to(DATA_RAW).as_posix())
    videos.sort()
    return videos


@app.get("/api/video-info")
def video_info(path: str) -> dict:
    full = DATA_RAW / path
    if not full.exists():
        raise HTTPException(404, f"{path} not found")

    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
            "-of", "json", str(full),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return {
        "width": stream["width"],
        "height": stream["height"],
        "r_frame_rate": _parse_fraction(stream["r_frame_rate"]),
        "avg_frame_rate": _parse_fraction(stream["avg_frame_rate"]),
        "nb_frames": int(stream.get("nb_frames", 0) or 0),
        "duration": float(stream.get("duration", 0) or 0),
    }


class Corridor(BaseModel):
    y_top: int
    y_bottom: int


class Target(BaseModel):
    cx: int
    cy: int
    r: int
    bbox: list[int] | None = None
    face_diameter_m: float = 0.40


class Shot(BaseModel):
    flight_window: list[int]


class VideoAnnotation(BaseModel):
    corridor: Corridor | None = None
    target: Target | None = None
    shots: list[Shot] = []


def _load_yaml() -> dict:
    if not ANNOTATIONS_PATH.exists():
        return {"videos": {}}
    with ANNOTATIONS_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("videos", {})
    return data


def _save_yaml(data: dict) -> None:
    ANNOTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ANNOTATIONS_PATH.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


@app.get("/api/annotations")
def get_annotations() -> JSONResponse:
    return JSONResponse(_load_yaml())


@app.get("/api/annotations/{video_path:path}")
def get_annotation(video_path: str) -> dict:
    data = _load_yaml()
    return (data.get("videos") or {}).get(video_path) or {}


@app.put("/api/annotations/{video_path:path}")
def save_annotation(video_path: str, annotation: VideoAnnotation) -> dict:
    data = _load_yaml()
    data.setdefault("videos", {})[video_path] = annotation.model_dump(exclude_none=True)
    _save_yaml(data)
    return {"ok": True}


# ============================================================================
# Live session (M1/M2): phone WS + view WS + /api/shot + async pipeline
# ============================================================================

LIVE_STATE: dict = {
    "phone_ws": None,
    "view_wss": [],
    "session": None,  # dict when active, None otherwise
}


def _new_session() -> dict:
    stem = "sess_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "id": stem,
        "stem": stem,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "range": None,               # physical geometry (distances) once set
        "calibration_frame": None,  # relative URL once JPEG uploaded
        "annotation": None,          # {corridor, target} once saved
        "shot_count": 0,
        "trajectories": [],          # list of Path to per-shot trajectory yaml
        "shots": [],                 # list of shot_ready payload dicts (for view replay)
        "fake_source": None,         # filename hint advertised by fake phone for the scrubber
    }


async def _broadcast_view(msg: dict) -> None:
    dead = []
    for ws in LIVE_STATE["view_wss"]:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in LIVE_STATE["view_wss"]:
            LIVE_STATE["view_wss"].remove(ws)


def _session_snapshot() -> dict:
    s = LIVE_STATE["session"]
    return {
        "phone_connected": LIVE_STATE["phone_ws"] is not None,
        "active": s is not None,
        "session_id": s["id"] if s else None,
        "range": s["range"] if s else None,
        "has_range": bool(s and s["range"]),
        "calibration_frame": s["calibration_frame"] if s else None,
        "has_annotation": bool(s and s["annotation"]),
        "shot_count": s["shot_count"] if s else 0,
        "fake_source": s["fake_source"] if s else None,
    }


def _require_session() -> dict:
    s = LIVE_STATE["session"]
    if s is None:
        raise HTTPException(400, "no active session")
    return s


@app.post("/api/session")
async def api_session_start() -> dict:
    if LIVE_STATE["session"] is not None:
        raise HTTPException(409, "session already active")
    LIVE_STATE["session"] = _new_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True, **_session_snapshot()}


@app.post("/api/session/end")
async def api_session_end() -> dict:
    LIVE_STATE["session"] = None
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True}


@app.get("/api/session")
def api_session() -> dict:
    return _session_snapshot()


class SessionAnnotation(BaseModel):
    corridor: Corridor
    target: Target


class SessionRange(BaseModel):
    shooter_to_target_m: float
    camera_perpendicular_m: float  # perpendicular distance from camera to the shooting line
    camera_along_m: float          # camera's foot-of-perpendicular on the line, measured from shooter (0 = at shooter, D = at target)
    arrow_mass_grains: float | None = None
    bow_weight_lbs: float | None = None
    notes: str | None = None


@app.put("/api/session/range")
async def api_session_range(r: SessionRange) -> dict:
    s = _require_session()
    s["range"] = r.model_dump(exclude_none=True)
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True}


@app.delete("/api/session/range")
async def api_session_clear_range() -> dict:
    s = _require_session()
    s["range"] = None
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True}


@app.delete("/api/session/calibration")
async def api_session_clear_calibration() -> dict:
    s = _require_session()
    # Wipe calibration frame + annotation from the session record
    old_frame = s.get("calibration_frame")
    s["calibration_frame"] = None
    s["annotation"] = None
    # Best-effort delete of the stored JPEG
    if old_frame:
        fname = old_frame.rsplit("/", 1)[-1]
        (DATA_RAW / "sessions" / fname).unlink(missing_ok=True)
    await _broadcast_view({"type": "state", **_session_snapshot()})
    phone = LIVE_STATE["phone_ws"]
    if phone is not None:
        try:
            await phone.send_json({"type": "annotation", "corridor": None, "target": None})
        except Exception:
            pass
    return {"ok": True}


@app.put("/api/session/annotation")
async def api_session_annotation(annotation: SessionAnnotation) -> dict:
    s = _require_session()
    s["annotation"] = annotation.model_dump(exclude_none=True)
    await _broadcast_view({"type": "state", **_session_snapshot()})
    phone = LIVE_STATE["phone_ws"]
    if phone is not None:
        try:
            await phone.send_json({"type": "annotation", **s["annotation"]})
        except Exception:
            pass
    return {"ok": True}


def _extract_middle_jpeg(mp4: Path, out_jpeg: Path) -> None:
    """Pull a single near-end frame out of an mp4 as JPEG."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-sseof", "-0.2",
            "-i", str(mp4),
            "-frames:v", "1",
            "-q:v", "3",
            str(out_jpeg),
        ],
        check=True,
    )


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


@app.post("/api/calibration-frame")
async def api_calibration_frame(request: Request) -> dict:
    s = _require_session()
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body")
    out = DATA_RAW / "sessions" / f"{s['stem']}_calibration.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    content_type = request.headers.get("content-type", "").lower()
    if "video/mp4" in content_type or _looks_like_mp4(data):
        # Real-phone path: short mp4 from the ring buffer. Extract one frame
        # server-side; orientation follows whatever the mp4 has baked in,
        # which matches the shot mp4 frames the tracker later consumes.
        mp4_path = DATA_RAW / "sessions" / f"{s['stem']}_calibration.mp4"
        mp4_path.write_bytes(data)
        try:
            await run_in_threadpool(_extract_middle_jpeg, mp4_path, out)
        finally:
            mp4_path.unlink(missing_ok=True)
    else:
        # Fake-phone path: direct JPEG upload.
        out.write_bytes(data)
    url = "/videos/" + out.relative_to(DATA_RAW).as_posix()
    s["calibration_frame"] = url
    await _broadcast_view({"type": "calibration_frame_ready", "url": url})
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True, "url": url}


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket) -> None:
    await ws.accept()
    if LIVE_STATE["phone_ws"] is not None:
        await ws.send_json({"type": "rejected", "reason": "phone already connected"})
        await ws.close()
        return
    if LIVE_STATE["session"] is None:
        await ws.send_json({"type": "rejected", "reason": "no active session"})
        await ws.close()
        return
    LIVE_STATE["phone_ws"] = ws
    s = LIVE_STATE["session"]
    await ws.send_json({"type": "paired", "session_id": s["id"]})
    if s["annotation"] is not None:
        await ws.send_json({"type": "annotation", **s["annotation"]})
    else:
        # Explicit clear so a reconnecting phone doesn't keep stale overlays.
        await ws.send_json({"type": "annotation", "corridor": None, "target": None})
    await _broadcast_view({"type": "state", **_session_snapshot()})
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "pair":
                await ws.send_json({"type": "paired", "session_id": s["id"]})
            elif kind == "hint_source":
                src = msg.get("source_video")
                if src and LIVE_STATE["session"] is not None:
                    LIVE_STATE["session"]["fake_source"] = src
                    await _broadcast_view({"type": "state", **_session_snapshot()})
    except WebSocketDisconnect:
        pass
    finally:
        if LIVE_STATE["phone_ws"] is ws:
            LIVE_STATE["phone_ws"] = None
        await _broadcast_view({"type": "state", **_session_snapshot()})


@app.websocket("/ws/view")
async def ws_view(ws: WebSocket) -> None:
    await ws.accept()
    LIVE_STATE["view_wss"].append(ws)
    await ws.send_json({"type": "state", **_session_snapshot()})
    # Replay previously processed shots for late-joining views
    s = LIVE_STATE["session"]
    if s is not None:
        for shot in s.get("shots", []):
            await ws.send_json(shot)
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "start_session":
                if LIVE_STATE["session"] is None:
                    LIVE_STATE["session"] = _new_session()
                await _broadcast_view({"type": "state", **_session_snapshot()})
            elif kind == "end_session":
                LIVE_STATE["session"] = None
                await _broadcast_view({"type": "state", **_session_snapshot()})
            elif kind == "request_calibration_frame":
                phone = LIVE_STATE["phone_ws"]
                if phone is None:
                    await ws.send_json({"type": "error", "msg": "no phone connected"})
                    continue
                if LIVE_STATE["session"] is None:
                    await ws.send_json({"type": "error", "msg": "no active session"})
                    continue
                relay: dict = {"type": "capture_frame"}
                if "at_s" in msg:
                    relay["at_s"] = msg["at_s"]
                await phone.send_json(relay)
            elif kind == "trigger_shot":
                phone = LIVE_STATE["phone_ws"]
                if phone is None:
                    await ws.send_json({"type": "error", "msg": "no phone connected"})
                    continue
                s = LIVE_STATE["session"]
                if s is None:
                    await ws.send_json({"type": "error", "msg": "no active session"})
                    continue
                if s["annotation"] is None:
                    await ws.send_json({"type": "error", "msg": "session not calibrated"})
                    continue
                await phone.send_json({"type": "slice", "duration": 6})
    except WebSocketDisconnect:
        if ws in LIVE_STATE["view_wss"]:
            LIVE_STATE["view_wss"].remove(ws)


@app.post("/api/shot")
async def api_shot(request: Request) -> dict:
    s = _require_session()
    if s["annotation"] is None:
        raise HTTPException(400, "session not calibrated")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body")
    s["shot_count"] += 1
    n = s["shot_count"]
    slice_path = DATA_RAW / "sessions" / f"{s['stem']}_shot{n:02d}.mp4"
    slice_path.parent.mkdir(parents=True, exist_ok=True)
    slice_path.write_bytes(data)
    clip_url = "/videos/" + slice_path.relative_to(DATA_RAW).as_posix()
    await _broadcast_view({
        "type": "shot_uploaded",
        "shot": n,
        "bytes": len(data),
        "clip_url": clip_url,
    })
    asyncio.create_task(_process_shot(slice_path, n, s))
    return {"ok": True, "shot_id": n, "clip_url": clip_url}


async def _process_shot(slice_path: Path, n: int, session: dict) -> None:
    annotation = session["annotation"]
    if annotation is None:
        await _broadcast_view({"type": "shot_failed", "shot": n, "reason": "no annotation"})
        return

    def _work() -> dict | None:
        import cv2
        from arrowlab.video.live_sim import auto_detect_flight_in_clip
        from arrowlab.video.track import build_roi_mask, track_clip

        cap = cv2.VideoCapture(str(slice_path))
        frames: list = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
        cap.release()
        if not frames:
            return None
        h, w = frames[0].shape[:2]
        roi = build_roi_mask((h, w), annotation)
        detected = auto_detect_flight_in_clip(frames, roi)
        if detected is None:
            return None
        a, b = detected
        traj_path = track_clip(
            slice_path, annotation, a + 1, b + 1,
            clip_start_frame=1, clip_end_frame=len(frames),
            output_stem=slice_path.stem,
            video_label=f"live shot {n}",
            shot_index=n - 1,
            log_prefix=f"[live] shot {n}",
        )
        session["trajectories"].append(traj_path)
        with open(traj_path) as f:
            trajectory = yaml.safe_load(f)
        tracked_url = "/processed/" + (DATA_PROCESSED / "tracked" / f"{slice_path.stem}_tracked.mp4").relative_to(DATA_PROCESSED).as_posix()
        clip_url = "/videos/" + slice_path.relative_to(DATA_RAW).as_posix()
        return {
            "tracked_url": tracked_url,
            "clip_url": clip_url,
            "trajectory": trajectory,
        }

    result = await run_in_threadpool(_work)
    if result is None:
        await _broadcast_view({"type": "shot_failed", "shot": n, "reason": "no flight detected"})
    else:
        payload = {"type": "shot_ready", "shot": n, **result}
        session["shots"].append(payload)
        await _broadcast_view(payload)
