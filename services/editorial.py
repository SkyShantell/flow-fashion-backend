from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


SHOT_TRIMS = {"A": 2.8, "B": 3.5, "C": 3.7}


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def stitch_editorial_clips(clips: dict[str, bytes]) -> bytes:
    """Hard-cut A/B/C into one 10-second 1080x1920 silent MP4.

    Inputs are expected to be the already-upscaled 1080p clip bytes. The filter still
    normalizes geometry/fps so a provider-side metadata variation cannot break concat.
    """
    missing = [shot for shot in ("A", "B", "C") if not clips.get(shot)]
    if missing:
        raise RuntimeError(f"Missing editorial clip(s): {', '.join(missing)}")
    if not ffmpeg_available():
        raise RuntimeError(
            "FFmpeg is not installed on the Railway worker. Add ffmpeg to the worker image/packages and redeploy."
        )

    with tempfile.TemporaryDirectory(prefix="flow-editorial-") as tmp:
        tmp_path = Path(tmp)
        inputs: list[str] = []
        for shot in ("A", "B", "C"):
            path = tmp_path / f"{shot}.mp4"
            path.write_bytes(clips[shot])
            inputs.extend(["-i", str(path)])

        output = tmp_path / "editorial-final.mp4"
        chains = []
        for index, shot in enumerate(("A", "B", "C")):
            dur = SHOT_TRIMS[shot]
            chains.append(
                f"[{index}:v]trim=start=0:end={dur},setpts=PTS-STARTPTS,"
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p"
                f"[v{index}]"
            )
        chains.append("[v0][v1][v2]concat=n=3:v=1:a=0[outv]")
        filter_complex = ";".join(chains)

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if proc.returncode != 0 or not output.exists() or output.stat().st_size < 10_000:
            detail = (proc.stderr or proc.stdout or "Unknown FFmpeg error")[-2500:]
            raise RuntimeError(f"FFmpeg editorial stitch failed: {detail}")
        return output.read_bytes()
