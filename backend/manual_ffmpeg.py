from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

from backend.models import Batch, ProductJob, QueueTask
from backend.services import useapi
import backend.tasks as tasks
import backend.text_overlay as text_overlay


log = logging.getLogger("flow-manual-ffmpeg")
_INSTALLED = False
_ORIGINAL_ARCHIVE_MEDIA: Callable[[Session, QueueTask], None] | None = None


def caption_for_job(job: ProductJob) -> str:
    """Return the same selected/default hook used by the existing FFmpeg renderer."""
    return text_overlay._fashion_caption(job)


def _archive_only_after_manual_ffmpeg(db: Session, task: QueueTask) -> None:
    """Do not archive the raw returned video; archive only after the user adds text."""
    if _ORIGINAL_ARCHIVE_MEDIA is None:
        raise RuntimeError("Original archive handler is unavailable.")
    payload = dict(task.payload or {})
    if payload.get("manual_ffmpeg_done"):
        return _ORIGINAL_ARCHIVE_MEDIA(db, task)
    log.info("Raw video ready; waiting for manual FFmpeg text · job=%s", task.job_id)


def run_apply_text_overlay(db: Session, task: QueueTask) -> None:
    job = db.get(ProductJob, task.job_id) if task.job_id else None
    if not job:
        raise RuntimeError("Product job no longer exists.")
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise RuntimeError("Batch no longer exists.")

    payload = dict(task.payload or {})
    expected_video_job_id = str(payload.get("video_job_id") or "").strip()
    current_video_job_id = str(job.video_job_id or "").strip()
    if expected_video_job_id and current_video_job_id and expected_video_job_id != current_video_job_id:
        raise RuntimeError("The source video changed before FFmpeg started. Use the button on the newest video.")
    if str(job.video_status or "").lower() != "completed":
        raise RuntimeError("Wait for the main video to finish before sending it to FFmpeg.")
    if not (job.video_media_id or job.video_url or job.video_source_media_id or job.video_source_url):
        raise RuntimeError("The returned video file is not available yet.")

    job.stage = "finalizing_text"
    db.add(job)
    db.flush()

    try:
        caption = caption_for_job(job)
        log.info("Manual FFmpeg text started · job=%s · caption=%s", job.id, caption)
        original_bytes = tasks._download_final_video_for_archive(job)
        if not original_bytes:
            raise RuntimeError("Could not download the returned video for FFmpeg.")

        final_bytes = text_overlay._burn_text(original_bytes, caption, job.id)
        uploaded = useapi.upload_video_asset(final_bytes, tasks._batch_flow_account(batch))
        final_media_id = str(uploaded.get("media_id") or "").strip()
        if not final_media_id:
            raise RuntimeError("FFmpeg output uploaded without a media ID.")

        job.video_media_id = final_media_id
        job.video_url = useapi.resolve_asset_url(final_media_id) or None
        job.video_source_email = str(uploaded.get("email") or job.video_source_email or "").strip() or None
        job.video_error = None
        # `complete` is the durable signal that text was applied. The raw returned video
        # stays at `video_complete`, so the dashboard knows when to show the manual button.
        job.stage = "complete"
        job.drive_video_id = None
        job.drive_video_url = None
        job.drive_video_download_url = None
        job.drive_error = None
        db.add(job)

        payload.update({
            "manual_ffmpeg_done": True,
            "fashion_text_overlay_applied": True,
            "fashion_text_overlay_text": caption,
            "fashion_text_overlay_media_id": final_media_id,
            "video_job_id": current_video_job_id,
        })
        task.payload = payload
        db.add(task)
        db.flush()

        tasks.enqueue_task(
            db,
            "archive_media",
            job_id=job.id,
            batch_id=job.batch_id,
            payload={"manual_ffmpeg_done": True},
            priority=80,
            max_attempts=2,
            allow_duplicate=True,
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
        log.info("Manual FFmpeg text completed · job=%s · media=%s", job.id, final_media_id)
    except Exception:
        # Keep the raw returned video usable if FFmpeg ultimately fails.
        if int(task.attempts or 0) >= int(task.max_attempts or 1):
            job.stage = "video_complete"
            db.add(job)
            db.flush()
        raise


def install_manual_ffmpeg_handler() -> None:
    """Switch automatic text rendering to a user-triggered queue task."""
    global _INSTALLED, _ORIGINAL_ARCHIVE_MEDIA
    if _INSTALLED:
        return

    original_enqueue = text_overlay._ORIGINAL_ENQUEUE_TASK
    original_archive = text_overlay._ORIGINAL_ARCHIVE_MEDIA
    if original_enqueue is None or original_archive is None:
        raise RuntimeError("Text overlay handler must be installed before manual FFmpeg mode.")

    # The old text-overlay wrapper hid the raw video and started FFmpeg whenever
    # archive_media was queued. Restore normal enqueue behavior so the raw video appears
    # immediately, then gate Drive archiving until the manual FFmpeg task finishes.
    tasks.enqueue_task = original_enqueue
    _ORIGINAL_ARCHIVE_MEDIA = original_archive
    tasks.HANDLERS["archive_media"] = _archive_only_after_manual_ffmpeg
    tasks.HANDLERS["apply_text_overlay"] = run_apply_text_overlay
    _INSTALLED = True
