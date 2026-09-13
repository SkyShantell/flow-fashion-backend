from __future__ import annotations

import logging
from typing import Callable

import requests
from sqlalchemy.orm import Session

from backend.models import Batch, ProductJob, QueueTask
from backend.services import useapi
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


def _download_url(url: str) -> bytes | None:
    url = str(url or "").strip()
    if not url:
        return None
    try:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        return response.content or None
    except Exception:
        return None


def _download_ffmpeg_source(job: ProductJob, payload: dict) -> bytes | None:
    """Always render from the pre-FFmpeg video so redoes never stack old text."""
    source_media_id = str(payload.get("ffmpeg_source_media_id") or "").strip()
    source_url = str(payload.get("ffmpeg_source_url") or "").strip()

    if source_media_id:
        data, _error = useapi.download_raw_asset(source_media_id)
        if data:
            return data
    data = _download_url(source_url)
    if data:
        return data

    # Legacy completed jobs predate source snapshots. Prefer the generation source over
    # job.video_media_id, because video_media_id may already contain burned-in text.
    if job.video_source_media_id:
        data, _error = useapi.download_raw_asset(str(job.video_source_media_id))
        if data:
            return data
    data = _download_url(str(job.video_source_url or ""))
    if data:
        return data

    # First-time renders still have the raw/upscaled video as the current final asset.
    if not payload.get("redo"):
        return tasks._download_final_video_for_archive(job)
    return None


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
        uploaded = useapi.upload_video_asset(final_bytes, tasks._batch_flow_account(batch))
        final_media_id = str(uploaded.get("media_id") or "").strip()
        if not final_media_id:
            raise RuntimeError("FFmpeg output uploaded without a media ID.")

        job.video_media_id = final_media_id
        job.video_url = useapi.resolve_asset_url(final_media_id) or None
        job.video_source_email = str(uploaded.get("email") or job.video_source_email or "").strip() or None
        job.video_error = None
        job.stage = "complete"
        job.drive_video_id = None
        job.drive_video_url = None
        job.drive_video_download_url = None
        job.drive_error = None
        db.add(job)

        payload.update({
            "manual_ffmpeg_done": True,
            "fashion_text_overlay_applied": True,
            "fashion_text_overlay_text": headline,
            "fashion_text_overlay_subheadline": subheadline,
            "fashion_text_overlay_preset": preset,
            "fashion_text_overlay_emoji_prefix": emoji_prefix,
            "fashion_text_overlay_emoji_suffix": emoji_suffix,
            "fashion_text_overlay_emoji_mode": "browser_system_png" if (emoji_prefix_pngs or emoji_suffix_pngs) else "fallback",
            "fashion_text_overlay_headline_color": headline_color,
            "fashion_text_overlay_subheadline_color": subheadline_color,
            "fashion_text_overlay_placement": placement,
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
        log.info("Manual styled FFmpeg completed · job=%s · media=%s · redo=%s", job.id, final_media_id, bool(payload.get("redo")))
    except Exception:
        if int(task.attempts or 0) >= int(task.max_attempts or 1):
            # A failed redo must leave the previous successful FFmpeg version available.
            job.stage = "complete" if payload.get("redo") and job.video_media_id else "video_complete"
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
