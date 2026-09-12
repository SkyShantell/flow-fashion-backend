from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

import backend.api as base_api
import backend.tasks as tasks
from backend.api import app, get_db, require_api_key
from backend.flow_account_affinity import install_flow_account_affinity
from backend.models import Batch, ProductJob
from backend.schemas import UpdateVideoProviderRequest
from backend.services import useapi
from backend.text_overlay import install_text_overlay_handler
from backend.video_provider import provider_config


router = APIRouter()
install_flow_account_affinity()
install_text_overlay_handler()


# The dashboard already renders a final-video action whenever JobOut.video_url is present.
# Some uploaded final MP4 assets do not return a public UseAPI URL, so provide a stable
# same-origin download route instead of leaving completed jobs with no link at all.
_original_job_out = base_api.job_out


def _job_out_with_download(job: ProductJob):
    out = _original_job_out(job)
    if (
        not getattr(out, "video_url", None)
        and str(job.stage or "") in {"video_complete", "complete"}
        and (job.video_media_id or job.video_source_media_id or job.drive_video_id)
    ):
        out.video_url = f"/api/backend/jobs/{job.id}/download-video"
    return out


base_api.job_out = _job_out_with_download


@router.get("/jobs/{job_id}/download-video", dependencies=[Depends(require_api_key)])
def download_final_video(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if str(job.stage or "") not in {"video_complete", "complete"}:
        raise HTTPException(409, "Final video is still processing")

    video_bytes = tasks._download_final_video_for_archive(job)
    if not video_bytes:
        raise HTTPException(404, "Final video file is not available")

    base_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(job.product_name or "video")).strip("-._")[:80] or "video"
    return Response(
        content=video_bytes,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}.mp4"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/video-provider/health")
def video_provider_health():
    return {
        "ok": True,
        "providers": ["omni", "kling"],
        "kling_model": "kling-v3-0",
        "kling_duration": 8,
        "kling_audio": False,
        "kling_multi_shot": False,
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
    return provider_config(batch)


@router.put("/batches/{batch_id}/video-provider", dependencies=[Depends(require_api_key)])
def update_batch_video_provider(
    batch_id: str,
    req: UpdateVideoProviderRequest,
    db: Session = Depends(get_db),
):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    provider = useapi.normalize_video_provider(req.video_provider)
    if (batch.mode or "fashion_tryon") == "shoe_showcase" and provider == "kling":
        raise HTTPException(
            400,
            "Shoe Showcase uses its dedicated 3-clip Google Flow editorial pipeline. "
            "Choose Kling 3.0 on a Fashion Try-On batch.",
        )

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
    return provider_config(batch)


app.include_router(router)
