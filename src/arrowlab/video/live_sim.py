from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import cv2
import numpy as np

from arrowlab.video.synth import render as synth_render
from arrowlab.video.synth_combined import render_combined
from arrowlab.video.track import (
    DATA_RAW,
    TRAJECTORY_DIR,
    build_roi_mask,
    load_annotation,
    probe_fps,
    track_clip,
    video_key,
)

SESSIONS_DIR = DATA_RAW / "sessions"
SYNTH_DIR = Path("data/processed/synth")
TRACKED_DIR = Path("data/processed/tracked")

PRE_PAD_S = 2.0
POST_PAD_S = 4.0


def parse_timecode(s: str) -> float:
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"bad timecode: {s}")


def ffmpeg_slice(source: Path, start_s: float, duration_s: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.3f}",
            "-t", f"{duration_s:.3f}",
            "-i", str(source),
            "-c", "copy",
            str(output),
        ],
        check=True, capture_output=True,
    )


def read_clip_frames(path: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    out: list[np.ndarray] = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    cap.release()
    return out


def auto_detect_flight_in_clip(
    frames: list[np.ndarray],
    roi_mask: np.ndarray,
    diff_threshold: int = 25,
) -> tuple[int, int] | None:
    if not frames:
        return None
    bg_samples = frames[::max(1, len(frames) // 15)][:15]
    bg_gray = cv2.cvtColor(
        np.median(np.stack(bg_samples), axis=0).astype(np.uint8),
        cv2.COLOR_BGR2GRAY,
    )
    scores = np.zeros(len(frames), dtype=np.int64)
    for i, f in enumerate(frames):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray, bg_gray)
        _, mask = cv2.threshold(diff, diff_threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.bitwise_and(mask, roi_mask)
        scores[i] = int(np.count_nonzero(mask))
    if scores.max() == 0:
        return None
    baseline = float(np.median(scores))
    threshold = max(30.0, baseline * 3.0 + 20.0)
    above = scores > threshold
    idxs = np.where(above)[0]
    if len(idxs) == 0:
        return None
    runs: list[tuple[int, int]] = []
    run_start = int(idxs[0])
    run_end = int(idxs[0])
    for v in idxs[1:]:
        if int(v) - run_end <= 2:
            run_end = int(v)
        else:
            runs.append((run_start, run_end))
            run_start = run_end = int(v)
    runs.append((run_start, run_end))
    runs = [r for r in runs if r[1] - r[0] + 1 >= 3]
    if not runs:
        return None
    runs.sort(key=lambda r: np.sum(scores[r[0]:r[1] + 1]), reverse=True)
    return runs[0]


def cleanup_previous_session(source_stem: str) -> None:
    for p in SESSIONS_DIR.glob(f"{source_stem}_shot*_t*.mp4"):
        p.unlink(missing_ok=True)
    for p in TRACKED_DIR.glob(f"{source_stem}_shot*_t*_tracked.mp4"):
        p.unlink(missing_ok=True)
    for p in TRAJECTORY_DIR.glob(f"{source_stem}_shot*_t*.yaml"):
        p.unlink(missing_ok=True)
    for p in SYNTH_DIR.glob(f"{source_stem}_shot*_t*_synth.mp4"):
        p.unlink(missing_ok=True)
    for p in SYNTH_DIR.glob(f"{source_stem}_session_combined.mp4"):
        p.unlink(missing_ok=True)


def process_marker(
    source_video: Path,
    annotation: dict,
    marker_time_s: float,
    marker_idx: int,
    diff_threshold: int,
) -> Path | None:
    start_s = max(0.0, marker_time_s - PRE_PAD_S)
    duration_s = PRE_PAD_S + POST_PAD_S
    slice_name = f"{source_video.stem}_shot{marker_idx + 1}_t{int(round(marker_time_s * 1000))}ms.mp4"
    slice_path = SESSIONS_DIR / slice_name
    print(f"\n[shot {marker_idx + 1}] marker={marker_time_s:.2f}s -> slice {start_s:.2f}s+{duration_s:.2f}s")
    ffmpeg_slice(source_video, start_s, duration_s, slice_path)

    frames = read_clip_frames(slice_path)
    if not frames:
        print("  warning: no frames in slice, skipping")
        return None
    h, w = frames[0].shape[:2]
    roi = build_roi_mask((h, w), annotation)

    detected = auto_detect_flight_in_clip(frames, roi, diff_threshold=diff_threshold)
    if detected is None:
        print("  no flight spike detected in slice")
        return None
    a, b = detected
    slice_fps = probe_fps(slice_path)
    fw_start = a + 1
    fw_end = b + 1
    print(f"  detected flight in slice: frames [{fw_start}, {fw_end}]  ({(b-a+1)/slice_fps:.2f}s)")

    output_stem = slice_path.stem
    traj_path = track_clip(
        slice_path,
        annotation,
        fw_start, fw_end,
        fps=slice_fps,
        clip_start_frame=1,
        clip_end_frame=len(frames),
        output_stem=output_stem,
        video_label=f"{source_video.name} shot {marker_idx + 1} @ {marker_time_s:.2f}s",
        shot_index=marker_idx,
        diff_threshold=diff_threshold,
        log_prefix=f"shot {marker_idx + 1}",
    )
    synth_out = SYNTH_DIR / f"{output_stem}_synth.mp4"
    synth_render(traj_path, synth_out)
    return traj_path


def run(
    source_video: Path,
    markers_s: list[float],
    diff_threshold: int = 25,
) -> None:
    src_key = video_key(source_video)
    annotation = load_annotation(source_video)
    if not annotation or not annotation.get("corridor") or not annotation.get("target"):
        raise SystemExit(f"annotate {src_key} (corridor + target) first")

    cleanup_previous_session(source_video.stem)
    print(f"source fps={probe_fps(source_video):.3f}; {len(markers_s)} markers")

    trajectories: list[Path] = []
    for i, ts in enumerate(markers_s):
        tp = process_marker(source_video, annotation, ts, i, diff_threshold)
        if tp is not None:
            trajectories.append(tp)

    if len(trajectories) >= 2:
        combined_out = SYNTH_DIR / f"{source_video.stem}_session_combined.mp4"
        render_combined(trajectories, combined_out)
        print(f"\ncombined synth -> {combined_out}")
    elif len(trajectories) == 1:
        print("\nsingle shot processed")
    else:
        print("\nno shots succeeded")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video", type=Path)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--shots", help="comma-separated shot marker times (e.g. 9:42,10:15)")
    g.add_argument("--shot-frames", help="comma-separated shot marker frames (e.g. 16971,20811)")
    p.add_argument("--threshold", type=int, default=25)
    args = p.parse_args()

    if args.shots:
        markers = [parse_timecode(s.strip()) for s in args.shots.split(",") if s.strip()]
    else:
        fps = probe_fps(args.video)
        frames = [int(s.strip()) for s in args.shot_frames.split(",") if s.strip()]
        markers = [f / fps for f in frames]
    if not markers:
        raise SystemExit("no markers given")
    run(args.video, markers, diff_threshold=args.threshold)


if __name__ == "__main__":
    main()
