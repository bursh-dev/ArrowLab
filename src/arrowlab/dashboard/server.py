from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
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


@app.get("/shoot", response_class=HTMLResponse)
def shoot_page() -> str:
    return (STATIC_DIR / "shoot.html").read_text(encoding="utf-8")


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
    data = {k: s.get(k) for k in ("id", "stem", "created_at", "range", "calibration_frame", "annotation", "shot_count", "fake_source", "shots", "sound_template")}
    SESSION_STATE_FILE.write_text(json.dumps(data, indent=2))


def _load_persisted_session() -> None:
    if not SESSION_STATE_FILE.exists():
        return
    try:
        data = json.loads(SESSION_STATE_FILE.read_text())
    except Exception:
        return
    s = _new_session()
    for k in ("id", "stem", "created_at", "range", "calibration_frame", "annotation", "shot_count", "fake_source", "shots", "sound_template"):
        if k in data:
            s[k] = data[k]
    # Recover shots from on-disk trajectories if persisted list is empty
    if not s["shots"]:
        s["shots"] = _recover_shots_for_stem(s["stem"])
    # Backfill target_photo_url for persisted shots that predate the feature
    # (or were recovered before the photo existed on disk).
    stem = s["stem"]
    for sh in s["shots"]:
        if sh.get("target_photo_url"):
            continue
        n = int(sh.get("shot") or 0)
        if not n:
            continue
        photo = DATA_RAW / "sessions" / f"{stem}_shot{n:02d}_target.jpg"
        if photo.exists():
            sh["target_photo_url"] = "/videos/" + photo.relative_to(DATA_RAW).as_posix()
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
        tfirst = int(trajectory.get("tracked_first_frame") or 1)
        trim_offset_s = max(0.0, (tfirst - 1) / fps)
        clip_trim = clip_mp4.with_name(clip_mp4.stem + "_trim.mp4")
        out_clip = clip_trim if clip_trim.exists() else clip_mp4
        out_tracked = tracked_mp4
        start_s = 0.0 if clip_trim.exists() else trim_offset_s
        photo_path = clip_mp4.with_name(clip_mp4.stem + "_target.jpg")
        target_photo_url = (
            "/videos/" + photo_path.relative_to(DATA_RAW).as_posix()
            if photo_path.exists() else None
        )
        recovered.append({
            "type": "shot_ready",
            "shot": n,
            "clip_url": "/videos/" + out_clip.relative_to(DATA_RAW).as_posix(),
            "tracked_url": "/processed/" + out_tracked.relative_to(DATA_PROCESSED).as_posix(),
            "trajectory": trajectory,
            "start_s": start_s,
            "trim_offset_s": trim_offset_s,
            "target_photo_url": target_photo_url,
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
    # Best-effort file cleanup (raw, trimmed raw, tracked, trajectory)
    (DATA_RAW / "sessions" / f"{stem}_shot{n:02d}.mp4").unlink(missing_ok=True)
    (DATA_RAW / "sessions" / f"{stem}_shot{n:02d}_trim.mp4").unlink(missing_ok=True)
    (DATA_RAW / "sessions" / f"{stem}_shot{n:02d}_target.jpg").unlink(missing_ok=True)
    (DATA_PROCESSED / "tracked" / f"{stem}_shot{n:02d}_tracked.mp4").unlink(missing_ok=True)
    (DATA_PROCESSED / "trajectories" / f"{stem}_shot{n:02d}.yaml").unlink(missing_ok=True)
    _persist_session()
    await _broadcast_view({"type": "state", **_session_snapshot()})
    return {"ok": True, "removed": removed}


# ============================================================================
# Sound-calibration endpoints: extract short audio snippets around each shot's
# release/impact so the operator can accept/reject them by ear and the server
# can average the accepted ones into a per-session template.
# ============================================================================

SOUND_SNIPPET_DIR = DATA_RAW / "sessions" / "sound_snippets"
SNIPPET_DURATION_S = 0.30
# Both release AND impact similarities must clear this for a candidate to be
# accepted. Log-mag cosine against per-session templates.
SOUND_MATCH_THRESHOLD = 0.80


def _extract_snippet(src_mp4: Path, at_s: float, out_path: Path) -> None:
    """Cut SNIPPET_DURATION_S of audio centred on `at_s` from `src_mp4`
    into a mono 16 kHz wav at `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, at_s - SNIPPET_DURATION_S / 2)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", str(src_mp4),
            "-t", f"{SNIPPET_DURATION_S:.3f}",
            "-vn",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ],
        check=True,
    )


def _decode_wav_to_f32_16k(wav_path: Path):
    """Decode any audio file to mono 16 kHz f32 PCM as a numpy array."""
    import numpy as np
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(wav_path),
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _decode_pcm_s16_to_f32_16k(pcm_bytes: bytes, src_rate: int):
    """Resample a raw little-endian 16-bit PCM mono stream at `src_rate`
    to 16 kHz f32 mono via ffmpeg. Used for the /api/sound-match path
    where the phone sends raw 44.1 kHz s16le — non-integer ratio so we
    rely on ffmpeg's resampler rather than decimating on-device."""
    import numpy as np
    if not pcm_bytes:
        return None
    proc = subprocess.run(
        ["ffmpeg", "-v", "error",
         "-f", "s16le", "-ar", str(int(src_rate)), "-ac", "1", "-i", "pipe:0",
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        input=pcm_bytes, capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _log_mag_spectrum(pcm_f32):
    """L2-normalised log-magnitude spectrum of the first 4096 samples
    (~256 ms at 16 kHz). Hann-windowed rfft → 2049 bins. Returns None if
    the input is too short to carry meaningful spectral content."""
    import numpy as np
    if pcm_f32 is None or pcm_f32.size < 512:
        return None
    N = 4096
    x = np.zeros(N, dtype=np.float32)
    src = pcm_f32[: min(pcm_f32.size, N)]
    x[: src.size] = src
    x *= np.hanning(N).astype(np.float32)
    spec = np.abs(np.fft.rfft(x))
    log_mag = np.log1p(spec)
    norm = float(np.linalg.norm(log_mag))
    if norm == 0.0:
        return None
    return (log_mag / norm).astype(np.float32)


@app.get("/api/calibration-sound/shots")
def api_calibration_sound_shots() -> dict:
    """List shots that have detected audio events, ready for calibration."""
    s = _require_session()
    out: list[dict] = []
    for sh in s.get("shots", []) or []:
        r = sh.get("audio_release_s")
        i = sh.get("audio_impact_s")
        if r is None or i is None:
            continue
        out.append({
            "shot": sh["shot"],
            "release_s": r,
            "impact_s": i,
            "release_snippet_url": f"/api/calibration-sound/snippet/{sh['shot']}/release",
            "impact_snippet_url": f"/api/calibration-sound/snippet/{sh['shot']}/impact",
        })
    return {"shots": out}


@app.get("/api/calibration-sound/snippet/{shot:int}/{kind}")
def api_calibration_sound_snippet(shot: int, kind: str):
    from fastapi.responses import FileResponse
    if kind not in ("release", "impact"):
        raise HTTPException(400, "kind must be release or impact")
    s = _require_session()
    match = next((sh for sh in s.get("shots", []) or [] if int(sh.get("shot") or 0) == shot), None)
    if match is None:
        raise HTTPException(404, f"shot {shot} not in session")
    at_s = match.get(f"audio_{kind}_s")
    if at_s is None:
        raise HTTPException(404, f"shot {shot} has no {kind} timestamp")
    src = DATA_RAW / "sessions" / f"{s['stem']}_shot{shot:02d}.mp4"
    if not src.exists():
        raise HTTPException(404, f"source mp4 missing for shot {shot}")
    out = SOUND_SNIPPET_DIR / f"{s['stem']}_shot{shot:02d}_{kind}.wav"
    if not out.exists():
        try:
            _extract_snippet(src, float(at_s), out)
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"ffmpeg failed: {e}")
    return FileResponse(out, media_type="audio/wav")


class SoundTemplateRequest(BaseModel):
    accepted_release_shots: list[int]
    accepted_impact_shots: list[int]


@app.put("/api/calibration-sound/template")
def api_calibration_sound_template(req: SoundTemplateRequest) -> dict:
    """Compute release + impact templates by averaging the log-magnitude
    spectra of the accepted snippets. Stored per-session in _active.json.
    Also self-scores each accepted snippet against the final averaged
    template so the operator can see up front whether their own
    calibration data clears the runtime threshold."""
    import numpy as np
    s = _require_session()
    stem = s["stem"]

    def _snippet_spectrum(shot: int, kind: str):
        wav = SOUND_SNIPPET_DIR / f"{stem}_shot{shot:02d}_{kind}.wav"
        if not wav.exists():
            match = next((sh for sh in s.get("shots", []) or [] if int(sh.get("shot") or 0) == shot), None)
            at_s = match and match.get(f"audio_{kind}_s")
            src = DATA_RAW / "sessions" / f"{stem}_shot{shot:02d}.mp4"
            if match is None or at_s is None or not src.exists():
                return None
            try:
                _extract_snippet(src, float(at_s), wav)
            except subprocess.CalledProcessError:
                return None
        pcm = _decode_wav_to_f32_16k(wav)
        return _log_mag_spectrum(pcm)

    def _gather(shots: list[int], kind: str):
        tagged: list[tuple[int, "np.ndarray"]] = []
        for sh in shots:
            sp = _snippet_spectrum(sh, kind)
            if sp is not None:
                tagged.append((sh, sp))
        return tagged

    def _average(tagged):
        if not tagged:
            return None
        stack = np.stack([sp for _, sp in tagged])
        mean = np.mean(stack, axis=0)
        mean = mean / max(float(np.linalg.norm(mean)), 1e-12)
        return mean.astype(np.float32)

    def _self_scores(template, tagged):
        if template is None or not tagged:
            return None
        sims = {sh: float(np.dot(template, sp)) for sh, sp in tagged}
        vals = list(sims.values())
        return {
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "mean": round(sum(vals) / len(vals), 4),
            "per_shot": {str(sh): round(v, 4) for sh, v in sims.items()},
            "threshold": SOUND_MATCH_THRESHOLD,
            "below_threshold_count": sum(1 for v in vals if v < SOUND_MATCH_THRESHOLD),
        }

    release_tagged = _gather(req.accepted_release_shots, "release")
    impact_tagged = _gather(req.accepted_impact_shots, "impact")
    release_template = _average(release_tagged)
    impact_template = _average(impact_tagged)
    if release_template is None or impact_template is None:
        raise HTTPException(400, "need at least one accepted snippet of each kind")

    s["sound_template"] = {
        "release": release_template.tolist(),
        "impact": impact_template.tolist(),
        "release_count": len(req.accepted_release_shots),
        "impact_count": len(req.accepted_impact_shots),
    }
    _persist_session()
    return {
        "ok": True,
        "release_count": len(req.accepted_release_shots),
        "impact_count": len(req.accepted_impact_shots),
        "template_bins": int(release_template.size),
        "release_self_scores": _self_scores(release_template, release_tagged),
        "impact_self_scores": _self_scores(impact_template, impact_tagged),
    }


@app.post("/api/sound-match")
async def api_sound_match(
    release: UploadFile = File(...),
    impact: UploadFile = File(...),
    src_rate: int = Form(44100),
) -> dict:
    """Armed-mode pre-filter. Phone uploads two 300 ms raw little-endian
    16-bit PCM mono snippets (at `src_rate`, default 44.1 kHz) centred on
    its on-device release + impact peak picks. Server resamples to 16 kHz,
    computes the same Hann-windowed log-mag spectrum the calibration
    endpoint uses, cosine-compares to the stored per-session templates.

    Accept requires BOTH sims ≥ SOUND_MATCH_THRESHOLD — a cough that
    happens to resemble an impact alone should not trigger a shot.

    Returns {accept, release_sim, impact_sim, threshold}. Special cases:
      - no template stored: accept=true with no_template=true (so armed
        mode keeps working pre-calibration; the operator is expected to
        calibrate before relying on runtime rejection).
      - snippet fails to decode / too short: accept=false with an
        `error` field — explicit so the phone logs the failure rather
        than silently accepting garbage.
    """
    import numpy as np
    s = _require_session()
    template = s.get("sound_template")

    release_bytes = await release.read()
    impact_bytes = await impact.read()

    if not template:
        result = {
            "accept": True,
            "no_template": True,
            "release_sim": None,
            "impact_sim": None,
            "threshold": SOUND_MATCH_THRESHOLD,
            "release_bytes": len(release_bytes),
            "impact_bytes": len(impact_bytes),
        }
        await _broadcast_view({"type": "sound_match_result", **result})
        return result

    def _score() -> dict:
        rel_pcm = _decode_pcm_s16_to_f32_16k(release_bytes, src_rate)
        imp_pcm = _decode_pcm_s16_to_f32_16k(impact_bytes, src_rate)
        if rel_pcm is None or imp_pcm is None:
            return {
                "accept": False, "error": "decode_failed",
                "release_sim": None, "impact_sim": None,
                "threshold": SOUND_MATCH_THRESHOLD,
            }
        rel_spec = _log_mag_spectrum(rel_pcm)
        imp_spec = _log_mag_spectrum(imp_pcm)
        if rel_spec is None or imp_spec is None:
            return {
                "accept": False, "error": "spectrum_failed",
                "release_sim": None, "impact_sim": None,
                "threshold": SOUND_MATCH_THRESHOLD,
            }
        rel_t = np.asarray(template["release"], dtype=np.float32)
        imp_t = np.asarray(template["impact"], dtype=np.float32)
        if rel_spec.shape != rel_t.shape or imp_spec.shape != imp_t.shape:
            return {
                "accept": False, "error": "shape_mismatch",
                "release_sim": None, "impact_sim": None,
                "threshold": SOUND_MATCH_THRESHOLD,
            }
        release_sim = float(np.dot(rel_spec, rel_t))
        impact_sim = float(np.dot(imp_spec, imp_t))
        accept = (
            release_sim >= SOUND_MATCH_THRESHOLD
            and impact_sim >= SOUND_MATCH_THRESHOLD
        )
        return {
            "accept": accept,
            "release_sim": round(release_sim, 4),
            "impact_sim": round(impact_sim, 4),
            "threshold": SOUND_MATCH_THRESHOLD,
        }

    result = await run_in_threadpool(_score)
    result["release_bytes"] = len(release_bytes)
    result["impact_bytes"] = len(impact_bytes)
    await _broadcast_view({"type": "sound_match_result", **result})
    return result


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


def _probe_duration_s(path: Path) -> float | None:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            text=True,
        )
        return float(out.strip())
    except Exception:
        return None


def _extract_target_photo(
    mp4_path: Path,
    at_s: float,
    bbox: list[int] | None,
    out_jpeg: Path,
    margin_px: int = 40,
) -> bool:
    """Extract a single frame from `mp4_path` at `at_s`, crop to the target
    region defined by `bbox` + margin, save as JPEG. Returns True on success.
    If `bbox` is None, saves the full frame."""
    out_jpeg.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, at_s):.3f}",
        "-i", str(mp4_path),
        "-frames:v", "1",
    ]
    if bbox is not None and len(bbox) == 4:
        x0, y0, x1, y1 = bbox
        cx = max(0, x0 - margin_px)
        cy = max(0, y0 - margin_px)
        cw = (x1 - x0) + 2 * margin_px
        ch = (y1 - y0) + 2 * margin_px
        cmd += ["-vf", f"crop={cw}:{ch}:{cx}:{cy}"]
    cmd += ["-q:v", "2", str(out_jpeg)]
    try:
        subprocess.run(cmd, check=True)
        return out_jpeg.exists() and out_jpeg.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False


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
        # JPEG path. Phone sends `X-Image-Rotate-Cw` to undo the display
        # matrix that TextureView.getBitmap baked into the bitmap, so the
        # saved frame ends up sensor-native (matching the encoder's mp4
        # frames the tracker consumes). fake_phone doesn't set the header
        # — its source-extracted JPEGs are already sensor-native.
        rotate_cw = 0
        rotate_hdr = request.headers.get("x-image-rotate-cw")
        if rotate_hdr:
            try:
                rotate_cw = int(rotate_hdr) % 360
            except ValueError:
                rotate_cw = 0
        if rotate_cw == 0:
            out.write_bytes(data)
        else:
            import cv2
            import numpy as np
            arr = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise HTTPException(400, "could not decode JPEG body")
            if rotate_cw == 90:
                img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            elif rotate_cw == 180:
                img = cv2.rotate(img, cv2.ROTATE_180)
            elif rotate_cw == 270:
                img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            cv2.imwrite(str(out), img)
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
            elif kind in ("arm", "disarm"):
                phone = LIVE_STATE["phone_ws"]
                if phone is None:
                    await ws.send_json({"type": "error", "msg": "no phone connected"})
                    continue
                if kind == "arm":
                    s = LIVE_STATE["session"]
                    if s is None or s["annotation"] is None:
                        await ws.send_json({"type": "error", "msg": "session not calibrated"})
                        continue
                await phone.send_json({"type": kind})
                await _broadcast_view({"type": f"{kind}ed"})
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
    # Phone-provided audio event timestamps (armed mode) override server-side
    # detection. Seconds are measured from the start of this mp4.
    def _float_header(name: str) -> float | None:
        v = request.headers.get(name)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None
    provided_events = {
        "release_s": _float_header("X-Arrow-Release-S"),
        "impact_s": _float_header("X-Arrow-Impact-S"),
    } if request.headers.get("X-Arrow-Release-S") or request.headers.get("X-Arrow-Impact-S") else None
    # Phone records the pre-filter similarity scores for each accepted
    # candidate so we can audit marginal accepts after the fact.
    rel_sim = _float_header("X-Arrow-Release-Sim")
    imp_sim = _float_header("X-Arrow-Impact-Sim")
    provided_sound_match = {
        "release_sim": rel_sim,
        "impact_sim": imp_sim,
    } if rel_sim is not None or imp_sim is not None else None

    clip_url = "/videos/" + slice_path.relative_to(DATA_RAW).as_posix()
    await _broadcast_view({
        "type": "shot_uploaded",
        "shot": n,
        "bytes": len(data),
        "clip_url": clip_url,
    })
    asyncio.create_task(_process_shot(
        slice_path, n, s,
        provided_events=provided_events,
        provided_sound_match=provided_sound_match,
    ))
    return {"ok": True, "shot_id": n, "clip_url": clip_url}


async def _process_shot(
    slice_path: Path,
    n: int,
    session: dict,
    provided_events: dict | None = None,
    provided_sound_match: dict | None = None,
) -> None:
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
        from arrowlab.video.track import build_roi_mask, probe_fps, track_clip

        t_start = time.perf_counter()
        # Probe metadata without decoding all frames. Holding the full frame
        # list in memory peaks at ~6 MB × frame_count (e.g. ~7 GB for a 5 s
        # 240 fps clip) and OOMs on 8 GB hosts. We only fall back to that
        # path when the motion detector is needed (no audio events from
        # phone) — the armed-mode hot path stays streaming.
        cap_probe = cv2.VideoCapture(str(slice_path))
        if not cap_probe.isOpened():
            return None
        w = int(cap_probe.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap_probe.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap_probe.get(cv2.CAP_PROP_FRAME_COUNT))
        cap_probe.release()
        if w <= 0 or h <= 0:
            return None
        t_decoded = time.perf_counter()

        # Audio onset detection. If the phone already ran its own detector
        # (armed mode) and sent timestamps, trust those and skip the server
        # pass entirely. Otherwise run the server detector.
        if provided_events and provided_events.get("release_s") is not None and provided_events.get("impact_s") is not None:
            audio_events = {
                "release_s": float(provided_events["release_s"]),
                "impact_s": float(provided_events["impact_s"]),
            }
        else:
            audio_events = _detect_audio_events(slice_path)
        t_audio = time.perf_counter()

        fps_probe = probe_fps(slice_path)
        a: int | None = None
        b: int | None = None
        flight_source = "none"
        # `frames` is only populated when motion fallback is needed (rare —
        # phone armed mode always provides audio events). Streaming path
        # leaves it None and lets track_clip read the cap itself.
        frames: list | None = None
        if (
            audio_events.get("release_s") is not None
            and audio_events.get("impact_s") is not None
        ):
            a_guess = int(round(audio_events["release_s"] * fps_probe))
            b_guess = int(round(audio_events["impact_s"] * fps_probe))
            # 2-frame pad each side covers audio/video alignment jitter.
            a = max(0, a_guess - 2)
            b = min(max(total_frames, 1) - 1, b_guess + 2)
            if b > a:
                flight_source = "audio"
        if flight_source == "none":
            # Motion fallback needs every frame in memory. Decode now.
            cap = cv2.VideoCapture(str(slice_path))
            frames = []
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                frames.append(f)
            cap.release()
            if not frames:
                return None
            if total_frames <= 0:
                total_frames = len(frames)
            roi = build_roi_mask((h, w), annotation)
            detected = auto_detect_flight_in_clip(frames, roi)
            if detected is None:
                print(f"[shot {n}] decode={t_decoded-t_start:.1f}s audio={t_audio-t_decoded:.1f}s (NO FLIGHT)")
                return None
            a, b = detected
            flight_source = "motion"
        t_detected = time.perf_counter()
        traj_path = track_clip(
            slice_path, annotation, a + 1, b + 1,
            clip_start_frame=1, clip_end_frame=max(total_frames, 1),
            output_stem=slice_path.stem,
            video_label=f"live shot {n}",
            shot_index=n - 1,
            log_prefix=f"[live] shot {n}",
            frames=frames,
        )
        t_tracked = time.perf_counter()
        timings_s = {
            "decode_s": round(t_decoded - t_start, 3),
            "audio_detect_s": round(t_audio - t_decoded, 3),
            "detect_s": round(t_detected - t_audio, 3),
            "track_s": round(t_tracked - t_detected, 3),
        }
        print(
            f"[shot {n}] decode={timings_s['decode_s']}s "
            f"audio={timings_s['audio_detect_s']}s "
            f"flight({flight_source})={timings_s['detect_s']}s "
            f"track={timings_s['track_s']}s frames={len(frames) if frames is not None else total_frames}"
        )
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
        # Downscale raw trim to ~960 wide to match the tracked mp4 and cut
        # cv2 encode time roughly in half. Operator only views these clips,
        # analysis already happened at full res.
        raw_out_w, raw_out_h = w, h
        raw_scale = 1.0
        if w > 960:
            raw_scale = 960.0 / w
            raw_out_w = 960
            raw_out_h = int(round(h * raw_scale))
            if raw_out_h % 2 == 1: raw_out_h -= 1
        raw_writer = cv2.VideoWriter(
            str(clip_trim),
            cv2.VideoWriter_fourcc(*"mp4v"),
            float(writer_fps_int),
            (raw_out_w, raw_out_h),
        )
        try:
            if frames is not None:
                for i in range(tfirst - 1, min(tlast, len(frames))):
                    f = frames[i]
                    if raw_scale != 1.0:
                        f = cv2.resize(f, (raw_out_w, raw_out_h), interpolation=cv2.INTER_AREA)
                    raw_writer.write(f)
            else:
                # Streaming path: re-open the cap and write only the
                # [tfirst-1 .. tlast-1] window. cap.set(POS_FRAMES) is
                # unreliable on H.264 inter-frame video, so we just stream
                # forward and skip until we reach tfirst-1.
                cap_trim = cv2.VideoCapture(str(slice_path))
                try:
                    pos = 0
                    while pos < tfirst - 1:
                        ok, _ = cap_trim.read()
                        if not ok:
                            break
                        pos += 1
                    while pos < tlast:
                        ok, f = cap_trim.read()
                        if not ok:
                            break
                        if raw_scale != 1.0:
                            f = cv2.resize(f, (raw_out_w, raw_out_h), interpolation=cv2.INTER_AREA)
                        raw_writer.write(f)
                        pos += 1
                finally:
                    cap_trim.release()
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

        # Audio chronograph: reuse the events already detected up-front for
        # flight-window picking. Speed is computed with sound-propagation
        # correction based on the session range geometry.
        speed_audio_ms = _audio_chronograph_speed_ms(
            audio_events.get("release_s"),
            audio_events.get("impact_s"),
            session.get("range"),
        )
        clip_url = "/videos/" + clip_trim.relative_to(DATA_RAW).as_posix()
        tracked_url = "/processed/" + tracked_path.relative_to(DATA_PROCESSED).as_posix()
        start_s = 0.0

        # Target photo: grab a frame ~300 ms after impact (arrow already
        # embedded, vibration mostly settled) and crop to the annotated
        # target bbox. Clamp to ~50 ms before the end of the mp4 — armed-
        # mode clips are ~1.5 s long so impact+0.3 s can land past EOF.
        target_photo_url: str | None = None
        impact_s = audio_events.get("impact_s")
        bbox = (annotation.get("target") or {}).get("bbox") if annotation else None
        # Fallback seek point if we have no impact timestamp.
        fallback_s = max(0.0, (tlast - 1) / fps)
        raw_duration_s = _probe_duration_s(slice_path) or fallback_s + 0.5
        photo_target_s = (impact_s if impact_s is not None else fallback_s) + 0.3
        photo_at_s = min(photo_target_s, max(0.0, raw_duration_s - 0.05))
        photo_path = DATA_RAW / "sessions" / f"{slice_path.stem}_target.jpg"
        if _extract_target_photo(slice_path, photo_at_s, bbox, photo_path):
            target_photo_url = "/videos/" + photo_path.relative_to(DATA_RAW).as_posix()

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
            "target_photo_url": target_photo_url,
            "sound_match": provided_sound_match,
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
