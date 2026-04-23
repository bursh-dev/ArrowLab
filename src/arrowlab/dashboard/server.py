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

SESSION_STATE_FILE = DATA_RAW / "sessions" / "_active.json"


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


def _persist_session() -> None:
    s = LIVE_STATE["session"]
    if s is None:
        SESSION_STATE_FILE.unlink(missing_ok=True)
        return
    SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {k: s[k] for k in ("id", "stem", "created_at", "range", "calibration_frame", "annotation", "shot_count", "fake_source", "shots")}
    SESSION_STATE_FILE.write_text(json.dumps(data, indent=2))


def _load_persisted_session() -> None:
    if not SESSION_STATE_FILE.exists():
        return
    try:
        data = json.loads(SESSION_STATE_FILE.read_text())
    except Exception:
        return
    s = _new_session()
    for k in ("id", "stem", "created_at", "range", "calibration_frame", "annotation", "shot_count", "fake_source", "shots"):
        if k in data:
            s[k] = data[k]
    # Recover shots from on-disk trajectories if persisted list is empty
    if not s["shots"]:
        s["shots"] = _recover_shots_for_stem(s["stem"])
    LIVE_STATE["session"] = s


def _recover_shots_for_stem(stem: str) -> list[dict]:
    traj_dir = DATA_PROCESSED / "trajectories"
    if not traj_dir.exists():
        return []
    recovered: list[dict] = []
    for yaml_path in sorted(traj_dir.glob(f"{stem}_shot*.yaml")):
        # Expect filename like {stem}_shot01.yaml
        shot_str = yaml_path.stem.replace(f"{stem}_shot", "")
        try:
            n = int(shot_str)
        except ValueError:
            continue
        clip_mp4 = DATA_RAW / "sessions" / f"{stem}_shot{n:02d}.mp4"
        tracked_mp4 = DATA_PROCESSED / "tracked" / f"{stem}_shot{n:02d}_tracked.mp4"
        if not clip_mp4.exists() or not tracked_mp4.exists():
            continue
        try:
            trajectory = yaml.safe_load(yaml_path.read_text())
        except Exception:
            continue
        fps = float(trajectory.get("fps") or 240.0)
        fw = trajectory.get("flight_window") or [1, 1]
        tfirst = trajectory.get("tracked_first_frame")
        clip_trim = clip_mp4.with_name(clip_mp4.stem + "_trim.mp4")
        tracked_trim = tracked_mp4.with_name(tracked_mp4.stem + "_trim.mp4")

        if tfirst is not None:
            # New tracker: tracked_mp4 is already trimmed to its own frame window.
            trim_offset_s = max(0.0, (int(tfirst) - 1) / fps)
            out_tracked = tracked_mp4
            out_clip = clip_trim if clip_trim.exists() else clip_mp4
            start_s = 0.0 if clip_trim.exists() else trim_offset_s
        elif clip_trim.exists() and tracked_trim.exists():
            # Legacy sibling-trim shots
            trim_offset_s = max(0.0, (fw[0] - 1) / fps - 0.2)
            out_clip = clip_trim
            out_tracked = tracked_trim
            start_s = 0.0
        else:
            trim_offset_s = 0.0
            out_clip = clip_mp4
            out_tracked = tracked_mp4
            start_s = max(0.0, (fw[0] - 1) / fps - 0.2)
        recovered.append({
            "type": "shot_ready",
            "shot": n,
            "clip_url": "/videos/" + out_clip.relative_to(DATA_RAW).as_posix(),
            "tracked_url": "/processed/" + out_tracked.relative_to(DATA_PROCESSED).as_posix(),
            "trajectory": trajectory,
            "start_s": start_s,
            "trim_offset_s": trim_offset_s,
        })
    return recovered


@app.on_event("startup")
def _on_startup() -> None:
    _load_persisted_session()


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
        "annotation": s["annotation"] if s else None,
        "shot_count": s["shot_count"] if s else 0,
        "fake_source": s["fake_source"] if s else None,
        "shots": s["shots"] if s else [],
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
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True, **_session_snapshot()}


@app.post("/api/session/end")
async def api_session_end() -> dict:
    LIVE_STATE["session"] = None
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True}


@app.get("/api/session")
def api_session() -> dict:
    return _session_snapshot()


@app.get("/api/session/debug")
def api_session_debug() -> dict:
    s = LIVE_STATE["session"]
    if s is None:
        return {"session": None}
    return {
        "session_id": s["id"],
        "stem": s["stem"],
        "annotation": s["annotation"],
        "range": s["range"],
        "shot_count": s["shot_count"],
    }


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
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True}


@app.delete("/api/session/range")
async def api_session_clear_range() -> dict:
    s = _require_session()
    s["range"] = None
    _persist_session()
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
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    phone = LIVE_STATE["phone_ws"]
    if phone is not None:
        try:
            await phone.send_json({"type": "annotation", "corridor": None, "target": None})
        except Exception:
            pass
    return {"ok": True}


@app.delete("/api/session/shot/{n}")
async def api_session_delete_shot(n: int) -> dict:
    s = _require_session()
    shots = s.get("shots") or []
    keep = [sh for sh in shots if int(sh.get("shot", -1)) != n]
    removed = len(shots) - len(keep)
    s["shots"] = keep
    stem = s["stem"]
    # Best-effort file cleanup (raw, trimmed raw, tracked, trimmed tracked, trajectory)
    (DATA_RAW / "sessions" / f"{stem}_shot{n:02d}.mp4").unlink(missing_ok=True)
    (DATA_RAW / "sessions" / f"{stem}_shot{n:02d}_trim.mp4").unlink(missing_ok=True)
    (DATA_PROCESSED / "tracked" / f"{stem}_shot{n:02d}_tracked.mp4").unlink(missing_ok=True)
    (DATA_PROCESSED / "tracked" / f"{stem}_shot{n:02d}_tracked_trim.mp4").unlink(missing_ok=True)
    (DATA_PROCESSED / "trajectories" / f"{stem}_shot{n:02d}.yaml").unlink(missing_ok=True)
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True, "removed": removed}


@app.put("/api/session/annotation")
async def api_session_annotation(annotation: SessionAnnotation) -> dict:
    s = _require_session()
    s["annotation"] = annotation.model_dump(exclude_none=True)
    _persist_session()
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


def _ffmpeg_trim(
    src: Path,
    dst: Path,
    start_s: float,
    duration_s: float | None = None,
    fps: float | None = None,
) -> None:
    """Re-encode `src` into `dst`, dropping everything before start_s (frame-accurate).
    If duration_s is given, also cap the output length.
    If fps is given, force constant framerate on the output (so two parallel clips
    — raw + tracked — share the same timebase and stay in sync when played together)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start_s):.3f}",
        "-i", str(src),
    ]
    if duration_s is not None:
        cmd += ["-t", f"{duration_s:.3f}"]
    if fps is not None and fps > 0:
        cmd += ["-vf", f"fps={fps:.6f}"]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def _detect_audio_events(mp4_path: Path) -> dict:
    """Return {'release_s','impact_s'} from the mp4's audio track, or Nones
    if audio is missing / no clear transients found. Simple two-peak energy
    detector — good enough as a first cut; per-session matched-filter
    templates come later."""
    import numpy as np
    sr = 16_000
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-i", str(mp4_path),
            "-f", "f32le", "-ac", "1", "-ar", str(sr),
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return {"release_s": None, "impact_s": None}
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size < sr // 10:
        return {"release_s": None, "impact_s": None}

    # Smooth energy envelope over a 5 ms window.
    env = np.abs(audio)
    win = max(1, int(sr * 0.005))
    cs = np.cumsum(np.concatenate([[0.0], env]))
    env_smooth = (cs[win:] - cs[:-win]) / win

    noise = float(np.median(env_smooth))
    peak = float(env_smooth.max())
    if peak < noise * 5.0:
        return {"release_s": None, "impact_s": None}

    # Impact = the single loudest sample in the envelope (arrow hitting a
    # target is usually by far the loudest transient on the track).
    impact_idx = int(np.argmax(env_smooth))

    # Release = the loudest peak in the 500 ms window BEFORE impact, with a
    # 80 ms guard zone so we don't pick impact's ramp-up. If no peak stands
    # out above the noise floor, we give up on release and return None.
    min_pre_gap = int(sr * 0.08)
    max_pre_gap = int(sr * 0.5)
    win_start = max(0, impact_idx - max_pre_gap)
    win_end = max(0, impact_idx - min_pre_gap)
    release_idx = None
    if win_end > win_start:
        seg = env_smooth[win_start:win_end]
        if seg.size > 0 and float(seg.max()) > noise * 4.0:
            release_idx = win_start + int(np.argmax(seg))

    def idx_to_s(i: int) -> float:
        return float((i + win / 2) / sr)

    return {
        "release_s": idx_to_s(release_idx) if release_idx is not None else None,
        "impact_s": idx_to_s(impact_idx),
    }


def _audio_chronograph_speed_ms(release_s: float | None, impact_s: float | None, rng: dict | None) -> float | None:
    """Arrow speed from two acoustic timestamps, corrected for the sound-
    propagation delay between mic (at the camera) and the two sources
    (bow at shooter, arrow-impact at target)."""
    if release_s is None or impact_s is None or rng is None:
        return None
    D = rng.get("shooter_to_target_m")
    if not D or D <= 0:
        return None
    cp = float(rng.get("camera_perpendicular_m") or 0.0)
    ca = float(rng.get("camera_along_m") if rng.get("camera_along_m") is not None else D / 2.0)
    c = 343.0
    d_mic_shooter = (ca * ca + cp * cp) ** 0.5
    d_mic_target = ((D - ca) ** 2 + cp * cp) ** 0.5
    gap_corrected = (impact_s - release_s) - (d_mic_target - d_mic_shooter) / c
    if gap_corrected <= 0:
        return None
    return float(D / gap_corrected)


def _merge_audio_from_source(src_mp4: Path, video_only_mp4: Path, start_s: float, dur_s: float) -> None:
    """Add an audio track to `video_only_mp4` by copying `dur_s` of audio from
    `src_mp4` starting at `start_s`. Overwrites `video_only_mp4` with the
    audio-merged result. Raises if `src_mp4` has no audio stream."""
    tmp = video_only_mp4.with_suffix(video_only_mp4.suffix + ".withaudio.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_only_mp4),
            "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{dur_s:.3f}", "-i", str(src_mp4),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart",
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(video_only_mp4)


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
    _persist_session()
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
    _persist_session()
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
    import time as _time
    t_pipeline_start = _time.perf_counter()
    annotation = session["annotation"]
    if annotation is None:
        await _broadcast_view({"type": "shot_failed", "shot": n, "reason": "no annotation"})
        return

    def _work() -> dict | None:
        import cv2
        import time
        from arrowlab.video.live_sim import auto_detect_flight_in_clip
        from arrowlab.video.track import build_roi_mask, track_clip

        t_start = time.perf_counter()
        cap = cv2.VideoCapture(str(slice_path))
        frames: list = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
        cap.release()
        t_decoded = time.perf_counter()
        if not frames:
            return None
        h, w = frames[0].shape[:2]
        roi = build_roi_mask((h, w), annotation)
        detected = auto_detect_flight_in_clip(frames, roi)
        t_detected = time.perf_counter()
        if detected is None:
            print(f"[shot {n}] decode={t_decoded-t_start:.1f}s detect={t_detected-t_decoded:.1f}s (NO FLIGHT)")
            return None
        a, b = detected
        traj_path = track_clip(
            slice_path, annotation, a + 1, b + 1,
            clip_start_frame=1, clip_end_frame=len(frames),
            output_stem=slice_path.stem,
            video_label=f"live shot {n}",
            shot_index=n - 1,
            log_prefix=f"[live] shot {n}",
            frames=frames,
        )
        t_tracked = time.perf_counter()
        timings_s = {
            "decode_s": round(t_decoded - t_start, 3),
            "detect_s": round(t_detected - t_decoded, 3),
            "track_s": round(t_tracked - t_detected, 3),
        }
        print(f"[shot {n}] decode={timings_s['decode_s']}s detect={timings_s['detect_s']}s track={timings_s['track_s']}s frames={len(frames)}")
        session["trajectories"].append(traj_path)
        with open(traj_path) as f:
            trajectory = yaml.safe_load(f)
        tracked_path = DATA_PROCESSED / "tracked" / f"{slice_path.stem}_tracked.mp4"
        fps = float(trajectory.get("fps") or 240.0)
        # Tracker already emits just the flight window + pad in tracked_path;
        # its t=0 corresponds to original clip-frame `tracked_first_frame` (1-indexed).
        tfirst = int(trajectory.get("tracked_first_frame") or 1)
        tlast = int(trajectory.get("tracked_last_frame") or tfirst)
        trim_offset_s = max(0.0, (tfirst - 1) / fps)
        duration_s = max(0.1, (tlast - tfirst + 1) / fps)

        # Write the raw trim with cv2 from the same already-decoded frame slice
        # [tfirst-1 .. tlast-1] that the tracker wrote. This guarantees identical
        # timestamps / frame count / fps as the tracked mp4, so the two clips
        # stay in sync at slow playback. (ffmpeg -vf fps resampled the phone's
        # VFR mp4 non-uniformly and drifted visibly.)
        writer_fps_int = max(1, int(round(fps)))
        clip_trim = slice_path.with_name(slice_path.stem + "_trim.mp4")
        from arrowlab.video.encode import to_h264_faststart
        raw_writer = cv2.VideoWriter(
            str(clip_trim),
            cv2.VideoWriter_fourcc(*"mp4v"),
            float(writer_fps_int),
            (w, h),
        )
        try:
            for i in range(tfirst - 1, min(tlast, len(frames))):
                raw_writer.write(frames[i])
        finally:
            raw_writer.release()
        to_h264_faststart(clip_trim)
        # Remux: take video from our cv2-written trim, audio from the same
        # window of the source mp4 (if it has an audio track). Gives the
        # operator an audible raw clip without changing video timestamps.
        try:
            _merge_audio_from_source(slice_path, clip_trim, trim_offset_s, duration_s)
        except Exception:
            pass  # No audio track / ffmpeg failure -> silent clip, fine.
        t_trimmed = time.perf_counter()
        timings_s["trim_s"] = round(t_trimmed - t_tracked, 3)

        # Audio onset detection: pull release + impact timestamps out of the
        # source mp4's audio track. `speed_audio_ms` is the chronograph
        # derivation — independent of the visual tracker, so it's a cross-
        # check on the pixel-based speed metric.
        audio_events = _detect_audio_events(slice_path)
        speed_audio_ms = _audio_chronograph_speed_ms(
            audio_events.get("release_s"),
            audio_events.get("impact_s"),
            session.get("range"),
        )
        timings_s["audio_s"] = round(time.perf_counter() - t_trimmed, 3)
        clip_url = "/videos/" + clip_trim.relative_to(DATA_RAW).as_posix()
        tracked_url = "/processed/" + tracked_path.relative_to(DATA_PROCESSED).as_posix()
        start_s = 0.0

        return {
            "tracked_url": tracked_url,
            "clip_url": clip_url,
            "trajectory": trajectory,
            "start_s": start_s,
            "trim_offset_s": trim_offset_s,
            "timings": timings_s,
            "audio_release_s": audio_events.get("release_s"),
            "audio_impact_s": audio_events.get("impact_s"),
            "speed_audio_ms": speed_audio_ms,
        }

    try:
        result = await run_in_threadpool(_work)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await _broadcast_view({"type": "shot_failed", "shot": n, "reason": f"{type(e).__name__}: {e}"})
        return
    if result is None:
        await _broadcast_view({"type": "shot_failed", "shot": n, "reason": "no flight detected"})
    else:
        processing_s = _time.perf_counter() - t_pipeline_start
        payload = {"type": "shot_ready", "shot": n, "processing_s": processing_s, **result}
        session["shots"].append(payload)
        _persist_session()
        await _broadcast_view(payload)
        await _broadcast_view({"type": "state", **_session_snapshot()})
