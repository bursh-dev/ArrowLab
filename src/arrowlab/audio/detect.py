from __future__ import annotations

import argparse
import subprocess
import wave
from pathlib import Path

import numpy as np

AUDIO_DIR = Path("data/processed/audio")
TEMPLATE_WIN_S = 0.25


def extract_audio(video_path: Path, sr: int = 16000) -> tuple[np.ndarray, int]:
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", str(sr),
            "-f", "s16le", "-",
        ],
        capture_output=True, check=True,
    )
    audio = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, sr


def compute_envelope(audio: np.ndarray, sr: int, win_ms: int = 20) -> np.ndarray:
    win = max(1, int(sr * win_ms / 1000))
    squared = audio.astype(np.float32) ** 2
    kernel = np.ones(win, dtype=np.float32) / win
    rms = np.sqrt(np.convolve(squared, kernel, mode="same"))
    return rms


def find_peaks(
    envelope: np.ndarray,
    sr: int,
    min_separation_s: float = 3.0,
    threshold_percentile: float = 99.8,
    abs_floor: float = 0.01,
) -> list[tuple[float, float]]:
    if envelope.size == 0:
        return []
    threshold = max(abs_floor, float(np.percentile(envelope, threshold_percentile)))
    n = len(envelope)
    min_sep = int(min_separation_s * sr)
    peaks: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if envelope[i] > threshold:
            peak_idx = i
            peak_val = float(envelope[i])
            j = i
            while j < n and envelope[j] > threshold:
                if envelope[j] > peak_val:
                    peak_val = float(envelope[j])
                    peak_idx = j
                j += 1
            peaks.append((peak_idx / sr, peak_val))
            i = peak_idx + min_sep
        else:
            i += 1
    return peaks


def save_wav(audio: np.ndarray, sr: int, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(output), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def save_clip(
    audio: np.ndarray,
    sr: int,
    center_s: float,
    output: Path,
    pre_s: float = 0.4,
    post_s: float = 0.8,
) -> None:
    start = int(max(0, (center_s - pre_s) * sr))
    end = int(min(len(audio), (center_s + post_s) * sr))
    save_wav(audio[start:end], sr, output)


def format_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m}:{s:06.3f}"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return audio, sr


def _normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x)
    if n < 1e-9:
        return x
    return x / n


def _fingerprint(seg: np.ndarray) -> np.ndarray:
    win = np.hanning(len(seg)).astype(np.float32)
    mag = np.abs(np.fft.rfft(seg * win))
    return _normalize(mag.astype(np.float32))


def _extract_window(audio: np.ndarray, center_sample: int, win_n: int) -> np.ndarray:
    half = win_n // 2
    start = max(0, center_sample - half)
    end = min(len(audio), start + win_n)
    start = max(0, end - win_n)
    seg = audio[start:end]
    if len(seg) < win_n:
        seg = np.pad(seg, (0, win_n - len(seg)))
    return seg.astype(np.float32)


def build_template(clip_paths: list[Path], sr_target: int = 16000, win_s: float = TEMPLATE_WIN_S) -> np.ndarray:
    win_n = int(sr_target * win_s)
    fps: list[np.ndarray] = []
    for p in clip_paths:
        audio, sr = read_wav(p)
        if sr != sr_target:
            raise RuntimeError(f"{p} sr={sr}, expected {sr_target}")
        env = compute_envelope(audio, sr, win_ms=10)
        if env.size == 0:
            continue
        peak = int(np.argmax(env))
        seg = _extract_window(audio, peak, win_n)
        fps.append(_fingerprint(seg))
    if not fps:
        raise RuntimeError("no templates built")
    tmpl = np.mean(np.stack(fps), axis=0)
    return _normalize(tmpl)


def peak_similarity(audio: np.ndarray, sr: int, peak_t: float, template: np.ndarray) -> float:
    win_n = (len(template) - 1) * 2
    center = int(round(peak_t * sr))
    seg = _extract_window(audio, center, win_n)
    fp = _fingerprint(seg)
    return float(np.dot(fp, template))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video", type=Path)
    p.add_argument("--min-sep", type=float, default=3.0)
    p.add_argument("--percentile", type=float, default=99.8)
    p.add_argument("--abs-floor", type=float, default=0.01)
    p.add_argument("--save-clips", action="store_true")
    p.add_argument("--clip-pre", type=float, default=0.4)
    p.add_argument("--clip-post", type=float, default=0.8)
    p.add_argument("--template-dir", type=Path, default=None,
                   help="directory of known-good shot wavs; filters peaks by similarity")
    p.add_argument("--similarity-threshold", type=float, default=0.6)
    args = p.parse_args()

    print(f"extracting audio from {args.video.name}...")
    audio, sr = extract_audio(args.video)
    print(f"got {len(audio)} samples at {sr} Hz ({len(audio)/sr:.2f}s)")

    envelope = compute_envelope(audio, sr)
    peaks = find_peaks(
        envelope, sr,
        min_separation_s=args.min_sep,
        threshold_percentile=args.percentile,
        abs_floor=args.abs_floor,
    )
    print(f"detected {len(peaks)} raw peaks (percentile={args.percentile}, min_sep={args.min_sep}s)")

    template = None
    if args.template_dir is not None:
        clips = sorted(args.template_dir.glob("*.wav"))
        if not clips:
            print(f"warning: no wavs in {args.template_dir}; skipping template filter")
        else:
            template = build_template(clips, sr_target=sr)
            print(f"built template from {len(clips)} calibration clips")

    for i, (t, v) in enumerate(peaks):
        sim_str = ""
        if template is not None:
            sim = peak_similarity(audio, sr, t, template)
            sim_str = f"  sim={sim:+.3f}"
            if sim < args.similarity_threshold:
                sim_str += "  [REJECT]"
        print(f"  peak {i+1:>2}: t={format_time(t)} ({t:.3f}s)  env={v:.4f}{sim_str}")

    if template is not None:
        peaks = [(t, v) for (t, v) in peaks
                 if peak_similarity(audio, sr, t, template) >= args.similarity_threshold]
        print(f"kept {len(peaks)} peaks after template filter (threshold={args.similarity_threshold})")

    if args.save_clips and peaks:
        out_dir = AUDIO_DIR / args.video.stem
        for i, (t, _) in enumerate(peaks):
            clip = out_dir / f"peak{i+1:02d}_t{int(round(t*1000))}ms.wav"
            save_clip(audio, sr, t, clip, pre_s=args.clip_pre, post_s=args.clip_post)
        print(f"saved {len(peaks)} clips to {out_dir}")


if __name__ == "__main__":
    main()
