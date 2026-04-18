from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
import yaml

from arrowlab.video.synth import (
    BBOX_COLOR,
    BG_COLOR,
    CORRIDOR_COLOR,
    TEXT_COLOR,
    draw_grid,
    draw_target_face,
    draw_trajectory_line,
    fit_trajectory,
    position_at,
)

SYNTH_DIR = Path("data/processed/synth")
TRACKED_DIR = Path("data/processed/tracked")

SHOT_COLORS = [
    (80, 255, 80),    # green
    (80, 80, 255),    # red
    (255, 200, 0),    # cyan/blue
    (80, 255, 255),   # yellow
    (255, 80, 255),   # magenta
]

GAP_FRAMES = 30
FINAL_LINGER_FRAMES = 120


def _tracked_path_for(data: dict) -> Path:
    explicit = data.get("tracked_path")
    if explicit:
        return Path(explicit)
    video = data.get("video", "")
    stem = Path(video).stem
    shot = (data.get("shot_index") or 0) + 1
    return TRACKED_DIR / f"{stem}_shot{shot}_tracked.mp4"


def _load_frames(path: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


def _render_synth_canvas(
    w: int, h: int,
    t_src: float,
    data_list: list[dict],
    schedules: list[float],
    fits: list,
    corridor: dict | None,
    target: dict,
) -> tuple[np.ndarray, int | None]:
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

    active_idx: int | None = None
    for idx, data in enumerate(data_list):
        fit = fits[idx]
        if fit is None:
            continue
        local_t = t_src - schedules[idx]
        if local_t < 0:
            continue
        current_f = fit.f_first + local_t
        color = SHOT_COLORS[idx % len(SHOT_COLORS)]

        draw_trajectory_line(canvas, fit, current_f, color, thickness=1)

        if fit.f_first <= current_f < fit.f_hit:
            px, py = position_at(fit, current_f)
            cv2.circle(canvas, (px, py), 6, color, -1)
            active_idx = idx

        if current_f >= fit.f_hit:
            hx, hy = int(fit.hit_x), int(fit.hit_y)
            cv2.drawMarker(canvas, (hx, hy), color,
                           markerType=cv2.MARKER_CROSS, markerSize=22, thickness=2)

    cv2.putText(canvas, "combined shots",
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, TEXT_COLOR, 2)
    for idx, data in enumerate(data_list):
        color = SHOT_COLORS[idx % len(SHOT_COLORS)]
        y0 = 60 + 30 * idx
        cv2.rectangle(canvas, (20, y0), (50, y0 + 20), color, -1)
        label = f"shot {idx + 1}"
        if idx == active_idx:
            label += "  <<"
        cv2.putText(canvas, label, (60, y0 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_COLOR, 2)

    return canvas, active_idx


def _find_current_shot(t_src: float, schedules: list[float]) -> int | None:
    idx = None
    for i, s in enumerate(schedules):
        if s <= t_src:
            idx = i
    return idx


def render_combined(
    trajectory_paths: list[Path],
    output: Path,
    speed: float = 0.4,
    simultaneous: bool = False,
    split: bool = True,
) -> None:
    data_list: list[dict] = []
    for p in trajectory_paths:
        with p.open() as f:
            data_list.append(yaml.safe_load(f))
    if not data_list:
        raise RuntimeError("no trajectories supplied")

    first = data_list[0]
    w = first["width"]
    h = first["height"]
    fps = first["fps"]
    annotation = first.get("annotation") or {}
    corridor = annotation.get("corridor")
    target = annotation.get("target") or {}
    target_cx = target.get("cx")

    fits = [fit_trajectory(d.get("detections") or [], target_cx) for d in data_list]
    flight_lengths = [(fit.f_hit - fit.f_first) if fit else 0.0 for fit in fits]
    max_flight = max(flight_lengths) if flight_lengths else 0.0

    if simultaneous:
        schedules = [0.0 for _ in data_list]
        total_src_frames = int(max_flight) + FINAL_LINGER_FRAMES
    else:
        slot = max_flight + GAP_FRAMES
        schedules = [i * slot for i in range(len(data_list))]
        total_src_frames = int(len(data_list) * slot + FINAL_LINGER_FRAMES)

    tracked_paths = [_tracked_path_for(d) for d in data_list]

    output.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out_h = h if not split else h
    writer = cv2.VideoWriter(str(output), fourcc, fps, (w, out_h))

    loaded_idx = -1
    current_tracked_frames: list[np.ndarray] = []
    placeholder_top = np.zeros((h // 2, w, 3), dtype=np.uint8)

    total_out_frames = int(total_src_frames / speed)
    for i in range(total_out_frames):
        t_src = i * speed
        synth_full, active_idx = _render_synth_canvas(
            w, h, t_src, data_list, schedules, fits, corridor, target
        )

        if not split:
            writer.write(synth_full)
            continue

        current_idx = _find_current_shot(t_src, schedules)
        if current_idx is not None and current_idx != loaded_idx:
            path = tracked_paths[current_idx]
            current_tracked_frames = _load_frames(path) if path.exists() else []
            loaded_idx = current_idx

        top = placeholder_top
        if current_idx is not None and current_tracked_frames:
            data = data_list[current_idx]
            fit = fits[current_idx]
            local_t = t_src - schedules[current_idx]
            if fit is not None:
                current_f = fit.f_first + max(0.0, local_t)
                tr_idx = int(round(current_f - data.get("clip_start_frame", fit.f_first)))
                tr_idx = max(0, min(tr_idx, len(current_tracked_frames) - 1))
                top = cv2.resize(current_tracked_frames[tr_idx], (w, h // 2))

        bottom = cv2.resize(synth_full, (w, h // 2))
        divider_y = h // 2
        composite = np.vstack([top, bottom])
        cv2.line(composite, (0, divider_y), (w, divider_y), (60, 60, 60), 1)

        writer.write(composite)

    writer.release()
    from arrowlab.video.encode import to_h264_faststart
    to_h264_faststart(output)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("trajectories", nargs="+", type=Path)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--speed", type=float, default=0.4,
                   help="source frames advanced per output frame (0.4 = 2.5x slower)")
    p.add_argument("--simultaneous", action="store_true",
                   help="all shots at once (default: sequential)")
    p.add_argument("--no-split", action="store_true",
                   help="disable split view (synth-only)")
    args = p.parse_args()

    output = args.output
    if output is None:
        stem = args.trajectories[0].stem.rsplit("_shot", 1)[0]
        output = SYNTH_DIR / f"{stem}_combined.mp4"
    render_combined(
        args.trajectories, output,
        speed=args.speed, simultaneous=args.simultaneous, split=not args.no_split,
    )
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
