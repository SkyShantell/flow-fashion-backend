from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api import app, get_db, require_api_key
from backend.flow_account_affinity import install_flow_account_affinity
from backend.models import Batch
from backend.schemas import UpdateVideoProviderRequest
from backend.services import useapi
from backend.text_overlay import install_text_overlay_handler
from backend.video_provider import provider_config


router = APIRouter()
install_flow_account_affinity()
install_text_overlay_handler()


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
