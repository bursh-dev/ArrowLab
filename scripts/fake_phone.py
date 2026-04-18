"""Fake phone simulator for ArrowLab POC.

Connects to the laptop server via WebSocket and plays the role of a
slow-motion phone with a rolling buffer. The server drives everything:

  * On `capture_frame`, extracts a single JPEG from the source video
    and POSTs it to `/api/calibration-frame`.
  * On `slice`, extracts a window (2s pre / 4s post) around the next
    configured shot marker and POSTs the mp4 to `/api/shot`.

The server creates sessions via the browser; the fake phone no longer
carries source-video metadata. Usage:

    uv run python scripts/fake_phone.py \\
        --source data/raw/video_2026-04-18_11-05-38.mp4 \\
        --shots 580.865,708.513,828.509,939.183
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import websockets

PRE_PAD_S = 2.0
POST_PAD_S = 4.0
DEFAULT_CALIB_OFFSET_S = 5.0


def slice_mp4(source: Path, marker_s: float) -> bytes:
    start = max(0.0, marker_s - PRE_PAD_S)
    duration = PRE_PAD_S + POST_PAD_S
    tmp = Path(tempfile.mktemp(suffix=".mp4"))
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{start:.3f}",
                "-t", f"{duration:.3f}",
                "-i", str(source),
                "-c", "copy",
                str(tmp),
            ],
            check=True, capture_output=True,
        )
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def extract_frame_jpeg(source: Path, at_s: float) -> bytes:
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{at_s:.3f}",
                "-i", str(source),
                "-frames:v", "1",
                "-q:v", "3",
                str(tmp),
            ],
            check=True, capture_output=True,
        )
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def http_post_bytes(url: str, data: bytes, content_type: str) -> dict:
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": content_type, "Content-Length": str(len(data))},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def run(ws_url: str, source: Path, shot_times: list[float], calib_at_s: float) -> None:
    parsed = urlparse(ws_url)
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    base = f"{http_scheme}://{parsed.netloc}"
    shot_url = f"{base}/api/shot"
    calib_url = f"{base}/api/calibration-frame"

    print(f"connecting to {ws_url}")
    async with websockets.connect(ws_url) as ws:
        idx = 0
        await ws.send(json.dumps({"type": "hint_source", "source_video": source.name}))
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "paired":
                print(f"paired, session={msg.get('session_id')}, {len(shot_times)} shots queued")
            elif kind == "rejected":
                print(f"rejected: {msg.get('reason')}")
                return
            elif kind == "capture_frame":
                at_s = float(msg["at_s"]) if "at_s" in msg else calib_at_s
                print(f"[calibration frame] extracting @ {at_s:.2f}s...")
                data = await asyncio.to_thread(extract_frame_jpeg, source, at_s)
                print(f"  uploading {len(data)} bytes to {calib_url}")
                result = await asyncio.to_thread(http_post_bytes, calib_url, data, "image/jpeg")
                print(f"  server: {result}")
            elif kind == "slice":
                if idx >= len(shot_times):
                    print("no more shots to serve")
                    continue
                t = shot_times[idx]
                idx += 1
                print(f"[slice request] shot {idx}: marker={t:.3f}s, extracting...")
                data = await asyncio.to_thread(slice_mp4, source, t)
                print(f"  uploading {len(data)} bytes to {shot_url}")
                result = await asyncio.to_thread(http_post_bytes, shot_url, data, "video/mp4")
                print(f"  server: {result}")


def parse_markers(s: str) -> list[float]:
    def parse_one(piece: str) -> float:
        piece = piece.strip()
        if ":" in piece:
            parts = piece.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(piece)
    return [parse_one(p) for p in s.split(",") if p.strip()]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="ws://127.0.0.1:8000/ws/phone")
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--shots", required=True,
                   help="comma-separated shot markers, e.g. 9:42,11:45,13:45")
    p.add_argument("--calib-at", type=float, default=DEFAULT_CALIB_OFFSET_S,
                   help="source timestamp (seconds) to sample for calibration frame")
    args = p.parse_args()

    shot_times = parse_markers(args.shots)
    if not shot_times:
        raise SystemExit("no shot markers provided")
    asyncio.run(run(args.server, args.source, shot_times, args.calib_at))


if __name__ == "__main__":
    main()
