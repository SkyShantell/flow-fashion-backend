from __future__ import annotations

import logging
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from backend import hook_index_from_motion_style
from backend.models import Batch, ProductJob, QueueTask
from backend.services import useapi
import backend.tasks as tasks


log = logging.getLogger("flow-text-overlay")
_INSTALLED = False
_ORIGINAL_ARCHIVE_MEDIA: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_ENQUEUE_TASK: Callable[..., QueueTask] | None = None
_ORIGINAL_SUBMIT_VIDEO_HANDLER: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_VIDEO_PROMPT: Callable[..., str] | None = None
_FONT_FILES = (
    "/usr/local/share/fonts/TikTokSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font_file() -> str:
    for path in _FONT_FILES:
        if Path(path).exists():
            return path
    return _FONT_FILES[-1]


def _normalized_product_name(job: ProductJob) -> str:
    raw = str(job.product_name or "").strip().lower()
    raw = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", raw)
    raw = re.sub(r"[_|/]+", " ", raw)
    raw = re.sub(r"[^a-z0-9' -]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _fashion_hook_profile(job: ProductJob) -> tuple[str, str]:
    """Return a simple product category plus the strongest default hook."""
    raw = _normalized_product_name(job)
    focus = str(job.focus or "").strip().lower()

    if "jean" in raw:
        return "pants", "the perfect jeans"
    if "polo" in raw and ("knit" in raw or "sweater" in raw):
        return "top", "polo knitwear >>>"
    if "knit" in raw:
        prefix = "sleeveless " if "sleeveless" in raw else ""
        return "top", f"{prefix}knitwear >>>"
    if "sweater" in raw:
        return "top", "the perfect sweater"
    if "hoodie" in raw and any(x in raw for x in ("set", "pant", "jogger", "sweat")):
        return "set", "the perfect cozy set"
    if "set" in raw:
        return "set", "the perfect set for fall"
    if "hoodie" in raw:
        return "top", "hoodie season >>>"
    if "dress" in raw:
        return "dress", "the perfect everyday dress"
    if "skirt" in raw:
        return "skirt", "the perfect everyday skirt"
    if "shorts" in raw or "short" in raw.split():
        return "shorts", "the perfect everyday shorts"
    if any(x in raw for x in ("pants", "trouser", "cargo")) or focus == "pants":
        return "pants", "the perfect everyday pants"
    if any(x in raw for x in ("jacket", "coat", "bomber", "cardigan")):
        return "outerwear", "the perfect layering piece"
    if any(x in raw for x in ("shirt", "tee", "top", "blouse")) or focus in {"shirt", "hoodie"}:
        return "top", "the perfect everyday top"
    if any(x in raw for x in ("shoe", "sneaker", "boot", "heel", "loafer")) or focus == "shoes":
        return "shoes", "the perfect pair >>>"
    if any(x in raw for x in ("bag", "purse", "handbag")) or focus == "handbag":
        return "bag", "the perfect everyday bag"
    return "outfit", "the perfect fit >>>"


def _fashion_hook_options(job: ProductJob) -> list[str]:
    """Five concise hook choices mirrored by the product-photo page control."""
    category, primary = _fashion_hook_profile(job)
    options_by_category = {
        "pants": [
            primary,
            "these fit way too good >>>",
            "found my new favorite pants",
            "the fit on these >>>",
            "need these in every color",
        ],
        "top": [
            primary,
            "this top is too good >>>",
            "found my new favorite top",
            "the fit on this >>>",
            "need this in every color",
        ],
        "set": [
            primary,
            "this set is too good >>>",
            "the easiest outfit ever",
            "found my new favorite set",
            "need this in every color",
        ],
        "dress": [
            primary,
            "this dress is too good >>>",
            "found my new favorite dress",
            "the fit on this >>>",
            "need this in every color",
        ],
        "skirt": [
            primary,
            "this skirt fits so good >>>",
            "found my new favorite skirt",
            "the shape on this >>>",
            "need this in every color",
        ],
        "shorts": [
            primary,
            "these shorts fit so good >>>",
            "found my new favorite shorts",
            "the fit on these >>>",
            "need these in every color",
        ],
        "outerwear": [
            primary,
            "this layer pulls it together",
            "found my new favorite jacket",
            "the fit on this >>>",
            "wearing this on repeat",
        ],
        "shoes": [
            primary,
            "these look even better on >>>",
            "found my new favorite pair",
            "the shape on these >>>",
            "need these in every color",
        ],
        "bag": [
            primary,
            "this bag goes with everything",
            "found my new everyday bag",
            "the details on this >>>",
            "need this in every color",
        ],
        "outfit": [
            primary,
            "this fit is too good >>>",
            "found my new favorite outfit",
            "the fit on this >>>",
            "need this in every color",
        ],
    }
    values = options_by_category.get(category, options_by_category["outfit"])
    out: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "").strip()).lower()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    fillers = ["this one is too good >>>", "adding this to the rotation", "the details on this >>>"]
    for value in fillers:
        if len(out) >= 5:
            break
        if value not in out:
            out.append(value)
    return out[:5]


def _fashion_caption(job: ProductJob) -> str:
    options = _fashion_hook_options(job)
    index = hook_index_from_motion_style(job.motion_style_override)
    return options[max(0, min(4, index - 1))] if options else "the perfect fit >>>"


def _sanitize_male_video_prompt(prompt: str) -> str:
    """Remove every male hands-on-hips instruction from generated or edited prompts."""
    text = str(prompt or "")
    replacements = (
        (r"free hand on hip and a small confident double nod", "free hand relaxed naturally at the side or briefly in a pocket, with a small confident double nod"),
        (r"lower the hand toward the hip", "lower the hand naturally to the side"),
        (r"\bfree hand on (?:the )?hip\b", "free hand relaxed naturally at the side"),
        (r"\bhand on (?:the )?hip\b", "hand relaxed naturally at the side"),
        (r"\bhands on hips\b", "hands relaxed naturally away from the hips"),
        (r"\bhands-on-hips\b", "relaxed natural stance"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    guard = (
        "MALE POSE RULE: never place either hand on the hips and never use a hands-on-hips pose. "
        "Keep the free hand relaxed at the side, briefly in a pocket, or naturally interacting with the garment instead."
    )
    if guard.lower() not in text.lower():
        text = f"{text.rstrip()} {guard}"
    return text.strip()


def _guarded_video_prompt(job, *, creator_profile: str = "Male", video_style: str = "Academy — Boss / Calm") -> str:
    if _ORIGINAL_VIDEO_PROMPT is None:
        raise RuntimeError("Original video prompt builder is unavailable.")
    prompt = _ORIGINAL_VIDEO_PROMPT(job, creator_profile=creator_profile, video_style=video_style)
    if str(creator_profile or "Male").lower().startswith("m"):
        prompt = _sanitize_male_video_prompt(prompt)
    return prompt


def _run_submit_with_prompt_controls(db: Session, task: QueueTask) -> None:
    """Keep the video prompt about motion only while enforcing the male pose rule."""
    if _ORIGINAL_SUBMIT_VIDEO_HANDLER is None:
        raise RuntimeError("Video submit handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    payload = dict(task.payload or {})
    override = str(payload.get("prompt_override") or "").strip()

    if override and batch and str(batch.creator_profile or "Male").lower().startswith("m"):
        payload["prompt_override"] = _sanitize_male_video_prompt(override)
        task.payload = payload
        db.add(task)
        db.flush()

    return _ORIGINAL_SUBMIT_VIDEO_HANDLER(db, task)


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
        ("(w-text_w)/2", "h*0.50"),
        ("w*0.055", "h*0.46"),
        ("(w-text_w)/2", "h*0.54"),
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
            "fontcolor=white:fontsize=h*0.029:borderw=1:bordercolor=black@0.30:"
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


def _enqueue_with_text_finalizing(db: Session, task_type: str, **kwargs) -> QueueTask:
    """Hide the raw provider video as soon as final text rendering is queued."""
    if _ORIGINAL_ENQUEUE_TASK is None:
        raise RuntimeError("Original enqueue_task is unavailable.")

    if task_type == "archive_media":
        job_id = str(kwargs.get("job_id") or "").strip()
        if job_id:
            job = db.get(ProductJob, job_id)
            if job and job.stage == "video_complete":
                if job.video_url and not job.video_source_url:
                    job.video_source_url = job.video_url
                job.video_url = None
                job.stage = "finalizing_text"
                db.add(job)
                db.flush()
                log.info("Finalizing text overlay · job=%s", job.id)

    return _ORIGINAL_ENQUEUE_TASK(db, task_type, **kwargs)


def _run_archive_with_text_overlay(db: Session, task: QueueTask) -> None:
    if _ORIGINAL_ARCHIVE_MEDIA is None:
        raise RuntimeError("Archive handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    payload = dict(task.payload or {})

    if (
        job
        and batch
        and job.stage in {"finalizing_text", "video_complete", "complete"}
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
        job.stage = "video_complete"

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
    """Install final text rendering and male pose safeguards."""
    global _INSTALLED, _ORIGINAL_ARCHIVE_MEDIA, _ORIGINAL_ENQUEUE_TASK
    global _ORIGINAL_SUBMIT_VIDEO_HANDLER, _ORIGINAL_VIDEO_PROMPT
    if _INSTALLED:
        return

    archive_handler = tasks.HANDLERS.get("archive_media")
    submit_handler = tasks.HANDLERS.get("submit_video")
    if archive_handler is None:
        raise RuntimeError("Existing archive handler could not be located.")
    if submit_handler is None:
        raise RuntimeError("Existing video submit handler could not be located.")

    _ORIGINAL_ARCHIVE_MEDIA = archive_handler
    _ORIGINAL_SUBMIT_VIDEO_HANDLER = submit_handler
    _ORIGINAL_ENQUEUE_TASK = tasks.enqueue_task
    _ORIGINAL_VIDEO_PROMPT = tasks.video_prompt

    tasks.video_prompt = _guarded_video_prompt
    video_provider_module = sys.modules.get("backend.video_provider")
    if video_provider_module is not None and hasattr(video_provider_module, "video_prompt"):
        video_provider_module.video_prompt = _guarded_video_prompt

    tasks.enqueue_task = _enqueue_with_text_finalizing
    tasks.HANDLERS["submit_video"] = _run_submit_with_prompt_controls
    tasks.HANDLERS["archive_media"] = _run_archive_with_text_overlay
    _INSTALLED = True
