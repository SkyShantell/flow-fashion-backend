from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

from backend.models import Batch, ProductJob, QueueTask
from backend.services import drive, useapi
from backend.styled_overlay import render_styled_overlay
import backend.tasks as tasks
import backend.text_overlay as text_overlay


log = logging.getLogger("flow-manual-ffmpeg")
_INSTALLED = False
_ORIGINAL_ARCHIVE_MEDIA: Callable[[Session, QueueTask], None] | None = None


def caption_for_job(job: ProductJob) -> str:
    """Return the selected/default hook as the starting text in the manual style editor."""
    return text_overlay._fashion_caption(job)


def _archive_only_after_manual_ffmpeg(db: Session, task: QueueTask) -> None:
    """Do not archive the raw returned video; archive only after the user adds text."""
    if _ORIGINAL_ARCHIVE_MEDIA is None:
        raise RuntimeError("Original archive handler is unavailable.")
    payload = dict(task.payload or {})
    if payload.get("manual_ffmpeg_done"):
        return _ORIGINAL_ARCHIVE_MEDIA(db, task)
    log.info("Raw video ready; waiting for manual FFmpeg text · job=%s", task.job_id)


def _download_ffmpeg_source(job: ProductJob, payload: dict) -> bytes | None:
    """Always render from the pre-FFmpeg video so redoes never stack old text."""
    source_media_id = str(payload.get("ffmpeg_source_media_id") or "").strip()
    source_url = str(payload.get("ffmpeg_source_url") or "").strip()
    if source_media_id or source_url:
        try:
            data, _mime = tasks._asset_bytes(source_media_id, source_url)
            if data:
                return data
        except Exception:
            pass

    # Legacy completed jobs predate source snapshots. Prefer the generation source over
    # job.video_media_id, because video_media_id may already contain burned-in text.
    legacy_media_id = str(job.video_source_media_id or "").strip()
    legacy_url = str(job.video_source_url or "").strip()
    if legacy_media_id or legacy_url:
        try:
            data, _mime = tasks._asset_bytes(legacy_media_id, legacy_url)
            if data:
                return data
        except Exception:
            pass

    # First-time renders still have the raw/upscaled video as the current final asset.
    if not payload.get("redo"):
        return tasks._download_final_video_for_archive(job)
    return None


def _archive_ffmpeg_fallback(db: Session, batch: Batch, job: ProductJob, final_bytes: bytes) -> dict:
    """Persist a rendered MP4 to Drive when Google Flow refuses a video asset upload."""
    idx = 1 + db.query(ProductJob).filter(
        ProductJob.batch_id == batch.id,
        ProductJob.created_at < job.created_at,
    ).count()
    batch_name = f"Batch {batch.id}"
    batch_date = str(batch.created_at.date()) if batch.created_at else str(job.created_at.date())
    media_tag = str(job.video_media_id or job.video_source_media_id or job.id)
    payload, error = drive.archive_bytes(
        final_bytes,
        "video/mp4",
        drive.media_filename(
            idx,
            job.product_name or "product",
            f"{media_tag}-ffmpeg",
            "video",
            job.video_resolution or "1080p",
        ),
        "video",
        batch_name=batch_name,
        product_name=job.product_name or "Product",
        batch_date=batch_date,
        description=f"Flow Fashion FFmpeg final video | Product URL: {job.product_url or ''} | Resolution: {job.video_resolution or '1080p'}",
    )
    if not payload:
        raise RuntimeError(error or "Drive fallback did not return a saved video.")
    return payload


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
        headline = str(payload.get("headline") or caption_for_job(job)).strip()[:120]
        subheadline = str(payload.get("subheadline") or "").strip()[:120]
        preset = str(payload.get("preset") or "luxury_serif").strip()[:40]
        emoji_prefix = str(payload.get("emoji_prefix") or "").strip()[:80]
        emoji_suffix = str(payload.get("emoji_suffix") or "").strip()[:80]
        emoji_prefix_pngs = list(payload.get("emoji_prefix_pngs") or [])[:8]
        emoji_suffix_pngs = list(payload.get("emoji_suffix_pngs") or [])[:8]
        headline_color = str(payload.get("headline_color") or "white").strip()[:30]
        subheadline_color = str(payload.get("subheadline_color") or "white").strip()[:30]
        placement = str(payload.get("placement") or "middle").strip()[:20]

        log.info(
            "Manual styled FFmpeg started · job=%s · preset=%s · headline=%s · apple_emoji=%s · redo=%s",
            job.id,
            preset,
            headline,
            bool(emoji_prefix_pngs or emoji_suffix_pngs),
            bool(payload.get("redo")),
        )
        original_bytes = _download_ffmpeg_source(job, payload)
        if not original_bytes:
            raise RuntimeError("Could not download the original pre-FFmpeg video.")

        final_bytes = render_styled_overlay(
            original_bytes,
            headline=headline,
            subheadline=subheadline,
            preset=preset,
            emoji_prefix=emoji_prefix,
            emoji_suffix=emoji_suffix,
            emoji_prefix_pngs=emoji_prefix_pngs,
            emoji_suffix_pngs=emoji_suffix_pngs,
            headline_color=headline_color,
            subheadline_color=subheadline_color,
            placement=placement,
        )

        final_media_id = ""
        final_url = ""
        final_source_email = ""
        storage_mode = "flow_asset"
        drive_payload: dict | None = None

        try:
            uploaded = useapi.upload_video_asset(final_bytes, tasks._batch_flow_account(batch))
            final_media_id = str(uploaded.get("media_id") or "").strip()
            if not final_media_id:
                raise RuntimeError("FFmpeg output uploaded without a media ID.")
            final_url = str(useapi.resolve_asset_url(final_media_id) or "").strip()
            final_source_email = str(uploaded.get("email") or job.video_source_email or "").strip()
        except Exception as upload_exc:
            upload_error = str(upload_exc)
            if "HTTP 410" not in upload_error and "Video upload init failed" not in upload_error:
                raise
            log.warning(
                "Flow rejected FFmpeg MP4 upload; using Drive fallback · job=%s · error=%s",
                job.id,
                upload_error[:1000],
            )
            drive_payload = _archive_ffmpeg_fallback(db, batch, job, final_bytes)
            final_url = str(
                drive_payload.get("download_url")
                or drive_payload.get("view_url")
                or ""
            ).strip()
            if not final_url:
                raise RuntimeError("Drive saved the FFmpeg output but returned no usable URL.")
            storage_mode = "drive_fallback"

        if storage_mode == "drive_fallback" and drive_payload:
            # Preserve the raw source in this completed task payload for future Redo FFmpeg,
            # but remove it from the job's final-media fields so downloads cannot accidentally
            # fall back to the uncaptioned source video.
            job.video_media_id = None
            job.video_url = final_url
            job.video_source_media_id = None
            job.video_source_url = None
            job.drive_video_id = str(drive_payload.get("file_id") or "").strip() or None
            job.drive_video_url = str(
                drive_payload.get("view_url") or drive_payload.get("download_url") or ""
            ).strip() or None
            job.drive_video_download_url = str(drive_payload.get("download_url") or "").strip() or None
            job.drive_error = None
        else:
            job.video_media_id = final_media_id
            job.video_url = final_url or None
            job.video_source_email = final_source_email or None
            job.drive_video_id = None
            job.drive_video_url = None
            job.drive_video_download_url = None
            job.drive_error = None

        job.video_error = None
        job.stage = "complete"
        db.add(job)

        payload.update({
            "manual_ffmpeg_done": True,
            "fashion_text_overlay_applied": True,
            "fashion_text_overlay_text": headline,
            "fashion_text_overlay_subheadline": subheadline,
            "fashion_text_overlay_preset": preset,
            "fashion_text_overlay_emoji_prefix": emoji_prefix,
            "fashion_text_overlay_emoji_suffix": emoji_suffix,
            "fashion_text_overlay_emoji_mode": "server_apple_cache" if (emoji_prefix_pngs or emoji_suffix_pngs) else "none",
            "fashion_text_overlay_headline_color": headline_color,
            "fashion_text_overlay_subheadline_color": subheadline_color,
            "fashion_text_overlay_placement": placement,
            "fashion_text_overlay_media_id": final_media_id,
            "fashion_text_overlay_storage": storage_mode,
            "fashion_text_overlay_url": final_url,
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
        log.info(
            "Manual styled FFmpeg completed · job=%s · media=%s · storage=%s · redo=%s",
            job.id,
            final_media_id or "drive",
            storage_mode,
            bool(payload.get("redo")),
        )
    except Exception:
        if int(task.attempts or 0) >= int(task.max_attempts or 1):
            # A failed redo must leave the previous successful FFmpeg version available.
            job.stage = "complete" if payload.get("redo") and (job.video_media_id or job.video_url or job.drive_video_download_url) else "video_complete"
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

    tasks.enqueue_task = original_enqueue
    _ORIGINAL_ARCHIVE_MEDIA = original_archive
    tasks.HANDLERS["archive_media"] = _archive_only_after_manual_ffmpeg
    tasks.HANDLERS["apply_text_overlay"] = run_apply_text_overlay
    _INSTALLED = True
