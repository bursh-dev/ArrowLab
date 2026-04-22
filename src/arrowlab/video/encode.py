from __future__ import annotations

import subprocess
from pathlib import Path


def to_h264_faststart(path: Path) -> None:
    """Re-encode an mp4 in place to H.264 yuv420p + faststart for browser playback."""
    tmp = path.with_suffix(path.suffix + ".h264.tmp.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(path),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "ultrafast", "-crf", "20",
            "-movflags", "+faststart",
            "-an",
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(path)
