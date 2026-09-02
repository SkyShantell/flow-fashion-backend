from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import session_scope
from backend.models import Batch, ProductJob, QueueTask, utcnow
from backend.prompts import image_prompt, video_prompt
from backend.services import drive, sheets, sociavault, useapi

TERMINAL_TASK_STATUSES = {"done", "failed", "canceled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def enqueue_task(
    db: Session,
    task_type: str,
    *,
    job_id: str | None = None,
    batch_id: str | None = None,
    payload: dict | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    allow_duplicate: bool = False,
) -> QueueTask:
    if not allow_duplicate and job_id:
        existing = (
            db.query(QueueTask)
            .filter(
                QueueTask.job_id == job_id,
                QueueTask.task_type == task_type,
                QueueTask.status.in_(["queued", "running"]),
            )
            .order_by(QueueTask.created_at.desc())
            .first()
        )
        if existing:
            return existing
    task = QueueTask(
        task_type=task_type,
        job_id=job_id,
        batch_id=batch_id,
        payload=payload or {},
        priority=priority,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
    )
    db.add(task)
    db.flush()
    return task


def _requeue(db: Session, task: QueueTask, error: str, delay_seconds: int | None = None) -> None:
    cfg = settings()
    delay = delay_seconds if delay_seconds is not None else cfg.task_backoff_seconds * max(1, task.attempts)
    task.status = "queued"
    task.error = str(error)[:4000]
    task.locked_at = None
    task.run_after = _now() + timedelta(seconds=delay)
    db.add(task)


def _fail_task(db: Session, task: QueueTask, error: str) -> None:
    task.status = "failed"
    task.error = str(error)[:4000]
    task.locked_at = None
    db.add(task)
    if task.job_id:
        job = db.get(ProductJob, task.job_id)
        if job:
            job.failure_count = int(job.failure_count or 0) + 1
            if task.task_type in {"import_product", "generate_image"}:
                job.image_status = "failed"
                job.image_error = str(error)[:4000]
                job.stage = "failed"
            elif task.task_type in {"submit_video", "poll_video"}:
                job.video_status = "failed"
                job.video_error = str(error)[:4000]
                job.stage = "failed"
            elif task.task_type in {"submit_upscale", "poll_upscale"}:
                job.upscale_status = "failed"
                job.upscale_error = str(error)[:4000]
                job.stage = "failed"
            elif task.task_type == "archive_media":
                job.drive_error = str(error)[:4000]
            db.add(job)


def claim_next_task(db: Session) -> QueueTask | None:
    # Reset tasks that were left running after a crash/redeploy.
    stale_cutoff = _now() - timedelta(minutes=20)
    stale = db.query(QueueTask).filter(QueueTask.status == "running", QueueTask.locked_at < stale_cutoff).all()
    for task in stale:
        task.status = "queued"
        task.locked_at = None
        task.run_after = _now()
        db.add(task)
    db.flush()

    query = (
        db.query(QueueTask)
        .filter(QueueTask.status == "queued", QueueTask.run_after <= _now())
        .order_by(QueueTask.priority.asc(), QueueTask.created_at.asc())
    )
    try:
        query = query.with_for_update(skip_locked=True)
    except Exception:
        pass
    task = query.first()
    if not task:
        return None
    task.status = "running"
    task.locked_at = _now()
    task.attempts = int(task.attempts or 0) + 1
    task.updated_at = _now()
    db.add(task)
    db.flush()
    return task


def _upload_avatar_if_needed(db: Session, batch: Batch) -> str:
    if batch.avatar_media_id:
        return batch.avatar_media_id
    if not batch.avatar_b64:
        raise RuntimeError("Batch has no avatar image. Add an avatar before generating.")
    raw = base64.b64decode(batch.avatar_b64)
    batch.avatar_media_id = useapi.upload_asset(raw, batch.avatar_mime or "image/jpeg", settings().google_flow_email)
    db.add(batch)
    db.flush()
    return batch.avatar_media_id


def _ensure_product_refs(db: Session, job: ProductJob, avatar_media_id: str) -> list[str]:
    selected = _as_list(job.selected_refs)
    if not selected:
        raise RuntimeError("No selected product reference images.")
    signature = hashlib.sha1("|".join(selected).encode("utf-8")).hexdigest()
    existing_refs = _as_list(job.flow_product_ref_ids)
    if job.ref_signature == signature and existing_refs:
        return [avatar_media_id] + existing_refs
    ids = []
    for url in selected[: settings().max_product_refs]:
        data, mime = sociavault.fetch_remote_image(str(url))
        ids.append(useapi.upload_asset(data, mime, settings().google_flow_email))
    if not ids:
        raise RuntimeError("No product references could be uploaded to Flow.")
    job.flow_product_ref_ids = ids
    job.ref_signature = signature
    db.add(job)
    db.flush()
    return [avatar_media_id] + ids


def run_import_product(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    job.stage = "importing"
    db.add(job)
    db.flush()

    data = sociavault.import_product(job.product_url)
    job.product_id = data["product_id"]
    job.product_name = data["product_name"]
    job.listing_images = data["listing_images"]
    job.review_images = data["review_images"]
    job.selected_refs = data["selected_refs"]
    job.focus = data["focus"]
    job.stage = "imported"
    job.image_status = "pending"
    db.add(job)
    db.flush()

    if task.payload.get("start_generation", True):
        enqueue_task(db, "generate_image", job_id=job.id, batch_id=job.batch_id, priority=20, max_attempts=2)


def run_generate_image(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")

    job.stage = "generating_image"
    job.image_status = "processing"
    job.image_attempts = int(job.image_attempts or 0) + 1
    job.image_error = None
    db.add(job)
    db.flush()

    avatar_media_id = _upload_avatar_if_needed(db, batch)
    refs = _ensure_product_refs(db, job, avatar_media_id)
    result = useapi.generate_image(
        image_prompt(job, scene=batch.scene or "Modern apartment mirror", refs_count=len(refs), creator_profile=batch.creator_profile or "Male"),
        refs,
        settings().google_flow_email,
    )
    job.image_job_id = result.get("job_id")
    job.image_media_id = result.get("media_id")
    job.image_url = result.get("url")
    job.image_seed = result.get("seed")
    job.image_status = "completed"
    job.image_error = None
    job.approved = bool(batch.auto_approve)
    job.stage = "ready_for_video" if job.approved else "awaiting_approval"
    db.add(job)
    db.flush()

    if batch.auto_approve:
        enqueue_task(db, "submit_video", job_id=job.id, batch_id=job.batch_id, priority=30, max_attempts=2)
    enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def run_submit_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")
    if not job.image_media_id:
        raise RuntimeError("No completed image media ID.")
    if not job.approved:
        raise RuntimeError("Image is not approved for video yet.")

    job.stage = "submitting_video"
    job.video_status = "created"
    job.video_error = None
    job.video_attempts = int(job.video_attempts or 0) + 1
    db.add(job)
    db.flush()

    result = useapi.submit_video(job.image_media_id, video_prompt(job, creator_profile=batch.creator_profile or "Male", video_style=batch.video_style or "Calm"), settings().google_flow_email)
    job.video_job_id = result["job_id"]
    job.video_status = str(result.get("status") or "created").lower()
    job.stage = "video_processing"
    db.add(job)
    db.flush()
    enqueue_task(db, "poll_video", job_id=job.id, batch_id=job.batch_id, priority=40, run_after=_now() + timedelta(seconds=settings().poll_seconds), max_attempts=80)
    enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def run_poll_video(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job or not job.video_job_id:
        raise RuntimeError("Missing video job ID.")
    result = useapi.parse_video_job(useapi.get_job(job.video_job_id))
    status = result.get("status") or job.video_status
    job.video_status = status
    if result.get("video_url"):
        job.video_source_url = result["video_url"]
    if result.get("video_media_id"):
        job.video_source_media_id = result["video_media_id"]
    if result.get("thumbnail_url"):
        job.thumbnail_url = result["thumbnail_url"]
    job.video_error = result.get("error")

    if status == "completed":
        job.video_source_resolution = settings().video_native_resolution
        job.stage = "ready_for_upscale"
        db.add(job)
        db.flush()
        enqueue_task(db, "submit_upscale", job_id=job.id, batch_id=job.batch_id, priority=50, max_attempts=2)
        enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)
        return

    if status == "failed":
        job.stage = "failed"
        db.add(job)
        db.flush()
        raise RuntimeError(job.video_error or "Video generation failed.")

    job.stage = "video_processing"
    db.add(job)
    db.flush()
    enqueue_task(db, "poll_video", job_id=job.id, batch_id=job.batch_id, priority=40, run_after=_now() + timedelta(seconds=settings().poll_seconds), max_attempts=80, allow_duplicate=True)


def run_submit_upscale(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    source_id = job.video_source_media_id or job.video_media_id
    if not source_id:
        raise RuntimeError("No source video mediaGenerationId to upscale.")
    job.stage = "upscaling"
    job.upscale_status = "created"
    job.upscale_attempts = int(job.upscale_attempts or 0) + 1
    job.upscale_error = None
    db.add(job)
    db.flush()

    result = useapi.submit_upscale(source_id, settings().video_final_resolution)
    if result.get("status") == "completed" and (result.get("media_id") or result.get("url")):
        job.upscale_status = "completed"
        job.video_media_id = result.get("media_id") or source_id
        job.video_url = result.get("url") or useapi.resolve_asset_url(job.video_media_id)
        job.video_resolution = settings().video_final_resolution
        job.stage = "video_complete"
        db.add(job)
        db.flush()
        enqueue_task(db, "archive_media", job_id=job.id, batch_id=job.batch_id, priority=80, max_attempts=2)
        enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)
        return

    job.upscale_job_id = result["job_id"]
    job.upscale_status = str(result.get("status") or "created").lower()
    db.add(job)
    db.flush()
    enqueue_task(db, "poll_upscale", job_id=job.id, batch_id=job.batch_id, priority=60, run_after=_now() + timedelta(seconds=settings().poll_seconds), max_attempts=60)


def run_poll_upscale(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id)
    if not job or not job.upscale_job_id:
        raise RuntimeError("Missing upscale job ID.")
    result = useapi.parse_video_job(useapi.get_job(job.upscale_job_id))
    status = result.get("status") or job.upscale_status
    job.upscale_status = status
    if result.get("video_media_id"):
        job.video_media_id = result["video_media_id"]
    if result.get("video_url"):
        job.video_url = result["video_url"]
    if result.get("thumbnail_url"):
        job.thumbnail_url = result["thumbnail_url"]
    job.upscale_error = result.get("error")

    if status == "completed":
        if job.video_media_id and not job.video_url:
            job.video_url = useapi.resolve_asset_url(job.video_media_id)
        job.video_resolution = settings().video_final_resolution
        job.stage = "video_complete"
        db.add(job)
        db.flush()
        enqueue_task(db, "archive_media", job_id=job.id, batch_id=job.batch_id, priority=80, max_attempts=2)
        enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)
        return

    if status == "failed":
        job.stage = "failed"
        db.add(job)
        db.flush()
        raise RuntimeError(job.upscale_error or "Video upscale failed.")

    job.stage = "upscaling"
    db.add(job)
    db.flush()
    enqueue_task(db, "poll_upscale", job_id=job.id, batch_id=job.batch_id, priority=60, run_after=_now() + timedelta(seconds=settings().poll_seconds), max_attempts=60, allow_duplicate=True)


def _download_image_for_archive(job: ProductJob) -> tuple[bytes | None, str]:
    if job.image_media_id:
        data, err = useapi.download_raw_asset(job.image_media_id)
        if data:
            return data, "image/jpeg"
    if job.image_url:
        try:
            data, mime = useapi.download_url(job.image_url, 120)
            return data, mime or "image/jpeg"
        except Exception:
            return None, "image/jpeg"
    return None, "image/jpeg"


def _download_final_video_for_archive(job: ProductJob) -> bytes | None:
    media_id = job.video_media_id or job.video_source_media_id
    if media_id:
        data, _err = useapi.download_raw_asset(media_id)
        if data:
            return data
    for url in [job.video_url, job.video_source_url]:
        if url:
            try:
                return useapi.download_url(url, 240)[0]
            except Exception:
                pass
    return None


def run_archive_media(db: Session, task: QueueTask) -> None:
    cfg = settings()
    if not cfg.google_drive_auto_archive:
        return
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")
    if not cfg.google_drive_archive_webhook_url or not cfg.google_drive_archive_secret:
        job.drive_error = "Drive archive is not configured."
        db.add(job)
        db.flush()
        return

    batch_name = f"Batch {batch.id}"
    batch_date = str(batch.created_at.date()) if batch.created_at else str(_now().date())
    idx = 1 + db.query(ProductJob).filter(ProductJob.batch_id == batch.id, ProductJob.created_at < job.created_at).count()
    job.archive_attempts = int(job.archive_attempts or 0) + 1

    if job.image_status == "completed" and not job.drive_image_id:
        img_bytes, img_mime = _download_image_for_archive(job)
        if img_bytes:
            payload, error = drive.archive_bytes(
                img_bytes,
                img_mime,
                drive.media_filename(idx, job.product_name or "product", job.image_media_id or "image", "image"),
                "image",
                batch_name=batch_name,
                product_name=job.product_name or "Product",
                batch_date=batch_date,
                description=f"Flow Try-On image | Product URL: {job.product_url}",
            )
            if payload:
                job.drive_image_id = str(payload.get("file_id") or "")
                job.drive_image_url = str(payload.get("view_url") or payload.get("download_url") or "")
                job.drive_image_download_url = str(payload.get("download_url") or "")
                job.drive_product_folder_url = str(payload.get("product_folder_url") or job.drive_product_folder_url or "")
                job.drive_batch_folder_url = str(payload.get("batch_folder_url") or job.drive_batch_folder_url or "")
            elif error:
                job.drive_error = error

    if job.stage in {"video_complete", "complete"} and not job.drive_video_id:
        vid_bytes = _download_final_video_for_archive(job)
        if vid_bytes:
            payload, error = drive.archive_bytes(
                vid_bytes,
                "video/mp4",
                drive.media_filename(idx, job.product_name or "product", job.video_media_id or job.video_source_media_id or "video", "video", job.video_resolution or settings().video_final_resolution),
                "video",
                batch_name=batch_name,
                product_name=job.product_name or "Product",
                batch_date=batch_date,
                description=f"Flow Try-On final video | Product URL: {job.product_url} | Resolution: {job.video_resolution or ''}",
            )
            if payload:
                job.drive_video_id = str(payload.get("file_id") or "")
                job.drive_video_url = str(payload.get("view_url") or payload.get("download_url") or "")
                job.drive_video_download_url = str(payload.get("download_url") or "")
                job.drive_product_folder_url = str(payload.get("product_folder_url") or job.drive_product_folder_url or "")
                job.drive_batch_folder_url = str(payload.get("batch_folder_url") or job.drive_batch_folder_url or "")
                job.stage = "complete"
            elif error:
                job.drive_error = error

    db.add(job)
    db.flush()
    enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)


def run_sync_sheet(db: Session, task: QueueTask) -> None:
    if not settings().google_sheet_auto_sync:
        return
    job = db.get(ProductJob, task.job_id)
    if not job:
        raise RuntimeError("Product job no longer exists.")
    ok, msg = sheets.sync_job(job, db)
    if not ok:
        # Sheets is not core generation; keep job usable, but keep the error for visibility.
        job.drive_error = msg if "Sheets" in msg else job.drive_error
        db.add(job)
        db.flush()
        raise RuntimeError(msg)


HANDLERS: dict[str, Callable[[Session, QueueTask], None]] = {
    "import_product": run_import_product,
    "generate_image": run_generate_image,
    "submit_video": run_submit_video,
    "poll_video": run_poll_video,
    "submit_upscale": run_submit_upscale,
    "poll_upscale": run_poll_upscale,
    "archive_media": run_archive_media,
    "sync_sheet": run_sync_sheet,
}


def run_task_by_id(task_id: str) -> str:
    with session_scope() as db:
        task = db.get(QueueTask, task_id)
        if not task:
            return "missing"
        if task.status not in {"running", "queued"}:
            return task.status
        handler = HANDLERS.get(task.task_type)
        if not handler:
            _fail_task(db, task, f"Unknown task type: {task.task_type}")
            return "failed"
        try:
            handler(db, task)
            task.status = "done"
            task.error = None
            task.locked_at = None
            task.updated_at = _now()
            db.add(task)
            return "done"
        except Exception as exc:
            error = str(exc)
            if task.attempts < task.max_attempts:
                _requeue(db, task, error)
                return "requeued"
            _fail_task(db, task, error)
            return "failed"


def run_one_claimed_task() -> str:
    with session_scope() as db:
        task = claim_next_task(db)
        if not task:
            return "idle"
        task_id = task.id
    return run_task_by_id(task_id)
