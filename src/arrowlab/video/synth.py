from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

TRAJECTORY_DIR = Path("data/processed/trajectories")
SYNTH_DIR = Path("data/processed/synth")

BG_COLOR = (15, 15, 15)
GRID_COLOR = (40, 40, 40)
CORRIDOR_COLOR = (60, 60, 60)
BBOX_COLOR = (0, 255, 255)
TRAIL_COLOR = (255, 200, 0)
ARROW_COLOR = (80, 255, 80)
HIT_COLOR = (80, 80, 255)
TEXT_COLOR = (220, 220, 220)


@dataclass
class Fit:
    sx: float
    ix: float
    sy: float
    iy: float
    f_first: float
    f_last: float
    f_hit: float
    hit_x: float
    hit_y: float


def draw_grid(canvas: np.ndarray, step: int = 100) -> None:
    h, w = canvas.shape[:2]
    for x in range(0, w, step):
        cv2.line(canvas, (x, 0), (x, h), GRID_COLOR, 1)
    for y in range(0, h, step):
        cv2.line(canvas, (0, y), (w, y), GRID_COLOR, 1)


def draw_target_face(canvas: np.ndarray, target: dict) -> None:
    cx, cy, r = target["cx"], target["cy"], int(target["r"])
    for ring_r, color in [
        (r, (255, 255, 255)),
        (int(r * 0.75), (0, 0, 255)),
        (int(r * 0.5), (0, 215, 255)),
        (int(r * 0.25), (0, 255, 255)),
    ]:
        cv2.circle(canvas, (cx, cy), ring_r, color, 2)
    cv2.circle(canvas, (cx, cy), 3, (255, 255, 255), -1)


def fit_trajectory(detections: list[dict], target_cx: int | None) -> Fit | None:
    if len(detections) < 2:
        return None
    frames = np.array([d["frame"] for d in detections], dtype=float)
    xs = np.array([d["x"] for d in detections], dtype=float)
    ys = np.array([d["y"] for d in detections], dtype=float)
    sx, ix = np.polyfit(frames, xs, 1)
    sy, iy = np.polyfit(frames, ys, 1)
    if abs(sx) < 1e-6:
        return None
    f_first = float(frames[0])
    f_last = float(frames[-1])
    hit_x = float(target_cx) if target_cx is not None else float(sx * f_last + ix)
    f_hit = (hit_x - ix) / sx
    hit_y = sy * f_hit + iy
    return Fit(sx=sx, ix=ix, sy=sy, iy=iy, f_first=f_first, f_last=f_last,
               f_hit=f_hit, hit_x=hit_x, hit_y=hit_y)


def draw_trajectory_line(
    canvas: np.ndarray,
    fit: Fit,
    f_end: float,
    color: tuple[int, int, int],
    thickness: int = 1,
    samples: int = 80,
) -> None:
    if f_end <= fit.f_first:
        return
    f_end = min(f_end, fit.f_hit)
    fs = np.linspace(fit.f_first, f_end, samples)
    xs = fit.sx * fs + fit.ix
    ys = fit.sy * fs + fit.iy
    pts = np.column_stack([xs, ys]).astype(np.int32)
    cv2.polylines(canvas, [pts], isClosed=False, color=color,
                  thickness=thickness, lineType=cv2.LINE_AA)


def position_at(fit: Fit, f: float) -> tuple[int, int]:
    x = fit.sx * f + fit.ix
    y = fit.sy * f + fit.iy
    return int(x), int(y)


def render(trajectory_path: Path, output_path: Path) -> None:
    with trajectory_path.open() as f:
        data = yaml.safe_load(f)

    fps = data["fps"]
    w = data["width"]
    h = data["height"]
    annotation = data.get("annotation") or {}
    detections = data.get("detections") or []
    fw_start, fw_end = data["flight_window"]
    clip_start = data.get("clip_start_frame", fw_start)
    clip_end = data.get("clip_end_frame", fw_end)
    label = data.get("video", "")

    corridor = annotation.get("corridor")
    target = annotation.get("target") or {}
    face_d_m = target.get("face_diameter_m")
    px_per_m = (2 * target["r"]) / face_d_m if target.get("r") and face_d_m else None

    fit = fit_trajectory(detections, target.get("cx"))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (w, h))

    for frame_num in range(clip_start, clip_end + 1):
        canvas = np.full((h, w, 3), BG_COLOR, dtype=np.uint8)
        draw_grid(canvas)

        if corridor:
            cv2.line(canvas, (0, corridor["y_top"]), (w, corridor["y_top"]), CORRIDOR_COLOR, 1)
            cv2.line(canvas, (0, corridor["y_bottom"]), (w, corridor["y_bottom"]), CORRIDOR_COLOR, 1)

        if target.get("bbox"):
            x0, y0, x1, y1 = target["bbox"]
            cv2.rectangle(canvas, (x0, y0), (x1, y1), BBOX_COLOR, 2)
        if target.get("r"):
            draw_target_face(canvas, target)

        if fit is not None:
            draw_trajectory_line(canvas, fit, float(frame_num), TRAIL_COLOR, thickness=1)

            if fit.f_first <= frame_num <= fit.f_hit:
                cx, cy = position_at(fit, float(frame_num))
                cv2.circle(canvas, (cx, cy), 6, ARROW_COLOR, -1)
                cv2.circle(canvas, (cx, cy), 10, ARROW_COLOR, 1)

            if frame_num >= fit.f_hit:
                hx, hy = int(fit.hit_x), int(fit.hit_y)
                cv2.drawMarker(canvas, (hx, hy), HIT_COLOR,
                               markerType=cv2.MARKER_CROSS, markerSize=26, thickness=2)
                if target.get("r"):
                    dist_px = float(np.hypot(hx - target["cx"], hy - target["cy"]))
                    msg = f"hit offset from center: {dist_px:.1f}px"
                    if px_per_m:
                        msg += f" ({dist_px / px_per_m * 100:.1f} cm)"
                    cv2.putText(canvas, msg, (20, h - 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, HIT_COLOR, 2)

        t = (frame_num - clip_start) / fps
        cv2.putText(canvas, f"frame {frame_num}  t={t:.2f}s",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, TEXT_COLOR, 2)
        cv2.putText(canvas, label,
                    (20, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_COLOR, 2)
        shot = data.get("shot_index")
        if shot is not None:
            cv2.putText(canvas, f"shot {shot + 1}",
                        (w - 200, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, TEXT_COLOR, 2)
        writer.write(canvas)

    writer.release()
    from arrowlab.video.encode import to_h264_faststart
    to_h264_faststart(output_path)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("trajectory", type=Path)
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    out = args.output or SYNTH_DIR / f"{args.trajectory.stem}_synth.mp4"
    render(args.trajectory, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
