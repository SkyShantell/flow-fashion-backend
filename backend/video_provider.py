from __future__ import annotations

from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Batch, ProductJob, QueueTask
from backend.prompts import default_motion_style, video_prompt
from backend.services import useapi
import backend.tasks as tasks


_INSTALLED = False
_ORIGINAL_SUBMIT_VIDEO: Callable[[Session, QueueTask], None] | None = None
_ORIGINAL_POLL_VIDEO: Callable[[Session, QueueTask], None] | None = None


def provider_name(value: str | None) -> str:
    """Normalize the stored/manual provider choice. There is intentionally no fallback mode."""
    return useapi.normalize_video_provider(value)


def provider_config(batch: Batch) -> dict:
    provider = provider_name(batch.video_provider)
    return {
        "batch_id": batch.id,
        "batch_name": batch.name,
        "mode": batch.mode or "fashion_tryon",
        "video_provider": provider,
        "video_provider_label": "Kling 3.0" if provider == "kling" else "Google Flow",
        "kling_account_email": batch.kling_account_email,
        "kling_model": "kling-v3-0",
        "kling_mode": "pro",
        "kling_duration": 8,
        "kling_audio": False,
        "kling_multi_shot": False,
        "aspect_ratio": "9:16 from approved Flow start frame",
        "automatic_fallback": False,
    }


def _submit_kling_30(image_url: str, prompt: str, *, email: str = "") -> dict:
    """Submit the approved Flow image directly to Kling 3.0 with the locked production settings."""
    cfg = settings()
    account = useapi.resolve_kling_account_email(email)
    body = {
        "email": account,
        "image": str(image_url),
        "prompt": str(prompt or "")[:2500],
        "duration": "8",
        "model_name": "kling-v3-0",
        "mode": "pro",
        "enable_audio": False,
        "multi_shot": False,
    }
    payload = useapi.request_json(
        "POST",
        f"{cfg.kling_base}/videos/image2video-frames",
        headers=useapi.flow_headers(cfg.useapi_token, True),
        json_body=body,
        timeout=180,
        retries=1,
    )
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_id = task.get("id") or payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError("Kling 3.0 submitted without returning a task ID.")
    return {
        "job_id": str(task_id),
        "status": str(task.get("status_name") or payload.get("status_name") or "submitted").lower(),
        "email": account,
    }


def _run_submit_kling_video(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.image_media_id:
        raise RuntimeError("No completed image media ID.")
    if not job.approved:
        raise RuntimeError("Image is not approved for video yet.")
    if (batch.mode or "fashion_tryon") == "shoe_showcase":
        raise RuntimeError("Shoe Showcase currently uses the Google Flow editorial-video pipeline.")

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

    prompt_text = str((task.payload or {}).get("prompt_override") or "").strip()
    if not prompt_text:
        prompt_text = video_prompt(
            job,
            creator_profile=batch.creator_profile or "Male",
            video_style=job.motion_style_override
            or batch.video_style
            or default_motion_style(batch.creator_profile or "Male"),
        )
    task.payload = {**dict(task.payload or {}), "prompt_used": prompt_text, "video_provider": "kling"}
    db.add(task)
    db.flush()

    # Kling image-to-video needs its own uploaded asset URL. The exact approved Google Flow
    # image is downloaded and re-uploaded without changing the frame or aspect ratio.
    raw, mime = tasks._asset_bytes(job.image_media_id, job.image_url or "")
    kling_asset = useapi.upload_kling_asset(raw, mime, batch.kling_account_email or "")
    account = str(kling_asset.get("email") or batch.kling_account_email or "").strip()
    result = _submit_kling_30(str(kling_asset.get("url") or ""), prompt_text, email=account)

    job.video_job_id = result["job_id"]
    job.video_provider_used = "kling"
    job.video_provider_account = str(result.get("email") or account or "").strip() or None
    job.video_source_email = job.video_provider_account
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


def _run_poll_kling_video(db: Session, task: QueueTask, job: ProductJob, batch: Batch) -> None:
    if not job.video_job_id:
        raise RuntimeError("Missing Kling video task ID.")

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
                # The task result URL is still a valid fallback. This is URL cleanup only,
                # never a switch to Google Flow.
                pass
        if not final_url:
            raise RuntimeError("Kling 3.0 completed but did not return a video URL.")

        job.video_source_url = final_url
        job.video_url = final_url
        job.video_source_resolution = settings().video_native_resolution
        job.video_resolution = settings().video_native_resolution
        job.video_provider_used = "kling"
        job.video_provider_account = account or job.video_provider_account
        job.video_source_email = account or job.video_source_email
        job.video_status = "completed"
        # Kling is the selected final video provider. Do not send its result through the
        # Google Flow upscale path, which would defeat the explicit provider selection.
        job.upscale_status = "completed"
        job.upscale_error = None
        job.video_error = None
        job.stage = "video_complete"
        db.add(job)
        db.flush()
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
        raise RuntimeError(job.video_error or "Kling 3.0 video generation failed.")

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


def _dispatch_submit_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")

    # Shoe Showcase has a dedicated three-clip Flow/Omni editorial workflow.
    provider = "omni" if (batch.mode or "fashion_tryon") == "shoe_showcase" else provider_name(batch.video_provider)
    if provider == "kling":
        return _run_submit_kling_video(db, task, job, batch)

    if _ORIGINAL_SUBMIT_VIDEO is None:
        raise RuntimeError("Google Flow video handler is unavailable.")
    _ORIGINAL_SUBMIT_VIDEO(db, task)
    # Record the provider actually used after the existing Flow handler resolves the account.
    job.video_provider_used = "omni"
    job.video_provider_account = job.video_source_email or tasks._batch_flow_account(batch) or None
    db.add(job)
    db.flush()


def _dispatch_poll_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")

    # Poll with the provider that actually accepted the job. Changing the batch selector
    # later only affects future submissions; it never silently migrates an in-flight job.
    if provider_name(job.video_provider_used) == "kling" and str(job.video_provider_used or "").lower() == "kling":
        return _run_poll_kling_video(db, task, job, batch)

    if _ORIGINAL_POLL_VIDEO is None:
        raise RuntimeError("Google Flow poll handler is unavailable.")
    return _ORIGINAL_POLL_VIDEO(db, task)


def install_video_provider_handlers() -> None:
    """Install direct, manual provider routing into the existing queue worker."""
    global _INSTALLED, _ORIGINAL_SUBMIT_VIDEO, _ORIGINAL_POLL_VIDEO
    if _INSTALLED:
        return
    _ORIGINAL_SUBMIT_VIDEO = tasks.HANDLERS.get("submit_video")
    _ORIGINAL_POLL_VIDEO = tasks.HANDLERS.get("poll_video")
    if _ORIGINAL_SUBMIT_VIDEO is None or _ORIGINAL_POLL_VIDEO is None:
        raise RuntimeError("Existing Flow video handlers could not be located.")
    tasks.HANDLERS["submit_video"] = _dispatch_submit_video
    tasks.HANDLERS["poll_video"] = _dispatch_poll_video
    _INSTALLED = True
