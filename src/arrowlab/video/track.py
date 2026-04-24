from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import yaml

DATA_RAW = Path("data/raw")
ANNOTATIONS_PATH = DATA_RAW / "annotations.yaml"
TRAJECTORY_DIR = Path("data/processed/trajectories")
TRACKED_DIR = Path("data/processed/tracked")
SHOT_CLIPS_DIR = Path("data/processed/shot_clips")

PAD_FRAMES = 30
MIN_ASPECT_RATIO = 2.0
TRAJ_RESIDUAL_PX = 20.0


def video_key(video_path: Path) -> str:
    return video_path.resolve().relative_to(DATA_RAW.resolve()).as_posix()


def load_annotation(video_path: Path) -> dict | None:
    if not ANNOTATIONS_PATH.exists():
        return None
    with ANNOTATIONS_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    return (data.get("videos") or {}).get(video_key(video_path))


def probe_fps(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    rate = json.loads(result.stdout)["streams"][0]["avg_frame_rate"]
    num, den = rate.split("/")
    return float(num) / float(den)


def extract_shot_clip(
    source: Path,
    fw_start: int,
    fw_end: int,
    fps: float,
    output: Path,
) -> tuple[int, int]:
    start_frame = max(1, fw_start - PAD_FRAMES)
    end_frame = fw_end + PAD_FRAMES
    start_t = (start_frame - 1) / fps
    duration = (end_frame - start_frame + 1) / fps
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_t:.3f}",
            "-t", f"{duration:.3f}",
            "-i", str(source),
            "-c", "copy",
            str(output),
        ],
        check=True, capture_output=True,
    )
    return start_frame, end_frame


def read_all_frames(clip_path: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(clip_path))
    frames: list[np.ndarray] = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


def auto_detect_flight(
    frames: list[np.ndarray],
    bg_gray: np.ndarray,
    roi_mask: np.ndarray,
    diff_threshold: int = 25,
    min_floor: int = 200,
    baseline_mult: float = 3.0,
    min_length: int = 3,
    merge_gap: int = 2,
) -> tuple[int, int] | None:
    """Return (start_idx, end_idx) 0-based within `frames` where motion spikes.

    Returns None if no clear flight detected.
    """
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
    threshold = max(float(min_floor), baseline * baseline_mult + 50.0)
    above = scores > threshold
    if not above.any():
        return None

    # merge small gaps
    idx = np.where(above)[0]
    runs: list[tuple[int, int]] = []
    run_start = int(idx[0])
    run_end = int(idx[0])
    for v in idx[1:]:
        if int(v) - run_end <= merge_gap:
            run_end = int(v)
        else:
            runs.append((run_start, run_end))
            run_start = run_end = int(v)
    runs.append((run_start, run_end))

    runs = [r for r in runs if r[1] - r[0] + 1 >= min_length]
    if not runs:
        return None
    runs.sort(key=lambda r: np.sum(scores[r[0]:r[1] + 1]), reverse=True)
    return runs[0]


def build_roi_mask(
    shape: tuple[int, int],
    annotation: dict | None,
    exclude_target: bool = True,
) -> np.ndarray:
    h, w = shape
    mask = np.full((h, w), 255, dtype=np.uint8)
    if not annotation:
        return mask
    corridor = annotation.get("corridor")
    if corridor:
        mask[: corridor["y_top"], :] = 0
        mask[corridor["y_bottom"] :, :] = 0
    if exclude_target:
        target = annotation.get("target") or {}
        if target.get("bbox"):
            x0, y0, x1, y1 = target["bbox"]
            mask[y0:y1, x0:x1] = 0
        elif target.get("r"):
            cv2.circle(mask, (target["cx"], target["cy"]), int(target["r"]), 0, thickness=-1)
    return mask


def _hough_pick_line(
    roi: np.ndarray,
    max_off_angle_deg: float = 35.0,
) -> tuple[tuple[int, int], tuple[int, int], float] | None:
    h, w = roi.shape[:2]
    if w < 20 or h < 3:
        return None
    votes = max(20, int(w * 0.3))
    min_len = max(20, int(w * 0.5))
    lines = cv2.HoughLinesP(
        roi, rho=1, theta=np.pi / 180,
        threshold=votes, minLineLength=min_len, maxLineGap=10,
    )
    if lines is None:
        return None
    best: tuple[float, tuple[int, int], tuple[int, int]] | None = None
    for x1, y1, x2, y2 in lines[:, 0, :]:
        length = float(np.hypot(x2 - x1, y2 - y1))
        angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
        if angle > max_off_angle_deg and angle < 180.0 - max_off_angle_deg:
            continue
        if best is None or length > best[0]:
            best = (length, (int(x1), int(y1)), (int(x2), int(y2)))
    if best is None:
        return None
    return best[1], best[2], best[0]


def detect_arrow(
    gray: np.ndarray,
    bg_gray: np.ndarray,
    roi_mask: np.ndarray,
    diff_threshold: int,
    min_area: int,
    target_cx: int | None = None,
) -> dict | None:
    diff = cv2.absdiff(gray, bg_gray)
    _, mask = cv2.threshold(diff, diff_threshold, 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_and(mask, roi_mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best: tuple[float, np.ndarray, tuple[int, int, int, int]] | None = None
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        bx, by, bw, bh = cv2.boundingRect(c)
        aspect = max(bw, bh) / max(min(bw, bh), 1)
        if aspect < MIN_ASPECT_RATIO:
            continue
        score = area * aspect
        if best is None or score > best[0]:
            best = (score, c, (bx, by, bw, bh))
    if best is None:
        return None
    _, _contour, (bx, by, bw, bh) = best

    pad = 3
    rx0 = max(0, bx - pad)
    ry0 = max(0, by - pad)
    rx1 = min(mask.shape[1], bx + bw + pad)
    ry1 = min(mask.shape[0], by + bh + pad)
    roi = mask[ry0:ry1, rx0:rx1]

    hough = _hough_pick_line(roi)
    length = float(bw)
    angle_deg = 0.0
    if hough is not None:
        p1_local, p2_local, length = hough
        p1 = (p1_local[0] + rx0, p1_local[1] + ry0)
        p2 = (p2_local[0] + rx0, p2_local[1] + ry0)
    else:
        p1 = (bx, by + bh // 2)
        p2 = (bx + bw, by + bh // 2)

    if target_cx is not None:
        if abs(p2[0] - target_cx) < abs(p1[0] - target_cx):
            tip, tail = p2, p1
        else:
            tip, tail = p1, p2
    else:
        tip, tail = (p2, p1) if p2[0] >= p1[0] else (p1, p2)

    angle_deg = float(np.degrees(np.arctan2(tip[1] - tail[1], tip[0] - tail[0])))
    return {
        "tip": tip,
        "tail": tail,
        "bbox": (bx, by, bw, bh),
        "length": length,
        "angle": angle_deg,
        "hough": hough is not None,
    }


def clean_trajectory(detections: list[dict], min_threshold: float = 50.0) -> list[dict]:
    if len(detections) < 5:
        return detections
    frames = np.array([d["frame"] for d in detections], dtype=float)
    xs = np.array([d["x"] for d in detections], dtype=float)
    ys = np.array([d["y"] for d in detections], dtype=float)

    sx, ix = np.polyfit(frames, xs, 1)
    sy, iy = np.polyfit(frames, ys, 1)
    resid = np.hypot(xs - (sx * frames + ix), ys - (sy * frames + iy))
    threshold = max(min_threshold, float(np.median(resid) * 4))
    mask = resid < threshold

    if mask.sum() >= 3 and mask.sum() < len(mask):
        sx, ix = np.polyfit(frames[mask], xs[mask], 1)
        sy, iy = np.polyfit(frames[mask], ys[mask], 1)
        resid = np.hypot(xs - (sx * frames + ix), ys - (sy * frames + iy))
        threshold = max(min_threshold, float(np.median(resid[mask]) * 4))
        mask = resid < threshold
    return [d for d, m in zip(detections, mask) if m]


def track_clip(
    clip_path: Path,
    annotation: dict,
    fw_start: int,
    fw_end: int,
    *,
    fps: float | None = None,
    clip_start_frame: int = 1,
    clip_end_frame: int | None = None,
    output_stem: str | None = None,
    video_label: str | None = None,
    shot_index: int = 0,
    diff_threshold: int = 25,
    min_area: int = 150,
    auto_flight: bool = False,
    log_prefix: str = "shot",
    write_pre_pad_frames: int = 60,
    write_post_pad_frames: int = 60,
    frames: list | None = None,
) -> Path:
    """Process a pre-extracted clip and write tracked mp4 + trajectory yaml.

    `fw_start`/`fw_end` are in the SAME frame-numbering as clip_start_frame
    (e.g., if clip_start_frame=1, they're 1-indexed within the clip).

    If `frames` is provided, skip decoding the clip from disk.

    Returns the trajectory yaml path.
    """
    import time as _t
    _t0 = _t.perf_counter()
    if frames is None:
        frames = read_all_frames(clip_path)
    _t_decode = _t.perf_counter()
    if not frames:
        raise RuntimeError(f"no frames read from {clip_path}")
    if fps is None:
        fps = probe_fps(clip_path)
    if clip_end_frame is None:
        clip_end_frame = clip_start_frame + len(frames) - 1
    h, w = frames[0].shape[:2]
    if output_stem is None:
        output_stem = clip_path.stem

    pre_count = max(1, fw_start - clip_start_frame)
    bg_pool = frames[: min(pre_count, len(frames))]
    if len(bg_pool) < 3:
        bg_pool = frames
    # Subsample to ~15 evenly spaced frames before np.median — stacking hundreds
    # of 1920x1080 frames burns gigabytes of RAM and seconds of CPU.
    stride = max(1, len(bg_pool) // 15)
    bg_samples = bg_pool[::stride][:15]
    bg_gray = cv2.cvtColor(
        np.median(np.stack(bg_samples), axis=0).astype(np.uint8),
        cv2.COLOR_BGR2GRAY,
    )

    roi_mask = build_roi_mask((h, w), annotation)
    target = annotation.get("target") or {}
    target_cx = target.get("cx")

    if auto_flight:
        auto = auto_detect_flight(frames, bg_gray, roi_mask, diff_threshold=diff_threshold)
        if auto is None:
            raise RuntimeError(f"auto-flight failed on {log_prefix}")
        si, _ei = auto
        fw_start = clip_start_frame + max(0, si - 2)
        fw_end = clip_end_frame

    detections: list[dict] = []
    for i, f in enumerate(frames):
        global_frame = clip_start_frame + i
        if not (fw_start <= global_frame <= fw_end):
            continue
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        result = detect_arrow(gray, bg_gray, roi_mask, diff_threshold, min_area, target_cx)
        if result is None:
            continue
        tip = result["tip"]
        tail = result["tail"]
        bx, by, bw, bh = result["bbox"]
        detections.append({
            "frame": global_frame,
            "x": int(tip[0]),
            "y": int(tip[1]),
            "tail_x": int(tail[0]),
            "tail_y": int(tail[1]),
            "bbox": [int(bx), int(by), int(bw), int(bh)],
            "length": float(result["length"]),
            "angle": float(result["angle"]),
            "hough": bool(result["hough"]),
        })

    raw_count = len(detections)
    cleaned = clean_trajectory(detections)
    _t_detect = _t.perf_counter()

    TRACKED_DIR.mkdir(parents=True, exist_ok=True)
    tracked_path = TRACKED_DIR / f"{output_stem}_tracked.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    # Round fps: source mp4s from the phone's MediaCodec encoder embed a
    # fine-grained timebase (e.g. 1/89409) that MPEG-4 Part 2 (mp4v)
    # rejects with "maximum admitted denominator is 65535".
    writer_fps = max(1, int(round(fps)))
    # Downscale the tracked mp4 to ~960px wide to halve encode time. The
    # operator only reviews these clips visually; detection already ran at
    # full resolution so accuracy is unaffected.
    out_w, out_h = w, h
    scale = 1.0
    if w > 960:
        scale = 960.0 / w
        out_w = 960
        out_h = int(round(h * scale))
        if out_h % 2 == 1: out_h -= 1
    writer = cv2.VideoWriter(str(tracked_path), fourcc, float(writer_fps), (out_w, out_h))
    kept_by_frame = {d["frame"]: d for d in cleaned}
    trail: list[tuple[int, int]] = []

    # Only emit the flight window + small pre/post pad. Writing the full 6s ring
    # buffer would cost ~30x more encoding time.
    write_first_frame = max(clip_start_frame, fw_start - write_pre_pad_frames)
    write_last_frame = min(clip_end_frame, fw_end + write_post_pad_frames)

    corridor = annotation.get("corridor")
    for i, f in enumerate(frames):
        global_frame = clip_start_frame + i
        if global_frame < write_first_frame or global_frame > write_last_frame:
            continue
        vis = f.copy()
        if corridor:
            cv2.line(vis, (0, corridor["y_top"]), (w, corridor["y_top"]), (200, 200, 0), 1)
            cv2.line(vis, (0, corridor["y_bottom"]), (w, corridor["y_bottom"]), (200, 200, 0), 1)
        if target.get("bbox"):
            x0, y0, x1, y1 = target["bbox"]
            cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 255, 255), 2)
        if target.get("r"):
            cv2.circle(vis, (target["cx"], target["cy"]), int(target["r"]), (0, 165, 255), 2)

        d = kept_by_frame.get(global_frame)
        if d:
            bx, by, bw, bh = d.get("bbox", [d["x"], d["y"], 0, 0])
            tlx = d.get("tail_x", d["x"])
            tly = d.get("tail_y", d["y"])
            trail.append((d["x"], d["y"]))
            cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
            cv2.line(vis, (tlx, tly), (d["x"], d["y"]), (0, 200, 255), 2, lineType=cv2.LINE_AA)
            cv2.circle(vis, (d["x"], d["y"]), 6, (0, 0, 255), -1)
            cv2.circle(vis, (tlx, tly), 4, (200, 200, 0), 1)
        for p in trail:
            cv2.circle(vis, p, 2, (255, 255, 0), -1)

        cv2.putText(
            vis, f"frame {global_frame}", (10, 40),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2,
        )
        if scale != 1.0:
            vis = cv2.resize(vis, (out_w, out_h), interpolation=cv2.INTER_AREA)
        writer.write(vis)
    writer.release()
    _t_write = _t.perf_counter()
    from arrowlab.video.encode import to_h264_faststart
    to_h264_faststart(tracked_path)
    _t_encode = _t.perf_counter()
    print(
        f"{log_prefix}: decode2={_t_decode-_t0:.1f}s "
        f"detect={_t_detect-_t_decode:.1f}s "
        f"write={_t_write-_t_detect:.1f}s "
        f"h264={_t_encode-_t_write:.1f}s"
    )

    TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)
    traj_path = TRAJECTORY_DIR / f"{output_stem}.yaml"
    with traj_path.open("w") as fh:
        yaml.safe_dump(
            {
                "video": video_label or clip_path.name,
                "clip_path": str(clip_path).replace("\\", "/"),
                "tracked_path": str(tracked_path).replace("\\", "/"),
                "shot_index": shot_index,
                "fps": fps,
                "width": w,
                "height": h,
                "clip_start_frame": clip_start_frame,
                "clip_end_frame": clip_end_frame,
                "flight_window": [fw_start, fw_end],
                "tracked_first_frame": write_first_frame,
                "tracked_last_frame": write_last_frame,
                "annotation": annotation,
                "detections_raw": raw_count,
                "detections_kept": len(cleaned),
                "detections": cleaned,
            },
            fh,
            sort_keys=False,
        )

    print(f"{log_prefix}: raw={raw_count} kept={len(cleaned)} -> {tracked_path.name}")
    return traj_path


def track_shot(
    video_path: Path,
    shot_index: int,
    diff_threshold: int = 25,
    min_area: int = 150,
    auto_flight: bool = False,
) -> None:
    annotation = load_annotation(video_path)
    if not annotation:
        raise RuntimeError(f"no annotation for {video_key(video_path)}")
    shots = annotation.get("shots") or []
    if shot_index >= len(shots):
        raise RuntimeError(f"shot {shot_index} out of range ({len(shots)} shots)")
    fw_start, fw_end = (int(v) for v in shots[shot_index]["flight_window"])

    fps = probe_fps(video_path)
    suffix = f"_shot{shot_index + 1}"
    shot_clip = SHOT_CLIPS_DIR / f"{video_path.stem}{suffix}.mp4"
    clip_start_frame, clip_end_frame = extract_shot_clip(
        video_path, fw_start, fw_end, fps, shot_clip
    )
    track_clip(
        shot_clip, annotation, fw_start, fw_end,
        fps=fps,
        clip_start_frame=clip_start_frame,
        clip_end_frame=clip_end_frame,
        output_stem=f"{video_path.stem}{suffix}",
        video_label=video_key(video_path),
        shot_index=shot_index,
        diff_threshold=diff_threshold,
        min_area=min_area,
        auto_flight=auto_flight,
        log_prefix=f"shot {shot_index + 1}",
    )


def evaluate_auto_detect(video_path: Path, diff_threshold: int = 25) -> None:
    annotation = load_annotation(video_path)
    if not annotation:
        raise SystemExit(f"no annotation for {video_key(video_path)}")
    shots = annotation.get("shots") or []
    if not shots:
        raise SystemExit("no shots annotated")
    fps = probe_fps(video_path)

    header = f"{'shot':>4}  {'gt':>14}  {'detected':>14}  {'dstart':>6} {'dend':>6}  {'IoU':>5}"
    print(header)
    print("-" * len(header))

    ious: list[float] = []
    for idx, shot in enumerate(shots):
        fw_start, fw_end = (int(v) for v in shot["flight_window"])
        suffix = f"_shot{idx + 1}"
        shot_clip = SHOT_CLIPS_DIR / f"{video_path.stem}{suffix}.mp4"
        if not shot_clip.exists():
            extract_shot_clip(video_path, fw_start, fw_end, fps, shot_clip)

        frames = read_all_frames(shot_clip)
        if not frames:
            print(f"shot {idx + 1}: no frames")
            continue
        h, w = frames[0].shape[:2]

        clip_start = max(1, fw_start - PAD_FRAMES)
        bg_gray = cv2.cvtColor(
            np.median(np.stack(frames[::5]), axis=0).astype(np.uint8),
            cv2.COLOR_BGR2GRAY,
        )
        roi_mask = build_roi_mask((h, w), annotation)

        result = auto_detect_flight(frames, bg_gray, roi_mask, diff_threshold=diff_threshold)
        if result is None:
            print(f"{idx+1:>4}  [{fw_start:>5},{fw_end:>5}]  {'no detect':>14}  {'-':>6} {'-':>6}  {'-':>5}")
            continue
        si, ei = result
        det_start = clip_start + si
        det_end = clip_start + ei

        inter = max(0, min(fw_end, det_end) - max(fw_start, det_start) + 1)
        union = max(fw_end, det_end) - min(fw_start, det_start) + 1
        iou = inter / union if union > 0 else 0.0
        ious.append(iou)
        gt = f"[{fw_start},{fw_end}]"
        det = f"[{det_start},{det_end}]"
        print(f"{idx+1:>4}  {gt:>14}  {det:>14}  {det_start - fw_start:>+6d} {det_end - fw_end:>+6d}  {iou:>5.3f}")

    if ious:
        print("-" * len(header))
        print(f"mean IoU: {np.mean(ious):.3f}   min: {min(ious):.3f}   n={len(ious)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video", type=Path)
    p.add_argument("--shot", type=int, default=0)
    p.add_argument("--all-shots", action="store_true")
    p.add_argument("--threshold", type=int, default=25)
    p.add_argument("--min-area", type=int, default=150)
    p.add_argument("--evaluate-auto", action="store_true",
                   help="compare auto-flight-detect vs ground-truth windows")
    p.add_argument("--auto", action="store_true",
                   help="use auto-detected flight window instead of annotated")
    args = p.parse_args()

    if args.evaluate_auto:
        evaluate_auto_detect(args.video, diff_threshold=args.threshold)
        return

    if args.all_shots:
        ann = load_annotation(args.video) or {}
        n = len(ann.get("shots") or [])
        if n == 0:
            raise SystemExit("no shots annotated")
        for i in range(n):
            track_shot(args.video, i, diff_threshold=args.threshold,
                       min_area=args.min_area, auto_flight=args.auto)
    else:
        track_shot(args.video, args.shot, diff_threshold=args.threshold,
                   min_area=args.min_area, auto_flight=args.auto)


if __name__ == "__main__":
    main()
