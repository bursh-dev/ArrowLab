# ArrowTelemetry

Audio + video archery telemetry system.

## Idea

Use two devices to analyze each shot:

- **Audio node** at mid-range to measure time of flight from bow release sound to target impact sound.
- **Video node** from a side angle to analyze arrow flight, oscillation, approach angle, and shot consistency.
- Optional **dashboard/server** on a laptop or local device to show shot telemetry, charts, and history.

## Goals

- Estimate arrow average speed from sound.
- Track arrow position in video frames.
- Visualize trajectory and oscillation.
- Correlate audio and video telemetry per shot.
- Later package the workflow into an Android APK.

## Recommended first prototype

Start offline on a laptop before building the APK.

### Phase 1 — data collection

- Record several slow-motion side-view videos.
- Keep lighting bright and background high-contrast.
- Use known distance to target.
- Save matching audio-based speed measurements for each shot.

### Phase 2 — laptop analysis

- Crop the flight corridor.
- Detect the arrow or fletching in each frame.
- Track position frame to frame.
- Fit a shaft line where possible.
- Export plots for:
  - trajectory
  - oscillation amplitude
  - final approach angle
  - shot-to-shot consistency

### Phase 3 — integrated system

- Audio phone computes flight time and speed.
- Video phone captures side-view slow-motion video.
- Laptop merges both into one shot record.
- Local web dashboard shows telemetry over Wi-Fi.

### Phase 4 — Android APK

- Camera setup handled programmatically.
- On-device or hybrid analysis.
- Optional local HTTP dashboard.

## Possible architecture

```text
Audio Phone  --->
                 \
                  ---> Laptop Server/Dashboard ---> Browser Monitor
                 /
Video Phone  --->
```

## Shot record example

```json
{
  "shotId": 42,
  "distanceM": 10.0,
  "audio": {
    "flightTimeMs": 118.6,
    "speedMps": 84.3,
    "confidence": 0.96
  },
  "video": {
    "trajectory": "available",
    "approachAngleDeg": 1.8,
    "oscillationScore": 0.72
  },
  "status": "ok"
}
```

## Initial tech stack

- **Python + OpenCV** for prototype analysis on laptop
- **FastAPI** or **Node/Express** for local dashboard server
- **WebSocket** for live telemetry updates
- **Android CameraX / Camera2** later for APK capture

## Suggested milestones

1. Validate audio speed measurement.
2. Capture usable side-view slow-motion video.
3. Detect arrow reliably in sample frames.
4. Track arrow across a full shot.
5. Plot trajectory and oscillation.
6. Merge audio + video into a single telemetry model.
7. Build the dashboard.
8. Port the proven pipeline to Android.

## Naming note

This repo is named **ArrowTelemetry** because it is broad enough for speed, tracking, graphs, and future APK/dashboard work.

Alternative names:

- ArrowScope
- FlightTrace
- BowTelemetry
- ArrowLab
- FlightLine

## Next step

Create a small sample dataset folder and test 10–20 shots before writing any mobile app code.
