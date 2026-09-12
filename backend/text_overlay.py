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

from backend.models import Batch, ProductJob, QueueTask
from backend.services import useapi
import backend.tasks as tasks


log = logging.getLogger("flow-text-overlay")
_INSTALLED = False
_ORIGINAL_ARCHIVE_MEDIA: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_ENQUEUE_TASK: Callable[..., QueueTask] | None = None
_ORIGINAL_SUBMIT_VIDEO_HANDLER: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_VIDEO_PROMPT: Callable[..., str] | None = None
_ORIGINAL_API_DEFAULT_VIDEO_PROMPT: Callable[..., str] | None = None
_ORIGINAL_API_LAST_VIDEO_PROMPT: Callable[..., tuple[str, str]] | None = None
_HOOK_PREFIX = "ON-SCREEN HOOK (EDIT THIS LINE):"
_HOOK_RE = re.compile(r"^\s*ON-SCREEN\s+HOOK(?:\s*\(EDIT THIS LINE\))?\s*:\s*(.*?)\s*$", re.IGNORECASE)
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
    """Build the default TikTok-style fashion callout from product metadata."""
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


def _clean_hook(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:90]


def _split_editor_prompt(value: str) -> tuple[str, str]:
    """Separate the editable post-production hook line from the generation prompt."""
    hook = ""
    prompt_lines: list[str] = []
    for line in str(value or "").splitlines():
        match = _HOOK_RE.match(line)
        if match:
            hook = _clean_hook(match.group(1))
            continue
        if line.strip().upper() == "VIDEO GENERATION PROMPT:":
            continue
        prompt_lines.append(line)
    return "\n".join(prompt_lines).strip(), hook


def _editor_prompt(prompt: str, hook: str) -> str:
    generation_prompt, embedded_hook = _split_editor_prompt(prompt)
    resolved_hook = _clean_hook(embedded_hook or hook) or "the perfect fit >>>"
    return f"{_HOOK_PREFIX} {resolved_hook}\n\nVIDEO GENERATION PROMPT:\n{generation_prompt}"


def _sanitize_male_video_prompt(prompt: str) -> str:
    """Remove male hands-on-hips poses from both saved and newly generated prompts."""
    text = str(prompt or "")
    text = re.sub(
        r"free hand on hip and a small confident double nod",
        "free hand relaxed naturally at the side or briefly in a pocket, with a small confident double nod",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"lower the hand toward the hip",
        "lower the hand naturally to the side",
        text,
        flags=re.IGNORECASE,
    )
    guard = (
        "MALE POSE RULE: never place the free hand on the hip and never use a hands-on-hips pose. "
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


def _latest_submit_video_task(db: Session, job: ProductJob) -> QueueTask | None:
    return (
        db.query(QueueTask)
        .filter(QueueTask.job_id == job.id, QueueTask.task_type == "submit_video")
        .order_by(QueueTask.created_at.desc())
        .first()
    )


def _caption_for_job(db: Session, job: ProductJob) -> str:
    submit = _latest_submit_video_task(db, job)
    payload = dict(submit.payload or {}) if submit else {}
    custom = _clean_hook(str(payload.get("onscreen_hook") or ""))
    return custom or _fashion_caption(job)


def _run_submit_with_prompt_controls(db: Session, task: QueueTask) -> None:
    """Strip editor-only hook metadata before Flow/Kling and enforce male pose rules."""
    if _ORIGINAL_SUBMIT_VIDEO_HANDLER is None:
        raise RuntimeError("Video submit handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    payload = dict(task.payload or {})
    override = str(payload.get("prompt_override") or "").strip()

    if override:
        generation_prompt, hook = _split_editor_prompt(override)
        if hook:
            payload["onscreen_hook"] = hook
        if batch and str(batch.creator_profile or "Male").lower().startswith("m"):
            generation_prompt = _sanitize_male_video_prompt(generation_prompt)
        payload["prompt_override"] = generation_prompt
        task.payload = payload
        db.add(task)
        db.flush()

    return _ORIGINAL_SUBMIT_VIDEO_HANDLER(db, task)


def _patch_api_prompt_editor() -> None:
    """Reuse the existing Video prompt modal as the hook editor without a dashboard migration."""
    global _ORIGINAL_API_DEFAULT_VIDEO_PROMPT, _ORIGINAL_API_LAST_VIDEO_PROMPT
    api_module = sys.modules.get("backend.api")
    if api_module is None:
        return

    default_builder = getattr(api_module, "_default_video_prompt", None)
    last_builder = getattr(api_module, "_last_video_prompt", None)
    if not callable(default_builder) or not callable(last_builder):
        return
    if _ORIGINAL_API_DEFAULT_VIDEO_PROMPT is not None:
        return

    _ORIGINAL_API_DEFAULT_VIDEO_PROMPT = default_builder
    _ORIGINAL_API_LAST_VIDEO_PROMPT = last_builder

    def default_with_hook(job: ProductJob, db: Session) -> str:
        if _ORIGINAL_API_DEFAULT_VIDEO_PROMPT is None:
            raise RuntimeError("Default video prompt builder is unavailable.")
        prompt = _ORIGINAL_API_DEFAULT_VIDEO_PROMPT(job, db)
        batch = db.get(Batch, job.batch_id)
        if batch and str(batch.creator_profile or "Male").lower().startswith("m"):
            prompt = _sanitize_male_video_prompt(prompt)
        return _editor_prompt(prompt, _fashion_caption(job))

    def last_with_hook(job: ProductJob, db: Session) -> tuple[str, str]:
        if _ORIGINAL_API_LAST_VIDEO_PROMPT is None:
            raise RuntimeError("Last video prompt builder is unavailable.")
        prompt, source = _ORIGINAL_API_LAST_VIDEO_PROMPT(job, db)
        generation_prompt, embedded_hook = _split_editor_prompt(prompt)
        submit = _latest_submit_video_task(db, job)
        payload = dict(submit.payload or {}) if submit else {}
        hook = _clean_hook(str(payload.get("onscreen_hook") or "")) or embedded_hook or _fashion_caption(job)
        batch = db.get(Batch, job.batch_id)
        if batch and str(batch.creator_profile or "Male").lower().startswith("m"):
            generation_prompt = _sanitize_male_video_prompt(generation_prompt)
        return _editor_prompt(generation_prompt, hook), source

    api_module._default_video_prompt = default_with_hook
    api_module._last_video_prompt = last_with_hook


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

    # Keep every caption near the visual center of the frame, while retaining a little
    # horizontal/vertical variation so consecutive videos do not look templated.
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
                # Keep a recoverable raw source for FFmpeg, but do not expose it as the
                # final video while the text-burn step is still processing.
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

    # Text overlay is a required finalization step. The raw provider/upscale video is
    # intentionally hidden from the UI until this block completes.
    if (
        job
        and batch
        and job.stage in {"finalizing_text", "video_complete", "complete"}
        and not payload.get("fashion_text_overlay_applied")
        and (job.video_media_id or job.video_url or job.video_source_media_id or job.video_source_url)
    ):
        caption = _caption_for_job(db, job)
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
    """Install final text rendering, editable hooks, and male pose safeguards."""
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

    # Worker-side defaults: both Flow and Kling get the male no-hands-on-hips safeguard.
    tasks.video_prompt = _guarded_video_prompt
    video_provider_module = sys.modules.get("backend.video_provider")
    if video_provider_module is not None and hasattr(video_provider_module, "video_prompt"):
        video_provider_module.video_prompt = _guarded_video_prompt

    # API-side prompt editor: the existing modal now exposes the editable FFmpeg hook
    # as its first line, so no dashboard redeploy is required for this control.
    _patch_api_prompt_editor()

    tasks.enqueue_task = _enqueue_with_text_finalizing
    tasks.HANDLERS["submit_video"] = _run_submit_with_prompt_controls
    tasks.HANDLERS["archive_media"] = _run_archive_with_text_overlay
    _INSTALLED = True
