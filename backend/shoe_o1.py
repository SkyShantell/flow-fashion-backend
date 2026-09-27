from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Batch, ProductJob, QueueTask
from backend.services import enhancor, sociavault, useapi
from backend.services.shoe_pov_flow import FlowPovClient
from backend.shoe_pov_image_gate import OpenAIImageGateClient, image_guard_note
from backend.shoe_pov_prompts import build_image_prompt, build_video_prompt, choose_beats
from backend.shoe_pov_video_gate import OpenAIVideoGateClient, VideoGateError, extract_video_sheets
import backend.tasks as tasks


log = logging.getLogger("flow-worker")

_INSTALLED = False
_ORIGINAL_GENERATE_IMAGE: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_GENERATE_EDITORIAL_FRAME: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_SUBMIT_VIDEO: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_POLL_VIDEO: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_SUBMIT_UPSCALE: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_POLL_UPSCALE: Callable[[Session, QueueTask], None] | None = None


def _is_shoe_batch(batch: Batch | None) -> bool:
    return bool(batch and (batch.mode or "fashion_tryon") == "shoe_showcase")


def shoe_provider(batch: Batch | None) -> str:
    if not _is_shoe_batch(batch):
        return ""
    return "shoe_pov" if str(batch.video_provider or "").strip().lower() == "shoe_pov" else "enhancor"


def _is_pov_batch(batch: Batch | None) -> bool:
    return shoe_provider(batch) == "shoe_pov"


def _pov_format(batch: Batch) -> str:
    return "worn" if str(batch.shoe_pov_format or "").strip().lower() == "worn" else "held"


def _pov_account(job: ProductJob, batch: Batch) -> str:
    selected = tasks._batch_flow_account(batch)
    if selected:
        return selected
    for record in list(job.flow_product_refs or []):
        if isinstance(record, dict) and str(record.get("sourceEmail") or "").strip():
            return str(record["sourceEmail"]).strip()
    configured = str(settings().google_flow_email or "").strip()
    if configured:
        return configured
    accounts = useapi.list_flow_accounts()
    healthy = [row for row in accounts if str(row.get("health") or "").upper() == "OK"]
    choices = healthy or accounts
    if not choices:
        raise RuntimeError("Flow Shoes POV requires a connected Google Flow account.")
    return str(choices[0].get("email") or "").strip()


def _pov_product_images(job: ProductJob, limit: int) -> list[tuple[bytes, str]]:
    images: list[tuple[bytes, str]] = []
    for url in list(job.selected_refs or [])[:limit]:
        images.append(sociavault.fetch_remote_image(str(url)))
    if not images:
        raise RuntimeError("Flow Shoes POV requires selected product photos.")
    return images


def _pov_item_index(db: Session, job: ProductJob) -> int:
    return db.query(ProductJob.id).filter(
        ProductJob.batch_id == job.batch_id,
        ProductJob.created_at < job.created_at,
    ).count()


def _pov_prompt_inputs(db: Session, job: ProductJob, batch: Batch) -> tuple[str, str, int, str]:
    fmt = _pov_format(batch)
    index = _pov_item_index(db, job)
    seed = str(job.id)
    description = str(job.product_name or "shoe").strip()
    return fmt, seed, index, description


def _set_pov_image(job: ProductJob, result: dict, *, prompt: str, warning: str = "") -> None:
    media_id = str(result.get("media_id") or "").strip()
    image_url = str(result.get("url") or "").strip() or useapi.resolve_asset_url(media_id)
    if not media_id or not image_url:
        raise RuntimeError("Nano Banana Pro completed without a usable image.")
    job.image_job_id = str(result.get("job_id") or "") or None
    job.image_media_id = media_id
    job.image_url = image_url
    job.image_seed = str(result.get("seed") or "") or None
    job.image_status = "completed"
    job.image_error = warning or None
    job.approved = False
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.stage = "awaiting_approval"
    job.editorial_shots = [{
        "shot": "A",
        "role": "POV starting frame",
        "image_status": "completed",
        "image_media_id": media_id,
        "image_url": image_url,
        "image_error": warning,
        "prompt_used": prompt,
    }]


def _run_shoe_pov_image(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    job.stage = "generating_pov_image"
    job.image_status = "processing"
    job.image_error = None
    job.image_attempts = int(job.image_attempts or 0) + 1
    db.add(job)
    db.flush()

    refs = tasks._ensure_product_refs(db, job, batch)[:4]
    account = _pov_account(job, batch)
    fmt, seed, index, description = _pov_prompt_inputs(db, job, batch)
    beats = choose_beats(fmt, seed=seed, index=index, rotating_formats=False)
    prompt = build_image_prompt(
        gender=batch.creator_profile or "Female",
        skin_tone=batch.shoe_pov_skin_tone or "medium brown",
        product_title=description,
        product_description=description,
        format_name=fmt,
        hook=beats.hook,
        seed=seed,
        index=index,
    )
    revision = str((task.payload or {}).get("instruction") or "").strip()
    if revision:
        prompt = f"{prompt}\n\nUSER REVISION: {revision}"

    client = FlowPovClient()
    result = client.generate_image(prompt=prompt, product_media_ids=refs, email=account)
    warning = ""
    try:
        product_images = _pov_product_images(job, 4)
        rendered = tasks._asset_bytes(str(result.get("media_id") or ""), str(result.get("url") or ""))
        decision = OpenAIImageGateClient().inspect(
            product_images=product_images,
            rendered_image=rendered,
            seller_description=description,
            format_name=fmt,
        )
        if not decision.passed and decision.action == "rerender":
            guarded_prompt = f"{prompt}\n\n{image_guard_note(format_name=fmt, reasons=decision.reasons)}"
            result = client.generate_image(prompt=guarded_prompt, product_media_ids=refs, email=account)
            prompt = guarded_prompt
            rendered = tasks._asset_bytes(str(result.get("media_id") or ""), str(result.get("url") or ""))
            decision = OpenAIImageGateClient().inspect(
                product_images=product_images,
                rendered_image=rendered,
                seller_description=description,
                format_name=fmt,
            )
        if not decision.passed:
            warning = "Manual review required: " + "; ".join(decision.reasons)
    except Exception as exc:
        warning = f"Manual review required because the image check was unavailable: {exc}"

    _set_pov_image(job, result, prompt=prompt, warning=warning)
    job.image_source_email = account
    task.payload = {
        **dict(task.payload or {}),
        "prompt_used": prompt,
        "video_provider": "shoe_pov",
        "shoe_pov_format": fmt,
        "quality_warning": warning,
    }
    db.add(job)
    db.add(task)
    db.flush()
    log.info("Flow Shoes POV image ready · job=%s · format=%s · review=%s", job.id, fmt, bool(warning))
    tasks.enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def _clean_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def shoe_o1_video_prompt(job: ProductJob, *, creator_profile: str = "Female", reference_count: int | None = None) -> str:
    """Shoe showcase prompt using the approved opener and product references.

    @image_1 is always the approved Google Flow opener. Any remaining @image_N inputs
    are product-detail references only.
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
        9:16 vertical, 8 seconds. Use the supplied approved Flow start image @image_1 as the exact first frame. The opening moment must perfectly match @image_1 before motion begins. {extra_rule}
        Preserve the exact {product} from @image_1 throughout the entire video: exact color, materials, silhouette, toe shape, sole and tread, heel, stitching, laces or closures, hardware, physical branding and proportions. Never redesign, recolor, morph, duplicate or invent product features.
        Environment: the same dark luxury car interior from @image_1 with black leather seating and subtle gloss-black, chrome or premium trim. Moody ambient lighting. Keep the shoe large and the visual priority. Premium editorial TikTok Shop look with natural phone-camera realism.
        SHOT 1 · 0:00–0:02: Start exactly on @image_1. Immediately after the opening moment, the {hand} naturally lifts, tilts and repositions the shoe with clear controlled energy. A subtle camera push or reframe is allowed.
        CUT · SHOT 2 · 0:02–0:05: New angle in the same luxury-car visual world. Show an active product showcase: hand-held rotation or an on-foot angle, whichever best suits the shoe. Clearly reveal the front-to-side profile and upper shape. Movement should feel intentional, stylish and physically realistic.
        CUT · SHOT 3 · 0:05–0:08: Close detail and hero finish. Reveal a useful detail that truly exists on the product, such as sole edge or tread, heel, stitching, tongue, lace area, zipper, lining or texture. Finish on a strong three-quarter hero angle.
        No face reveal. No upper body. If hand-held, show no person above the forearm. If on-foot, keep framing product-focused. No extra people. Silent. No generated text, captions, subtitles, watermarks or added logos. No color changes or made-up features.
    """)[:1700]


def _shoe_reference_urls(job: ProductJob) -> list[str]:
    # Serve every reference as JPEG; Enhancor rejects WebP product URLs.
    if not job.image_media_id:
        raise RuntimeError("The approved Flow opener is missing.")
    urls = [enhancor.reference_url(job.id, 0)]
    for index, raw_url in enumerate(list(job.selected_refs or []), start=1):
        url = str(raw_url or "").strip()
        if not url:
            raise RuntimeError(f"Selected shoe reference {index} has no URL.")
        urls.append(enhancor.reference_url(job.id, index))
    return urls


def _run_shoe_frame_a_only(db: Session, task: QueueTask) -> None:
    if _ORIGINAL_GENERATE_EDITORIAL_FRAME is None:
        raise RuntimeError("Original Shoe Showcase image handler is unavailable.")

    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if not job or not _is_shoe_batch(batch):
        return _ORIGINAL_GENERATE_EDITORIAL_FRAME(db, task)
    if _is_pov_batch(batch):
        return _run_shoe_pov_image(db, task, job, batch)

    shot = str((task.payload or {}).get("shot") or "A").upper()
    if shot != "A":
        # Shoe Showcase never needs Flow frames B/C. Treat stale queued B/C work as a no-op.
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
            queued_task.error = "Skipped: Shoe Showcase uses one approved Flow opener + Seedance 2.0."
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


def _dispatch_generate_image(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch):
        task.payload = {**dict(task.payload or {}), "shot": "A"}
        return _run_shoe_frame_a_only(db, task)
    if _ORIGINAL_GENERATE_IMAGE is None:
        raise RuntimeError("Existing image generation handler is unavailable.")
    return _ORIGINAL_GENERATE_IMAGE(db, task)


def _run_submit_shoe_o1(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if job.image_status != "completed" or not job.image_media_id:
        raise RuntimeError("A completed Flow opener is required before Seedance video generation.")
    if not job.approved:
        raise RuntimeError("Approve the Flow opener before generating the Seedance video.")

    job.stage = "submitting_video"
    job.video_status = "created"
    job.video_error = None
    job.video_attempts = int(job.video_attempts or 0) + 1
    job.video_provider_used = "enhancor"
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

    cfg = settings()
    if not cfg.seedance_black_video_url:
        raise RuntimeError("SEEDANCE_BLACK_VIDEO_URL must point to the two-second black reference video.")
    image_urls = _shoe_reference_urls(job)
    prompt_override = str((task.payload or {}).get("prompt_override") or "").strip()
    prompt_text = prompt_override or shoe_o1_video_prompt(
        job,
        creator_profile=batch.creator_profile or "Female",
        reference_count=len(image_urls),
    )
    task.payload = {
        **dict(task.payload or {}),
        "prompt_used": prompt_text,
        "video_provider": "enhancor",
        "seedance_model": "seedance-2.0",
        "reference_count": len(image_urls),
    }
    db.add(task)
    db.flush()

    log.info("Submitting Seedance video · job=%s · images=%s", job.id, len(image_urls))
    result = enhancor.submit_seedance_video(
        prompt_text, image_urls,
        start_video=cfg.seedance_black_video_url,
        webhook_url=enhancor.webhook_url(job.id),
    )
    job.video_job_id = result["job_id"]
    log.info("Enhancor requestId: %s", job.video_job_id)
    job.video_provider_used = "enhancor"
    job.video_status = "processing"
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


def _queue_pov_upscale(db: Session, job: ProductJob, result: dict) -> None:
    media_id = str(result.get("media_id") or "").strip()
    video_url = str(result.get("url") or "").strip()
    if not media_id:
        raise RuntimeError("Omni Flash completed without a video media ID.")
    job.video_source_media_id = media_id
    job.video_source_url = video_url or useapi.resolve_asset_url(media_id)
    job.video_source_resolution = "720p"
    job.video_status = "completed"
    job.stage = "ready_for_upscale"
    db.add(job)
    db.flush()
    tasks.enqueue_task(db, "submit_upscale", job_id=job.id, batch_id=job.batch_id, priority=50, max_attempts=2, allow_duplicate=True)
    tasks.enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def _run_submit_shoe_pov(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if job.image_status != "completed" or not job.image_media_id:
        raise RuntimeError("A completed Nano Banana Pro POV frame is required before Flow video generation.")
    if not job.approved:
        raise RuntimeError("Approve the POV starting frame before generating the Flow video.")

    fmt, seed, index, description = _pov_prompt_inputs(db, job, batch)
    beats = choose_beats(fmt, seed=seed, index=index, rotating_formats=False)
    prompt_override = str((task.payload or {}).get("prompt_override") or "").strip()
    prompt_text = prompt_override or build_video_prompt(
        gender=batch.creator_profile or "Female",
        skin_tone=batch.shoe_pov_skin_tone or "medium brown",
        product_title=description,
        product_description=description,
        format_name=fmt,
        beats=beats,
        seed=seed,
        index=index,
        talking=False,
    )
    account = str(job.image_source_email or "").strip() or _pov_account(job, batch)

    job.stage = "submitting_video"
    job.video_status = "created"
    job.video_error = None
    job.video_attempts = int(job.video_attempts or 0) + 1
    job.video_provider_used = "shoe_pov"
    job.video_provider_account = account
    job.video_source_email = account
    job.video_source_media_id = None
    job.video_source_url = None
    job.video_source_resolution = None
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.upscale_status = "pending"
    job.upscale_job_id = None
    job.upscale_error = None
    task.payload = {
        **dict(task.payload or {}),
        "prompt_used": prompt_text,
        "video_provider": "shoe_pov",
        "shoe_pov_format": fmt,
        "model": "omni-flash",
        "duration": 8,
        "resolution": "720p",
    }
    db.add(job)
    db.add(task)
    db.flush()

    log.info("Submitting Flow Shoes POV video · job=%s · format=%s", job.id, fmt)
    result = FlowPovClient().submit_video(prompt=prompt_text, start_image_media_id=job.image_media_id, email=account)
    if result["status"] == "completed":
        return _queue_pov_upscale(db, job, result)
    job.video_job_id = str(result["job_id"])
    job.video_status = "processing"
    job.stage = "video_processing"
    db.add(job)
    db.flush()
    tasks.enqueue_task(
        db, "poll_video", job_id=job.id, batch_id=job.batch_id, priority=40,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds), max_attempts=80,
    )
    tasks.enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def _run_poll_shoe_pov(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.video_job_id:
        raise RuntimeError("Missing Flow Shoes POV video job ID.")
    result = FlowPovClient().get_job(job.video_job_id, kind="video")
    status = str(result.get("status") or "processing")
    job.video_status = status
    if result.get("thumbnail_url"):
        job.thumbnail_url = str(result["thumbnail_url"])
    if status == "completed":
        return _queue_pov_upscale(db, job, result)
    if status == "failed":
        job.video_error = str(result.get("error") or "Flow Shoes POV video generation failed.")
        job.stage = "failed"
        db.add(job)
        db.flush()
        return
    job.stage = "video_processing"
    db.add(job)
    db.flush()
    tasks.enqueue_task(
        db, "poll_video", job_id=job.id, batch_id=job.batch_id, priority=40,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds), max_attempts=80, allow_duplicate=True,
    )


def _dispatch_submit_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch):
        if _is_pov_batch(batch):
            return _run_submit_shoe_pov(db, task, job, batch)
        return _run_submit_shoe_o1(db, task, job, batch)
    if _ORIGINAL_SUBMIT_VIDEO is None:
        raise RuntimeError("Existing video submit handler is unavailable.")
    return _ORIGINAL_SUBMIT_VIDEO(db, task)


def _run_poll_shoe_o1(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.video_job_id:
        raise RuntimeError("Missing Enhancor request ID.")

    log.info("Polling Enhancor · job=%s · requestId=%s", job.id, job.video_job_id)
    result = enhancor.get_seedance_status(job.video_job_id)
    status = result["status"]
    job.video_status = status
    if result.get("thumbnail_url"):
        job.thumbnail_url = str(result["thumbnail_url"])
    if result.get("video_url"):
        job.video_source_url = str(result["video_url"])
    if result.get("error"):
        job.video_error = str(result["error"])

    if status == "completed":
        final_url = str(result.get("video_url") or job.video_source_url or "").strip()
        if not final_url:
            raise RuntimeError("Enhancor completed but did not return a video URL.")

        job.video_source_url = final_url
        job.video_url = final_url
        job.video_source_resolution = "720p"
        job.video_resolution = "720p"
        job.video_provider_used = "enhancor"
        job.video_status = "completed"
        # Seedance returns 720p. The mandatory manual FFmpeg pass normalizes the final
        # captioned export to 1080x1920, so keep 1080p pending until that pass succeeds.
        job.upscale_status = "pending"
        job.upscale_error = None
        job.video_error = None
        job.stage = "video_complete"
        db.add(job)
        db.flush()
        log.info("Enhancor COMPLETED · job=%s", job.id)
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
        raise RuntimeError(job.video_error or "Seedance video generation failed.")

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
        if str(job.video_provider_used or "").lower() == "shoe_pov" or _is_pov_batch(batch):
            return _run_poll_shoe_pov(db, task, job, batch)
        return _run_poll_shoe_o1(db, task, job, batch)
    if _ORIGINAL_POLL_VIDEO is None:
        raise RuntimeError("Existing video poll handler is unavailable.")
    return _ORIGINAL_POLL_VIDEO(db, task)


def _complete_pov_upscale(db: Session, job: ProductJob, batch: Batch, result: dict) -> None:
    media_id = str(result.get("media_id") or job.video_source_media_id or "").strip()
    video_url = str(result.get("url") or "").strip() or useapi.resolve_asset_url(media_id)
    if not media_id or not video_url:
        raise RuntimeError("Flow Shoes POV upscale completed without a usable 1080p video.")
    job.upscale_status = "completed"
    job.upscale_error = None
    job.video_media_id = media_id
    job.video_url = video_url
    job.video_resolution = "1080p"
    job.video_status = "completed"
    job.video_provider_used = "shoe_pov"
    db.add(job)
    db.flush()

    fmt = _pov_format(batch)
    description = str(job.product_name or "shoe").strip()
    try:
        video_bytes = tasks._download_final_video_for_archive(job)
        if not video_bytes:
            raise VideoGateError("The 1080p Flow Shoes POV video could not be downloaded for inspection.")
        sheets = extract_video_sheets(video_bytes, format_name=fmt, seconds=8)
        decision = OpenAIVideoGateClient().inspect(
            product_images=_pov_product_images(job, 3),
            timeline_sheet=sheets["timeline"],
            shoe_sheet=sheets["shoes"],
            product_details=description,
            format_name=fmt,
            seconds=8,
        )
    except Exception as exc:
        job.video_status = "failed"
        job.video_error = f"Flow Shoes POV quality check unavailable: {exc}"
        job.stage = "failed"
        db.add(job)
        db.flush()
        return

    if not decision.passed:
        job.video_status = "failed"
        job.video_error = "Flow Shoes POV quality check: " + "; ".join(decision.reasons)
        job.stage = "failed"
        db.add(job)
        db.flush()
        log.warning("Flow Shoes POV quality gate failed · job=%s · reasons=%s", job.id, job.video_error)
        return

    job.video_error = None
    job.stage = "video_complete"
    db.add(job)
    db.flush()
    log.info("Flow Shoes POV quality gate passed · job=%s · 1080p · silent final pending manual FFmpeg", job.id)
    tasks.enqueue_task(db, "archive_media", job_id=job.id, batch_id=job.batch_id, priority=80, max_attempts=2)
    tasks.enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def _run_submit_shoe_pov_upscale(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    source_id = str(job.video_source_media_id or "").strip()
    if not source_id:
        raise RuntimeError("Flow Shoes POV has no native video media ID to upscale.")
    job.stage = "upscaling"
    job.upscale_status = "created"
    job.upscale_attempts = int(job.upscale_attempts or 0) + 1
    job.upscale_error = None
    db.add(job)
    db.flush()
    result = FlowPovClient().submit_upscale(media_generation_id=source_id, resolution="1080p")
    if result["status"] == "completed":
        return _complete_pov_upscale(db, job, batch, result)
    job.upscale_job_id = str(result["job_id"])
    job.upscale_status = "processing"
    db.add(job)
    db.flush()
    tasks.enqueue_task(
        db, "poll_upscale", job_id=job.id, batch_id=job.batch_id, priority=60,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds), max_attempts=60,
    )


def _run_poll_shoe_pov_upscale(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.upscale_job_id:
        raise RuntimeError("Missing Flow Shoes POV upscale job ID.")
    result = FlowPovClient().get_job(job.upscale_job_id, kind="video")
    status = str(result.get("status") or "processing")
    job.upscale_status = status
    if status == "completed":
        return _complete_pov_upscale(db, job, batch, result)
    if status == "failed":
        job.upscale_error = str(result.get("error") or "Flow Shoes POV upscale failed.")
        job.stage = "failed"
        db.add(job)
        db.flush()
        return
    job.stage = "upscaling"
    db.add(job)
    db.flush()
    tasks.enqueue_task(
        db, "poll_upscale", job_id=job.id, batch_id=job.batch_id, priority=60,
        run_after=tasks._now() + timedelta(seconds=settings().poll_seconds), max_attempts=60, allow_duplicate=True,
    )


def _dispatch_submit_upscale(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch) and str(job.video_provider_used or "").lower() == "shoe_pov":
        return _run_submit_shoe_pov_upscale(db, task, job, batch)
    if _ORIGINAL_SUBMIT_UPSCALE is None:
        raise RuntimeError("Existing upscale submit handler is unavailable.")
    return _ORIGINAL_SUBMIT_UPSCALE(db, task)


def _dispatch_poll_upscale(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    batch = db.get(Batch, job.batch_id) if job else None
    if job and _is_shoe_batch(batch) and str(job.video_provider_used or "").lower() == "shoe_pov":
        return _run_poll_shoe_pov_upscale(db, task, job, batch)
    if _ORIGINAL_POLL_UPSCALE is None:
        raise RuntimeError("Existing upscale poll handler is unavailable.")
    return _ORIGINAL_POLL_UPSCALE(db, task)


def shoe_o1_images_ready(job: ProductJob) -> bool:
    if job.image_status == "completed" and job.image_media_id and job.image_url:
        return True
    for item in list(job.editorial_shots or []):
        if isinstance(item, dict) and str(item.get("shot") or "").upper() == "A":
            return str(item.get("image_status") or "") == "completed" and bool(item.get("image_media_id"))
    return False


def install_shoe_o1_handlers() -> None:
    """Replace only Shoe Showcase generation; all fashion handlers remain unchanged."""
    global _INSTALLED, _ORIGINAL_GENERATE_IMAGE, _ORIGINAL_GENERATE_EDITORIAL_FRAME, _ORIGINAL_SUBMIT_VIDEO, _ORIGINAL_POLL_VIDEO
    global _ORIGINAL_SUBMIT_UPSCALE, _ORIGINAL_POLL_UPSCALE
    if _INSTALLED:
        return

    image_handler = tasks.HANDLERS.get("generate_image")
    frame_handler = tasks.HANDLERS.get("generate_editorial_frame")
    submit_handler = tasks.HANDLERS.get("submit_video")
    poll_handler = tasks.HANDLERS.get("poll_video")
    submit_upscale_handler = tasks.HANDLERS.get("submit_upscale")
    poll_upscale_handler = tasks.HANDLERS.get("poll_upscale")
    if image_handler is None or frame_handler is None or submit_handler is None or poll_handler is None or submit_upscale_handler is None or poll_upscale_handler is None:
        raise RuntimeError("Existing Flow Fashion handlers could not be located.")

    _ORIGINAL_GENERATE_IMAGE = image_handler
    _ORIGINAL_GENERATE_EDITORIAL_FRAME = frame_handler
    _ORIGINAL_SUBMIT_VIDEO = submit_handler
    _ORIGINAL_POLL_VIDEO = poll_handler
    _ORIGINAL_SUBMIT_UPSCALE = submit_upscale_handler
    _ORIGINAL_POLL_UPSCALE = poll_upscale_handler
    tasks.HANDLERS["generate_image"] = _dispatch_generate_image
    tasks.HANDLERS["generate_editorial_frame"] = _run_shoe_frame_a_only
    tasks.HANDLERS["submit_video"] = _dispatch_submit_video
    tasks.HANDLERS["poll_video"] = _dispatch_poll_video
    tasks.HANDLERS["submit_upscale"] = _dispatch_submit_upscale
    tasks.HANDLERS["poll_upscale"] = _dispatch_poll_upscale
    _INSTALLED = True
