from __future__ import annotations

import re
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Batch, ProductJob, QueueTask
from backend.services import sociavault, useapi
import backend.tasks as tasks


_INSTALLED = False
_ORIGINAL_GENERATE_EDITORIAL_FRAME: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_SUBMIT_VIDEO: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_POLL_VIDEO: Callable[[Session, QueueTask], None] | None = None


def _is_shoe_batch(batch: Batch | None) -> bool:
    return bool(batch and (batch.mode or "fashion_tryon") == "shoe_showcase")


def _clean_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def shoe_o1_video_prompt(job: ProductJob, *, creator_profile: str = "Female", reference_count: int | None = None) -> str:
    """Kling O1 shoe prompt using uploaded image references, not the Frames workflow.

    @image_1 is always the approved Google Flow opener. Any remaining @image_N inputs
    are product-detail references only. This intentionally avoids frame_start so O1 can
    receive several shoe reference images in the same generation.
    """
    product = str(job.product_name or "shoe").strip()
    if reference_count is None:
        reference_count = 1 + min(6, len([x for x in list(job.selected_refs or []) if str(x).strip()]))
    reference_count = max(1, min(7, int(reference_count or 1)))
    extra_tags = [f"@image_{i}" for i in range(2, reference_count + 1)]
    extra_rule = ""
    if extra_tags:
        extra_rule = (
            "Use " + ", ".join(extra_tags) +
            " only as extra product references for the exact shoe identity, materials, construction and details. "
            "Do not use their people, poses, backgrounds or composition. "
        )

    hand = "woman's hand" if str(creator_profile or "Female").lower().startswith("f") else "man's hand"
    return _clean_prompt(f"""
        9:16 vertical, 10 seconds. Use the supplied approved Flow start image @image_1 as the exact first frame. The opening moment must perfectly match @image_1 before motion begins. {extra_rule}
        Preserve the exact {product} from @image_1 throughout the entire video: exact color, materials, silhouette, toe shape, sole and tread, heel, stitching, laces or closures, hardware, physical branding and proportions. Never redesign, recolor, morph, duplicate or invent product features.
        Environment: the same dark luxury car interior from @image_1 with black leather seating and subtle gloss-black, chrome or premium trim. Moody ambient lighting. Keep the shoe large and the visual priority. Premium editorial TikTok Shop look with natural phone-camera realism.
        SHOT 1 · 0:00–0:03: Start exactly on @image_1. Immediately after the opening moment, the {hand} naturally lifts, tilts and repositions the shoe with clear controlled energy. A subtle camera push or reframe is allowed.
        CUT · SHOT 2 · 0:03–0:07: New angle in the same luxury-car visual world. Show an active product showcase: hand-held rotation or an on-foot angle, whichever best suits the shoe. Clearly reveal the front-to-side profile and upper shape. Movement should feel intentional, stylish and physically realistic.
        CUT · SHOT 3 · 0:07–0:10: Close detail and hero finish. Reveal a useful detail that truly exists on the product, such as sole edge or tread, heel, stitching, tongue, lace area, zipper, lining or texture. Finish on a strong three-quarter hero angle.
        No face reveal. No upper body. If hand-held, show no person above the forearm. If on-foot, keep framing product-focused. No extra people. Silent. No generated text, captions, subtitles, watermarks or added logos. No color changes or made-up features.
    """)[:1700]


def _submit_o1(image_urls: list[str], prompt: str, *, email: str) -> dict:
    if not image_urls:
        raise RuntimeError("Kling O1 needs at least the approved Flow image.")
    if len(image_urls) > 7:
        image_urls = image_urls[:7]

    cfg = settings()
    body: dict[str, object] = {
        "email": email,
        "prompt": str(prompt or "")[:1700],
        "omni_version": "o1",
        "mode": "pro",
        "duration": "10",
        "aspect_ratio": "9:16",
        "count": 1,
    }
    for index, url in enumerate(image_urls, start=1):
        body[f"image_{index}"] = str(url)

    payload = useapi.request_json(
        "POST",
        f"{cfg.kling_base}/videos/omni",
        headers=useapi.flow_headers(cfg.useapi_token, True),
        json_body=body,
        timeout=180,
        retries=1,
    )
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_id = task.get("id") or payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError("Kling O1 submitted without returning a task ID.")
    return {
        "job_id": str(task_id),
        "status": str(task.get("status_name") or payload.get("status_name") or "submitted").lower(),
    }


def _upload_shoe_o1_references(job: ProductJob, batch: Batch) -> tuple[list[str], str]:
    account = useapi.resolve_kling_account_email(str(batch.kling_account_email or "").strip())

    # @image_1 = exact approved Flow image.
    approved_bytes, approved_mime = tasks._asset_bytes(job.image_media_id or "", job.image_url or "")
    approved_asset = useapi.upload_kling_asset(approved_bytes, approved_mime, account)
    urls = [str(approved_asset.get("url") or "").strip()]
    if not urls[0]:
        raise RuntimeError("Could not upload the approved Flow image to Kling O1.")

    # @image_2..@image_7 = original product refs selected on the product-photo page.
    seen: set[str] = set()
    for raw_url in list(job.selected_refs or []):
        source_url = str(raw_url or "").strip()
        if not source_url or source_url in seen or len(urls) >= 7:
            continue
        seen.add(source_url)
        data, mime = sociavault.fetch_remote_image(source_url)
        uploaded = useapi.upload_kling_asset(data, mime, account)
        url = str(uploaded.get("url") or "").strip()
        if url:
            urls.append(url)

    return urls, account


def _run_shoe_frame_a_only(db: Session, task: QueueTask) -> None:
    if _ORIGINAL_GENERATE_EDITORIAL_FRAME is None:
        raise RuntimeError("Original Shoe Showcase image handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if not job or not _is_shoe_batch(batch):
        return _ORIGINAL_GENERATE_EDITORIAL_FRAME(db, task)

    shot = str((task.payload or {}).get("shot") or "A").upper()
    if shot != "A":
        # The O1 workflow never needs Flow frames B/C. Treat stale queued B/C work as a no-op.
        return

    # Reuse the proven Flow Frame A prompt/generator exactly as it exists today.
    _ORIGINAL_GENERATE_EDITORIAL_FRAME(db, task)
    job = db.get(ProductJob, task.job_id)
    if not job:
        return

    # Original Frame A queues B next. Cancel it before this transaction becomes visible to
    # another worker, then retain only A as the single reviewable Flow image.
    queued = (
        db.query(QueueTask)
        .filter(
            QueueTask.job_id == job.id,
            QueueTask.task_type == "generate_editorial_frame",
            QueueTask.status == "queued",
        )
        .all()
    )
    for queued_task in queued:
        queued_shot = str((queued_task.payload or {}).get("shot") or "").upper()
        if queued_shot in {"B", "C"}:
            queued_task.status = "canceled"
            queued_task.error = "Skipped: Shoe Showcase now uses one approved Flow opener + Kling O1."
            db.add(queued_task)

    shots = [dict(x) for x in list(job.editorial_shots or []) if isinstance(x, dict)]
    opener = next((x for x in shots if str(x.get("shot") or "").upper() == "A"), None)
    job.editorial_shots = [opener] if opener else []
    if not job.image_media_id or not job.image_url:
        raise RuntimeError("Flow Frame A finished without an approved-image asset.")
    job.image_status = "completed"
    job.image_error = None
    job.approved = False
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.stage = "awaiting_approval"
    db.add(job)
    db.flush()


def _run_submit_shoe_o1(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if job.image_status != "completed" or not job.image_media_id:
        raise RuntimeError("A completed Flow opener is required before Kling O1 video generation.")
    if not job.approved:
        raise RuntimeError("Approve the Flow opener before generating the Kling O1 video.")

    job.stage = "submitting_video"
    job.video_status = "created"
    job.video_error = None
    job.video_attempts = int(job.video_attempts or 0) + 1
    job.video_provider_used = "kling"
    job.video_provider_account = None
    job.video_source_email = None
    job.video_source_media_id = None
    job.video_source_url = None
    job.video_source_resolution = None
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.upscale_status = "pending"
    job.upscale_job_id = None
    job.upscale_error = None
    db.add(job)
    db.flush()

    image_urls, account = _upload_shoe_o1_references(job, batch)
    prompt_override = str((task.payload or {}).get("prompt_override") or "").strip()
    prompt_text = prompt_override or shoe_o1_video_prompt(
        job,
        creator_profile=batch.creator_profile or "Female",
        reference_count=len(image_urls),
    )
    prompt_text = str(prompt_text)[:1700]
    task.payload = {
        **dict(task.payload or {}),
        "prompt_used": prompt_text,
        "video_provider": "kling",
        "kling_model": "kling-o1",
        "o1_reference_count": len(image_urls),
    }
    db.add(task)
    db.flush()

    result = _submit_o1(image_urls, prompt_text, email=account)
    job.video_job_id = result["job_id"]
    job.video_provider_used = "kling"
    job.video_provider_account = account
    job.video_source_email = account
    job.video_status = str(result.get("status") or "submitted").lower()
    job.stage = "video_processing"
    db.add(job)
    db.flush()

    tasks.enqueue_task(
        db,
        "poll_video",
        job_id=job.id,
        batch_id=job.batch_id,
        priority=40,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds),
        max_attempts=80,
    )
    tasks.enqueue_task(
        db,
        "sync_sheet",
        job_id=job.id,
        batch_id=job.batch_id,
        priority=300,
        max_attempts=2,
        allow_duplicate=True,
    )


def _dispatch_submit_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch):
        return _run_submit_shoe_o1(db, task, job, batch)
    if _ORIGINAL_SUBMIT_VIDEO is None:
        raise RuntimeError("Existing video submit handler is unavailable.")
    return _ORIGINAL_SUBMIT_VIDEO(db, task)


def _run_poll_shoe_o1(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.video_job_id:
        raise RuntimeError("Missing Kling O1 task ID.")

    account = str(job.video_provider_account or batch.kling_account_email or "").strip()
    result = useapi.parse_kling_task(useapi.get_kling_task(job.video_job_id, account))
    status = str(result.get("status") or job.video_status or "processing").lower()
    job.video_status = status
    if result.get("thumbnail_url"):
        job.thumbnail_url = str(result["thumbnail_url"])
    if result.get("video_url"):
        job.video_source_url = str(result["video_url"])
    if result.get("error"):
        job.video_error = str(result["error"])

    if status == "completed":
        final_url = str(result.get("video_url") or job.video_source_url or "").strip()
        work_id = str(result.get("work_id") or "").strip()
        if work_id:
            try:
                clean_url = useapi.get_kling_clean_url(work_id, account)
                if clean_url:
                    final_url = clean_url
            except Exception:
                pass
        if not final_url:
            raise RuntimeError("Kling O1 completed but did not return a video URL.")

        job.video_source_url = final_url
        job.video_url = final_url
        job.video_source_resolution = settings().video_final_resolution
        job.video_resolution = settings().video_final_resolution
        job.video_provider_used = "kling"
        job.video_provider_account = account or job.video_provider_account
        job.video_source_email = account or job.video_source_email
        job.video_status = "completed"
        # O1 pro is the final generation path; do not send it through the Google Flow upscale path.
        job.upscale_status = "completed"
        job.upscale_error = None
        job.video_error = None
        job.stage = "video_complete"
        db.add(job)
        db.flush()
        # text_overlay wraps enqueue_task and turns this into the final FFmpeg hook render.
        tasks.enqueue_task(db, "archive_media", job_id=job.id, batch_id=job.batch_id, priority=80, max_attempts=2)
        tasks.enqueue_task(
            db,
            "sync_sheet",
            job_id=job.id,
            batch_id=job.batch_id,
            priority=300,
            max_attempts=2,
            allow_duplicate=True,
        )
        return

    if status == "failed":
        job.stage = "failed"
        db.add(job)
        db.flush()
        raise RuntimeError(job.video_error or "Kling O1 video generation failed.")

    job.stage = "video_processing"
    db.add(job)
    db.flush()
    tasks.enqueue_task(
        db,
        "poll_video",
        job_id=job.id,
        batch_id=job.batch_id,
        priority=40,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds),
        max_attempts=80,
        allow_duplicate=True,
    )


def _dispatch_poll_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch):
        return _run_poll_shoe_o1(db, task, job, batch)
    if _ORIGINAL_POLL_VIDEO is None:
        raise RuntimeError("Existing video poll handler is unavailable.")
    return _ORIGINAL_POLL_VIDEO(db, task)


def shoe_o1_images_ready(job: ProductJob) -> bool:
    if job.image_status == "completed" and job.image_media_id and job.image_url:
        return True
    for item in list(job.editorial_shots or []):
        if isinstance(item, dict) and str(item.get("shot") or "").upper() == "A":
            return str(item.get("image_status") or "") == "completed" and bool(item.get("image_media_id"))
    return False


def install_shoe_o1_handlers() -> None:
    """Replace only Shoe Showcase generation; all fashion handlers remain unchanged."""
    global _INSTALLED, _ORIGINAL_GENERATE_EDITORIAL_FRAME, _ORIGINAL_SUBMIT_VIDEO, _ORIGINAL_POLL_VIDEO
    if _INSTALLED:
        return

    frame_handler = tasks.HANDLERS.get("generate_editorial_frame")
    submit_handler = tasks.HANDLERS.get("submit_video")
    poll_handler = tasks.HANDLERS.get("poll_video")
    if frame_handler is None or submit_handler is None or poll_handler is None:
        raise RuntimeError("Existing Flow Fashion handlers could not be located.")

    _ORIGINAL_GENERATE_EDITORIAL_FRAME = frame_handler
    _ORIGINAL_SUBMIT_VIDEO = submit_handler
    _ORIGINAL_POLL_VIDEO = poll_handler
    tasks.HANDLERS["generate_editorial_frame"] = _run_shoe_frame_a_only
    tasks.HANDLERS["submit_video"] = _dispatch_submit_video
    tasks.HANDLERS["poll_video"] = _dispatch_poll_video
    _INSTALLED = True
