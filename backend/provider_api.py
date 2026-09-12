from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

import backend.api as base_api
import backend.tasks as tasks
import backend.shoe_o1 as shoe_o1
from backend.api import app, get_db, require_api_key
from backend.flow_account_affinity import install_flow_account_affinity
from backend.models import Batch, ProductJob
from backend.schemas import UpdateVideoProviderRequest
from backend.services import useapi
from backend.shoe_o1 import install_shoe_o1_handlers, shoe_o1_images_ready
from backend.shoe_o1_prompt import shoe_o1_video_prompt
from backend.text_overlay import install_text_overlay_handler
from backend.video_provider import install_video_provider_handlers, provider_config


router = APIRouter()

# API-side task helpers mirror the worker install order. The worker is what executes queued
# jobs, but keeping the same handler chain here makes prompt previews and enqueue behavior
# consistent with production.
install_flow_account_affinity()
install_video_provider_handlers()
install_text_overlay_handler()
shoe_o1.shoe_o1_video_prompt = shoe_o1_video_prompt
install_shoe_o1_handlers()

# Shoe O1 can use the approved Flow opener + six product references. Keep the worker's
# Flow image-reference cap unchanged; this API-only override lets a shoe job persist a
# sixth product ref for Kling even though Frame A still uses the existing Flow ref limit.
_original_api_settings = base_api.settings


def _api_settings_with_o1_refs():
    cfg = _original_api_settings()
    return replace(cfg, max_product_refs=max(6, int(cfg.max_product_refs or 0)))


base_api.settings = _api_settings_with_o1_refs
base_api.shoe_showcase_video_prompt = shoe_o1_video_prompt
base_api._editorial_images_ready = shoe_o1_images_ready


# Never expose a provider/raw video as the final deliverable. A finished fashion video
# must have a distinct post-processed media ID from the source media ID before the
# dashboard gets a playable/downloadable URL.
_original_job_out = base_api.job_out


def _has_final_text_render(job: ProductJob) -> bool:
    final_id = str(job.video_media_id or "").strip()
    source_id = str(job.video_source_media_id or "").strip()
    if not final_id:
        return False
    return not source_id or final_id != source_id


def _job_out_with_download(job: ProductJob):
    out = _original_job_out(job)
    if str(job.stage or "") in {"video_complete", "complete"}:
        if _has_final_text_render(job):
            # Force every completed job through the backend final-video route so the UI
            # cannot accidentally expose an older provider/upscale URL without text.
            out.video_url = f"/api/backend/jobs/{job.id}/download-video"
        else:
            # Fail closed while FFmpeg finalization is pending rather than showing raw video.
            out.video_url = None
            if hasattr(out, "drive_video_url"):
                out.drive_video_url = None
            if hasattr(out, "drive_video_download_url"):
                out.drive_video_download_url = None
    return out


base_api.job_out = _job_out_with_download


def _provider_config(batch: Batch) -> dict:
    if (batch.mode or "fashion_tryon") == "shoe_showcase":
        return {
            "batch_id": batch.id,
            "batch_name": batch.name,
            "mode": batch.mode or "shoe_showcase",
            "video_provider": "kling",
            "video_provider_label": "Kling O1",
            "kling_account_email": batch.kling_account_email,
            "kling_model": "kling-o1",
            "kling_mode": "pro",
            "kling_duration": 10,
            "kling_audio": False,
            "kling_multi_shot": False,
            "aspect_ratio": "9:16 · approved Flow image @image_1 + up to 6 shoe refs",
            "automatic_fallback": False,
            "locked_for_shoes": True,
        }
    return provider_config(batch)


@router.get("/jobs/{job_id}/download-video", dependencies=[Depends(require_api_key)])
def download_final_video(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if str(job.stage or "") not in {"video_complete", "complete"}:
        raise HTTPException(409, "Final video is still processing")
    if not _has_final_text_render(job):
        raise HTTPException(409, "Final text overlay is still processing")

    video_bytes = tasks._download_final_video_for_archive(job)
    if not video_bytes:
        raise HTTPException(404, "Final video file is not available")

    base_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(job.product_name or "video")).strip("-._")[:80] or "video"
    return Response(
        content=video_bytes,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}.mp4"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/api/video-provider/health")
def video_provider_health():
    return {
        "ok": True,
        "providers": ["omni", "kling"],
        "fashion_kling_model": "kling-v3-0",
        "fashion_kling_duration": 8,
        "shoe_kling_model": "kling-o1",
        "shoe_kling_duration": 10,
        "shoe_reference_limit": 7,
        "kling_audio": False,
        "automatic_fallback": False,
        "fashion_text_overlay": True,
    }


@router.get("/api/kling/accounts", dependencies=[Depends(require_api_key)])
def kling_accounts_status():
    """Return sanitized UseAPI Kling account metadata. No auth token leaves the backend."""
    try:
        return useapi.list_kling_accounts()
    except Exception as exc:
        raise HTTPException(502, f"Could not load Kling accounts: {exc}")


@router.get("/batches/{batch_id}/video-provider", dependencies=[Depends(require_api_key)])
def get_batch_video_provider(batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return _provider_config(batch)


@router.put("/batches/{batch_id}/video-provider", dependencies=[Depends(require_api_key)])
def update_batch_video_provider(
    batch_id: str,
    req: UpdateVideoProviderRequest,
    db: Session = Depends(get_db),
):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    if (batch.mode or "fashion_tryon") == "shoe_showcase":
        # Shoe Showcase is intentionally locked to O1. Flow remains the still-image
        # generator only; the approved opener then becomes @image_1 for Kling O1.
        try:
            account = useapi.resolve_kling_account_email(
                req.kling_account_email or batch.kling_account_email or ""
            )
        except Exception as exc:
            raise HTTPException(400, f"Kling O1 could not be selected: {exc}")
        batch.video_provider = "kling"
        batch.kling_account_email = account
        batch.kling_model = "kling-o1"
        batch.kling_mode = "pro"
        batch.updated_at = datetime.now(timezone.utc)
        db.add(batch)
        db.commit()
        db.refresh(batch)
        return _provider_config(batch)

    provider = useapi.normalize_video_provider(req.video_provider)
    if provider == "kling":
        try:
            account = useapi.resolve_kling_account_email(
                req.kling_account_email or batch.kling_account_email or ""
            )
        except Exception as exc:
            raise HTTPException(400, f"Kling 3.0 could not be selected: {exc}")
        batch.kling_account_email = account
        batch.kling_model = "kling-v3-0"
        batch.kling_mode = "pro"
    else:
        batch.kling_model = "kling-v3-0"
        batch.kling_mode = "pro"

    batch.video_provider = provider
    batch.updated_at = datetime.now(timezone.utc)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return _provider_config(batch)


app.include_router(router)
