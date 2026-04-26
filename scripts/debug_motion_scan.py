"""Diagnostic: scan a shot's raw clip frame-by-frame, report where motion is.

Compares each frame to a stable bg (median of first N frames), counts pixels
above a diff threshold inside the annotation's corridor mask. Prints a table
of frame -> motion magnitude so we can see where the arrow ACTUALLY is vs
where the audio-defined flight window says it should be.

Usage:
    uv run python scripts/debug_motion_scan.py <shot_number>
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

DATA_RAW = Path("data/raw")
SESSION_FILE = DATA_RAW / "sessions" / "_active.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shot", type=int)
    ap.add_argument("--bg-frames", type=int, default=10)
    ap.add_argument("--threshold", type=int, default=25)
    args = ap.parse_args()

    if not SESSION_FILE.exists():
        print("no active session", file=sys.stderr)
        return 1
    sess = json.loads(SESSION_FILE.read_text())
    stem = sess["stem"]
    annot = sess.get("annotation") or {}
    corridor = annot.get("corridor") or {}

    clip = DATA_RAW / "sessions" / f"{stem}_shot{args.shot:02d}.mp4"
    if not clip.exists():
        print(f"clip not found: {clip}", file=sys.stderr)
        return 1

    # Trajectory yaml for context
    traj_path = Path("data/processed/trajectories") / f"{stem}_shot{args.shot:02d}.yaml"
    traj = yaml.safe_load(traj_path.read_text()) if traj_path.exists() else {}
    fw_a, fw_b = traj.get("flight_window", [None, None])

    cap = cv2.VideoCapture(str(clip))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()

    if not frames:
        print("no frames", file=sys.stderr)
        return 1

    h, w = frames[0].shape[:2]
    print(f"clip: {len(frames)} frames at {w}x{h}")
    print(f"corridor: y_top={corridor.get('y_top')} y_bottom={corridor.get('y_bottom')}")
    print(f"audio flight_window: {fw_a}..{fw_b}")
    print()

    # Bg = median of first N frames (stable, no arrow yet)
    bg_n = min(args.bg_frames, len(frames))
    bg = np.median(np.stack([f for f in frames[:bg_n]]), axis=0).astype(np.uint8)
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)

    # Corridor mask
    mask_full = np.ones((h, w), dtype=np.uint8) * 255
    mask_corridor = np.zeros((h, w), dtype=np.uint8)
    y_top = corridor.get("y_top", 0)
    y_bottom = corridor.get("y_bottom", h)
    mask_corridor[y_top:y_bottom, :] = 255

    # Per-frame diff inside corridor + outside (to compare)
    print(f"{'frame':>5} {'in_corr':>8} {'out_corr':>9}  marker")
    for i, f in enumerate(frames):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray, bg_gray)
        thr = (diff > args.threshold).astype(np.uint8) * 255
        in_c = int(np.count_nonzero(cv2.bitwise_and(thr, mask_corridor)))
        out_c = int(np.count_nonzero(cv2.bitwise_and(thr, cv2.bitwise_not(mask_corridor))))
        # Highlight frames with significant in-corridor motion
        marker = ""
        if fw_a is not None and fw_a <= i + 1 <= fw_b:
            marker += "[FLIGHT] "
        if in_c > 200:
            marker += "**"
        print(f"{i+1:>5} {in_c:>8} {out_c:>9}  {marker}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
