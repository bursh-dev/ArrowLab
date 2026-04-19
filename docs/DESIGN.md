# ArrowLab — Design

Living design doc. Snapshots the state of the system as of 2026-04-18 and the
near-term roadmap. Companion to [`initial_idea.md`](initial_idea.md), which
captures the original vision without implementation details.

---

## 1. What the system does

ArrowLab is a per-shot archery telemetry rig built around a mounted slow-motion
phone camera, a laptop, and (later) an audio/chronograph input. Each shot
produces:

- A raw ~6 s slow-mo clip of the arrow in flight.
- A tracked overlay video (arrow detections + trail).
- A synthetic reconstruction of the trajectory.
- A cumulative "combined" video showing all shots of the session stacked on a
  single canvas.

Everything is operator-driven from a browser on the laptop. The phone is a dumb
camera + transport — all calibration and control lives on the laptop.

---

## 2. Operator workflow

1. **Setup.** Mount camera, power laptop, start the server.
2. **Start session.** Operator clicks *Start session* in `/view`. Server
   creates a session record `sess_YYYYMMDD_HHMMSS`.
3. **Phone connects.** Phone app (APK or `scripts/fake_phone.py`) opens a
   WebSocket to `/ws/phone`. Server attaches it to the active session and
   replies `paired`.
4. **Calibrate.** Operator clicks *Start calibration* → server tells phone
   `capture_frame` → phone posts a JPEG of the current preview to
   `/api/calibration-frame`. Browser shows the JPEG with a canvas overlay. The
   operator draws:
   - Corridor (two horizontal lines)
   - Target (center + rim for the circle, optional tight bbox)
   - Face diameter in metres
   The annotation is saved to the session record via
   `PUT /api/session/annotation`.
5. **Test / live shoot.** Each click of *SHOT!* sends `{type:"slice"}` to the
   phone. Phone uploads an mp4 slice. Server kicks the tracking pipeline
   asynchronously and broadcasts `shot_ready` with the rendered artefacts.
6. **End session.** Operator clicks *End session* → server clears the record;
   on-disk outputs remain under the session stem.

---

## 3. Architecture

```
 ┌───────────┐   WS  /ws/phone   ┌────────────────────────┐   WS /ws/view   ┌────────────┐
 │  Phone /  │◄─────────────────►│  Laptop FastAPI server │◄───────────────►│  Browser   │
 │  fake_ph. │   POST /api/shot  │                        │  POST /api/*    │  (/view)   │
 │           │◄──────────────────┤  session + pipeline    │                 │            │
 │           │   capture_frame   │                        │                 │            │
 │           │──────────────────►│                        │                 │            │
 │           │   /api/calibration│                        │                 │            │
 │           │   -frame          │                        │                 │            │
 └───────────┘                   └────────────────────────┘                 └────────────┘
                                          │
                                          ▼
                                  OpenCV + ffmpeg pipeline
                                  (track → synth → combined)
```

Single active session at a time (no registry, no multi-tenant). One phone at a
time. Multiple browser views can observe the same session over `/ws/view`.

---

## 4. Protocol reference

### 4.1 HTTP endpoints

| Method | Path                           | Purpose                                            |
|--------|--------------------------------|----------------------------------------------------|
| GET    | `/` `/view`                    | annotate and live-view HTML pages                  |
| GET    | `/static/*` `/videos/*` `/processed/*` | static assets + raw/processed media        |
| GET    | `/api/videos`                  | list mp4s under `data/raw/`                        |
| GET    | `/api/video-info?path=`        | ffprobe metadata                                   |
| GET    | `/api/view?video=`             | list processed artefacts keyed by video stem       |
| GET    | `/api/annotations` / `/{path}` | offline-video annotations (yaml-backed)            |
| PUT    | `/api/annotations/{path}`      | save offline annotation                            |
| POST   | `/api/session`                 | create live session (409 if one is active)         |
| POST   | `/api/session/end`             | clear live session                                 |
| GET    | `/api/session`                 | current session snapshot                           |
| PUT    | `/api/session/annotation`      | save corridor + target for the active session      |
| POST   | `/api/calibration-frame`       | phone uploads JPEG (body = raw jpeg bytes)         |
| POST   | `/api/shot`                    | phone uploads mp4 slice (body = raw mp4 bytes)     |

### 4.2 WebSocket — `/ws/phone`

Server → phone:

- `{type:"paired", session_id}` on attach
- `{type:"rejected", reason}` + close on no session / another phone already present
- `{type:"capture_frame", at_s?: number}` — ask phone to POST one JPEG (`at_s` is a fake-phone-only scrub hint; real APK ignores it)
- `{type:"slice", duration: 6}` — ask phone to POST an mp4 of the last ~6 s

Phone → server:

- `{type:"pair"}` — optional no-op, server already paired on WS accept
- `{type:"hint_source", source_video}` — **fake-phone only**; advertises the pre-recorded source filename under `data/raw/` so the browser can show a scrubber

### 4.3 WebSocket — `/ws/view`

Server → view (broadcast):

- `{type:"state", phone_connected, active, session_id, calibration_frame, has_annotation, shot_count, fake_source}` — sent on connect and on every relevant state change
- `{type:"calibration_frame_ready", url}`
- `{type:"shot_uploaded", shot, bytes, slice}`
- `{type:"shot_ready", shot, tracked, synth, combined}`
- `{type:"shot_failed", shot, reason}`
- `{type:"error", msg}`

Browser → server:

- `{type:"start_session"}` (or POST /api/session)
- `{type:"end_session"}`
- `{type:"request_calibration_frame", at_s?: number}`
- `{type:"trigger_shot"}`

### 4.4 Session snapshot shape

```json
{
  "phone_connected": true,
  "active": true,
  "session_id": "sess_20260418_151204",
  "calibration_frame": "/videos/sessions/sess_20260418_151204_calibration.jpg",
  "has_annotation": true,
  "shot_count": 3,
  "fake_source": "video_2026-04-18_11-05-38.mp4"
}
```

---

## 5. Server pipeline

`_process_shot(slice_path, n, session)` runs in a threadpool and:

1. Loads frames via `cv2.VideoCapture`.
2. `arrowlab.video.track.build_roi_mask(shape, annotation)` + `auto_detect_flight_in_clip` → detects which sub-range of frames contains the arrow.
3. `track_clip(...)` → writes a tracked mp4 + trajectory yaml (detection bboxes, smoothed positions, hit frame).
4. `synth.render(trajectory, out)` → writes a synthetic reconstruction.
5. If ≥2 trajectories exist for this session, `synth_combined.render_combined(all_trajectories, out)` → writes a single "all shots" canvas.
6. Every mp4 is re-encoded by `arrowlab.video.encode.to_h264_faststart()` to H.264 / yuv420p / `+faststart` so browsers actually play them (OpenCV's default `mp4v` fourcc produces MPEG-4 Part 2, which HTML5 `<video>` refuses).
7. `shot_ready` is broadcast with `/processed/...` URLs.

Annotation shape used by the pipeline:

```json
{
  "corridor": {"y_top": 556, "y_bottom": 840},
  "target":   {"cx": 1748, "cy": 678, "r": 28,
               "bbox": [1665, 575, 1843, 784],
               "face_diameter_m": 0.40}
}
```

---

## 6. On-disk layout

```
data/
  raw/
    video_*.mp4                    # offline / source videos (phone dumps)
    sessions/
      sess_<stamp>_calibration.jpg
      sess_<stamp>_shot01.mp4
      sess_<stamp>_shot02.mp4
      ...
    annotations.yaml               # offline-video annotations (legacy, /api/annotations)
  processed/
    shot_clips/                    # legacy offline pipeline output
    tracked/    sess_<stamp>_shotNN_tracked.mp4
    synth/      sess_<stamp>_shotNN_synth.mp4
                sess_<stamp>_combined.mp4
    trajectories/  sess_<stamp>_shotNN.yaml
    audio/      (peak wavs if audio path runs)
```

Session artefacts use the session stem so two sessions' outputs never collide.
Offline pipeline (from the annotate page) still uses `video_<stem>_shotN_*.mp4`
names from before the rework.

---

## 7. Source tree

```
src/arrowlab/
  audio/detect.py          # peak detection from source video audio (offline)
  video/
    track.py               # arrow detection + trajectory fit + tracked-overlay render
    synth.py               # synthetic per-shot visualisation
    synth_combined.py      # multi-trajectory stacked visualisation
    live_sim.py            # ffmpeg slicing helpers + auto_detect_flight_in_clip
    encode.py              # to_h264_faststart: post-OpenCV ffmpeg re-encode
  dashboard/
    server.py              # FastAPI app, WS handlers, session state, pipeline kickoff
    static/
      index.html  app.css  app.js    # /  (offline annotate)
      view.html   view.css view.js   # /view (live session + legacy browser)

scripts/fake_phone.py      # WS client that plays the phone role from a pre-recorded file

docs/
  initial_idea.md          # original vision
  DESIGN.md                # this doc
```

---

## 8. Implementation notes & gotchas

- **Codec.** Browsers reject MPEG-4 Part 2; every OpenCV-rendered mp4 must pass
  through `to_h264_faststart`. The helper also adds `+faststart` so the browser
  can start playback before the download completes.
- **Session scope for annotations.** The M1/M2 fake-phone first keyed sessions
  off a pre-recorded `source_video` and looked up annotations in
  `annotations.yaml`. That was replaced on 2026-04-18 with a session-first
  model: annotations live on the session record, not on a video filename. The
  APK path never had a `source_video`, so the rework is load-bearing for M3.
- **fake_source hint.** The fake phone advertises its source file via
  `hint_source` so the browser can show a video scrubber and pick a
  calibration-frame timestamp. Real APK must not send this message; when
  `fake_source` is absent the scrubber stays hidden.
- **No pair codes.** Single session, single phone, first phone wins. Revisit
  if the APK ever ships into an environment with multiple phones on the LAN.
- **`live_sim.py`** is the older offline simulator — the live server imports
  `auto_detect_flight_in_clip` from it but not the rest. Candidate for split
  into `detect.py` once the live path is the only one.

---

## 9. Roadmap

Near-term milestones in order of intended work:

### M3 — Kotlin APK (next)
- CameraX/Camera2 slow-mo (120/240 fps if available) with a rolling in-memory
  ring buffer of encoded H.264 frames.
- WS client → `/ws/phone`.
- Handle `capture_frame` (single JPEG from preview) and `slice` (mux last 6 s
  from ring buffer, POST to `/api/shot`).
- Minimal UI: laptop IP/port entry, pair/unpair, status, preview.

### M4 — Audio calibration
- On session start, phone records ~2 s ambient and POSTs it.
- `audio/detect.py` computes noise floor → sets a per-session detection
  threshold.
- Use that threshold to pick the exact shot time inside each slice, replacing
  `auto_detect_flight_in_clip`'s purely visual fallback.

### M5 — Chronograph integration
- Third input stream per shot (speed, kinetic energy).
- Hangs off the session record next to `trajectories`; no new top-level state.
- Exposed in `/view` alongside combined synth.

### M6 — Scoring / group analysis
- Per-session group size, mean POI, dispersion ellipse, scored value if the
  target face has scoring rings.
- Persists per session so past sessions can be browsed by date.

### M7 — Multi-shot test-mode feedback loop
- After the first calibration shot, overlay the tracked arrow path on the
  calibration JPEG so the operator can verify the annotation before doing a
  full set.

---

## 10. Open questions

- **Session persistence.** Today a server restart drops the active session.
  Should sessions be journalled to disk and reloadable? Probably not until
  sessions routinely exceed one server-process lifetime.
- **Multi-camera.** If we ever want two angles, the session model needs to
  accept N phones and N calibration records. The WS handshake would need a
  role field (`side_view`, `downrange`, etc.).
- **Offline vs live modularity.** The annotate page (`/`) still targets
  pre-recorded videos and writes `data/raw/annotations.yaml`. Worth keeping as
  a data-labelling tool, but it has accumulated shot-window code that the live
  path doesn't use.
- **Live audio on the slice.** Phones record audio alongside video; we
  currently ignore it in the slice. Keeping it (and wiring the audio detector
  into each shot) would tighten shot-time estimation without needing M4's
  ambient calibration.
