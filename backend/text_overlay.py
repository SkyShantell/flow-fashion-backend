from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from backend.models import Batch, ProductJob, QueueTask
from backend.services import useapi
import backend.tasks as tasks


log = logging.getLogger("flow-text-overlay")
_INSTALLED = False
_ORIGINAL_ARCHIVE_MEDIA: Callable[[Session, QueueTask], None] | None = None
_FONT_FILES = (
    "/usr/local/share/fonts/TikTokSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font_file() -> str:
    for path in _FONT_FILES:
        if Path(path).exists():
            return path
    return _FONT_FILES[-1]


def _fashion_caption(job: ProductJob) -> str:
    """Build a short TikTok-style fashion callout from the product metadata."""
    raw = str(job.product_name or "").strip().lower()
    raw = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", raw)
    raw = re.sub(r"[_|/]+", " ", raw)
    raw = re.sub(r"[^a-z0-9' -]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    colors = ["faded black", "black", "white", "cream", "navy", "blue", "pink", "brown", "grey", "gray", "green", "red"]
    details = [
        "low rise", "high rise", "mid rise", "baggy", "wide leg", "straight leg",
        "drop shoulder", "striped", "cropped", "oversized", "sleeveless", "cable knit",
    ]
    found_color = next((x for x in colors if x in raw), "")
    found_details = [x for x in details if x in raw][:2]
    descriptor = " ".join(([found_color] if found_color else []) + found_details).strip()

    if "jean" in raw:
        return f"the perfect {descriptor + ' ' if descriptor else ''}jeans".strip()
    if "sweater" in raw:
        return f"the perfect {descriptor + ' ' if descriptor else ''}sweater".strip()
    if "polo" in raw and ("knit" in raw or "sweater" in raw):
        return "polo knitwear >>>"
    if "knit" in raw:
        prefix = "sleeveless " if "sleeveless" in raw else ""
        return f"{prefix}knitwear >>>"
    if "hoodie" in raw and any(x in raw for x in ("set", "pant", "jogger", "sweat")):
        return "the perfect cozy set"
    if "set" in raw:
        return "the perfect set for fall"
    if "hoodie" in raw:
        return "hoodie season >>>"
    if "dress" in raw:
        return "the perfect everyday dress"
    if any(x in raw for x in ("pants", "trouser", "cargo")):
        return "the perfect everyday pants"
    if any(x in raw for x in ("shirt", "tee", "top", "blouse")):
        return "the perfect everyday top"
    if any(x in raw for x in ("shoe", "sneaker", "boot", "heel", "loafer")) or str(job.focus or "") == "shoes":
        return "the perfect pair >>>"
    if any(x in raw for x in ("bag", "purse", "handbag")) or str(job.focus or "") == "handbag":
        return "the perfect everyday bag"

    stop = {"women", "womens", "women's", "men", "mens", "men's", "fashion", "casual", "new", "style", "2026"}
    words = [w for w in raw.split() if w not in stop][:5]
    short_name = " ".join(words).strip()
    return f"the perfect {short_name}".strip() if short_name else "the perfect fit >>>"


def _wrap_caption(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip()).lower()
    if len(text) <= 34:
        return text
    lines = textwrap.wrap(text, width=30, break_long_words=False, break_on_hyphens=False)
    if len(lines) <= 2:
        return "\n".join(lines)
    second = lines[1]
    if len(second) > 28:
        second = second[:27].rstrip() + "…"
    return lines[0] + "\n" + second


def _burn_text(video_bytes: bytes, caption: str, placement_seed: str) -> bytes:
    if not video_bytes:
        raise RuntimeError("No final video bytes were available for the text overlay.")

    placements = [
        ("(w-text_w)/2", "h*0.56"),
        ("w*0.055", "h*0.34"),
        ("(w-text_w)/2", "h*0.66"),
    ]
    idx = sum(ord(ch) for ch in str(placement_seed or "")) % len(placements)
    x_expr, y_expr = placements[idx]

    with tempfile.TemporaryDirectory(prefix="flow_text_") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.mp4"
        output_path = root / "output.mp4"
        text_path = root / "caption.txt"
        input_path.write_bytes(video_bytes)
        text_path.write_text(_wrap_caption(caption), encoding="utf-8")

        drawtext = (
            f"drawtext=fontfile={_font_file()}:textfile={text_path}:expansion=none:"
            "fontcolor=white:fontsize=h*0.024:borderw=1:bordercolor=black@0.30:"
            "shadowcolor=black@0.45:shadowx=1:shadowy=1:line_spacing=6:fix_bounds=1:"
            f"x={x_expr}:y={y_expr}"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", drawtext,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not output_path.exists():
            error = (proc.stderr or proc.stdout or "FFmpeg text overlay failed.")[-3000:]
            raise RuntimeError(f"FFmpeg text overlay failed: {error}")
        return output_path.read_bytes()


def _run_archive_with_text_overlay(db: Session, task: QueueTask) -> None:
    if _ORIGINAL_ARCHIVE_MEDIA is None:
        raise RuntimeError("Archive handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    payload = dict(task.payload or {})

    # Text overlay is part of finalization, not an optional Drive-only step. Do not let
    # an existing/stale Drive file cause a newly generated video to skip FFmpeg.
    if (
        job
        and batch
        and job.stage in {"video_complete", "complete"}
        and not payload.get("fashion_text_overlay_applied")
        and (job.video_media_id or job.video_url or job.video_source_media_id or job.video_source_url)
    ):
        caption = _fashion_caption(job)
        log.info("Applying fashion text overlay · job=%s · caption=%s", job.id, caption)
        original_bytes = tasks._download_final_video_for_archive(job)
        if not original_bytes:
            raise RuntimeError("Could not download the final video before adding on-screen text.")
        final_bytes = _burn_text(original_bytes, caption, job.id)
        uploaded = useapi.upload_video_asset(final_bytes, tasks._batch_flow_account(batch))
        final_media_id = str(uploaded.get("media_id") or "").strip()
        if not final_media_id:
            raise RuntimeError("Text-overlaid video uploaded without a media ID.")

        job.video_media_id = final_media_id
        job.video_url = useapi.resolve_asset_url(final_media_id) or None
        job.video_source_email = str(uploaded.get("email") or job.video_source_email or "").strip() or None
        job.video_error = None

        # Any previous Drive video points at an older/raw render. Clear only the video
        # archive references so the normal archive handler stores the text-burned MP4.
        job.drive_video_id = None
        job.drive_video_url = None
        job.drive_video_download_url = None
        job.drive_error = None
        db.add(job)

        payload["fashion_text_overlay_applied"] = True
        payload["fashion_text_overlay_text"] = caption
        payload["fashion_text_overlay_media_id"] = final_media_id
        task.payload = payload
        db.add(task)
        db.flush()
        log.info("Fashion text overlay applied · job=%s · media=%s", job.id, final_media_id)

    return _ORIGINAL_ARCHIVE_MEDIA(db, task)


def install_text_overlay_handler() -> None:
    """Burn TikTok-style fashion text onto the final video before archive."""
    global _INSTALLED, _ORIGINAL_ARCHIVE_MEDIA
    if _INSTALLED:
        return
    original = tasks.HANDLERS.get("archive_media")
    if original is None:
        raise RuntimeError("Existing archive handler could not be located.")
    _ORIGINAL_ARCHIVE_MEDIA = original
    tasks.HANDLERS["archive_media"] = _run_archive_with_text_overlay
    _INSTALLED = True
