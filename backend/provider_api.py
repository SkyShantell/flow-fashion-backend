from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

import backend.api as base_api
import backend.tasks as tasks
import backend.shoe_o1 as shoe_o1
from backend.api import app, get_db, require_api_key
from backend.flow_account_affinity import install_flow_account_affinity
from backend.manual_ffmpeg import caption_for_job, install_manual_ffmpeg_handler
from backend.models import Batch, EmojiAsset, ProductJob, QueueTask
from backend.schemas import ApplyTextOverlayRequest, EmojiSeedRequest, UpdateVideoProviderRequest
from backend.services import useapi
from backend.shoe_o1 import install_shoe_o1_handlers, shoe_o1_images_ready
from backend.shoe_o1_prompt import shoe_o1_video_prompt
from backend.styled_overlay import overlay_options
from backend.text_overlay import install_text_overlay_handler
from backend.video_provider import install_video_provider_handlers, provider_config


router = APIRouter()

install_flow_account_affinity()
install_video_provider_handlers()
install_text_overlay_handler()
shoe_o1.shoe_o1_video_prompt = shoe_o1_video_prompt
install_shoe_o1_handlers()
install_manual_ffmpeg_handler()

_original_api_settings = base_api.settings


def _api_settings_with_o1_refs():
    cfg = _original_api_settings()
    return replace(cfg, max_product_refs=max(6, int(cfg.max_product_refs or 0)))


base_api.settings = _api_settings_with_o1_refs
base_api.shoe_showcase_video_prompt = shoe_o1_video_prompt
base_api._editorial_images_ready = shoe_o1_images_ready

_original_job_out = base_api.job_out


def _job_out_with_download(job: ProductJob):
    out = _original_job_out(job)
    if str(job.stage or "") in {"video_complete", "finalizing_text", "complete"}:
        if job.video_media_id or job.video_source_media_id or job.video_url or job.video_source_url:
            out.video_url = f"/api/backend/jobs/{job.id}/download-video"
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


def _safe_emoji_pngs(values: list[str] | None, limit: int = 120) -> list[str]:
    out: list[str] = []
    for raw in list(values or [])[:limit]:
        value = str(raw or "").strip()
        if not value:
            out.append("")
            continue
        if value.startswith("data:image/png;base64,") and len(value) <= 450_000:
            out.append(value)
        else:
            out.append("")
    return out


def _emoji_tokens(value: str, limit: int = 8) -> list[str]:
    return [token for token in str(value or "").strip().split() if token][:limit]


def _cache_apple_assets(db: Session, tokens: list[str], pngs: list[str]) -> int:
    safe_pngs = _safe_emoji_pngs(pngs, limit=max(1, len(tokens)))
    saved = 0
    for index, token in enumerate(tokens):
        token = str(token or "").strip()[:160]
        png = safe_pngs[index] if index < len(safe_pngs) else ""
        if not token or not png:
            continue
        asset = db.get(EmojiAsset, token)
        if asset is None:
            asset = EmojiAsset(token=token, image_b64=png, source="apple_browser")
        else:
            asset.image_b64 = png
            asset.source = "apple_browser"
        db.add(asset)
        saved += 1
    db.flush()
    return saved


def _cached_apple_pngs(db: Session, tokens: list[str]) -> tuple[list[str], list[str]]:
    pngs: list[str] = []
    missing: list[str] = []
    for token in tokens:
        asset = db.get(EmojiAsset, token)
        if asset and str(asset.source or "") == "apple_browser" and str(asset.image_b64 or "").startswith("data:image/png;base64,"):
            pngs.append(str(asset.image_b64))
        else:
            pngs.append("")
            missing.append(token)
    return pngs, missing


def _latest_completed_overlay_payload(db: Session, job: ProductJob) -> dict:
    current_video_job_id = str(job.video_job_id or "").strip()
    rows = (
        db.query(QueueTask)
        .filter(
            QueueTask.job_id == job.id,
            QueueTask.task_type == "apply_text_overlay",
            QueueTask.status == "done",
        )
        .order_by(QueueTask.created_at.desc())
        .limit(20)
        .all()
    )
    for row in rows:
        payload = dict(row.payload or {})
        payload_video_job_id = str(payload.get("video_job_id") or "").strip()
        if not current_video_job_id or not payload_video_job_id or payload_video_job_id == current_video_job_id:
            return payload
    return {}


def _active_overlay_task(db: Session, job: ProductJob) -> QueueTask | None:
    return (
        db.query(QueueTask)
        .filter(
            QueueTask.job_id == job.id,
            QueueTask.task_type == "apply_text_overlay",
            QueueTask.status.in_(["queued", "running"]),
        )
        .order_by(QueueTask.created_at.desc())
        .first()
    )


@router.get("/jobs/{job_id}/download-video", dependencies=[Depends(require_api_key)])
def download_final_video(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if str(job.stage or "") not in {"video_complete", "finalizing_text", "complete"}:
        raise HTTPException(409, "Video is still processing")

    video_bytes = tasks._download_final_video_for_archive(job)
    if not video_bytes:
        raise HTTPException(404, "Video file is not available")

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


@router.post("/api/apple-emoji-assets", dependencies=[Depends(require_api_key)])
def seed_apple_emoji_assets(req: EmojiSeedRequest, db: Session = Depends(get_db)):
    if str(req.source or "").strip().lower() != "apple_browser":
        raise HTTPException(400, "Apple emoji assets must be seeded from an Apple browser")
    tokens = [str(token or "").strip()[:160] for token in list(req.tokens or [])[:120]]
    if not tokens:
        return {"ok": True, "saved": 0}
    saved = _cache_apple_assets(db, tokens, list(req.pngs or [])[:120])
    db.commit()
    return {"ok": True, "saved": saved}


@router.get("/jobs/{job_id}/text-overlay-config", dependencies=[Depends(require_api_key)])
def text_overlay_config(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    batch = db.get(Batch, job.batch_id)
    shoe_mode = bool(batch and (batch.mode or "fashion_tryon") == "shoe_showcase")
    previous = _latest_completed_overlay_payload(db, job)
    return {
        "headline": str(previous.get("headline") or caption_for_job(job)),
        "subheadline": str(previous.get("subheadline") or ""),
        "preset": str(previous.get("preset") or ("luxury_serif" if shoe_mode else "clean_social")),
        "emoji_prefix": str(previous.get("emoji_prefix") or ""),
        "emoji_suffix": str(previous.get("emoji_suffix") or ""),
        "headline_color": str(previous.get("headline_color") or "white"),
        "subheadline_color": str(previous.get("subheadline_color") or "white"),
        "placement": str(previous.get("placement") or "middle"),
        **overlay_options(),
        "emoji_mode": "server_apple_cache",
        "is_redo": bool(previous),
    }


@router.post("/jobs/{job_id}/redo-text-overlay", dependencies=[Depends(require_api_key)])
def redo_text_overlay(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if str(job.video_status or "").lower() != "completed":
        raise HTTPException(409, "The main video is not complete")
    if str(job.stage or "") != "complete":
        raise HTTPException(409, "FFmpeg can only be redone after a completed text render")
    active = _active_overlay_task(db, job)
    if active:
        raise HTTPException(409, "FFmpeg is already processing this video")

    # Keep the previous successful final asset in place until the redo succeeds. We only
    # reopen the styling stage; the next apply task will render from the saved raw source.
    job.stage = "video_complete"
    db.add(job)
    db.commit()
    return {"ok": True, "status": "ready", "job_id": job.id}


@router.post("/jobs/{job_id}/apply-text-overlay", dependencies=[Depends(require_api_key)])
def apply_text_overlay(
    job_id: str,
    http_request: Request,
    req: ApplyTextOverlayRequest | None = None,
    db: Session = Depends(get_db),
):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if str(job.video_status or "").lower() != "completed":
        raise HTTPException(409, "Wait for the main video to finish first")
    if str(job.stage or "") == "complete":
        raise HTTPException(409, "Use Redo FFmpeg to reopen the completed overlay")
    if str(job.stage or "") not in {"video_complete", "finalizing_text"}:
        raise HTTPException(409, "This video is not ready for FFmpeg yet")

    active = _active_overlay_task(db, job)
    if active:
        return {
            "ok": True,
            "status": active.status,
            "caption": str((active.payload or {}).get("headline") or caption_for_job(job)),
            "task_id": active.id,
        }

    previous = _latest_completed_overlay_payload(db, job)
    redo = bool(previous)
    request = req or ApplyTextOverlayRequest()
    headline = " ".join(str(request.headline or caption_for_job(job)).split()).strip()[:120]
    subheadline = " ".join(str(request.subheadline or "").split()).strip()[:120]
    if not headline and not subheadline:
        raise HTTPException(400, "Add at least one line of text")

    prefix_tokens = _emoji_tokens(request.emoji_prefix)
    suffix_tokens = _emoji_tokens(request.emoji_suffix)
    source = str(request.emoji_source or "server_cache").strip().lower()
    user_agent = str(http_request.headers.get("user-agent") or "")
    if re.search(r"Macintosh|Mac OS X|iPhone|iPad|iPod", user_agent, re.IGNORECASE):
        source = "apple_browser"

    if source == "apple_browser":
        _cache_apple_assets(db, prefix_tokens, list(request.emoji_prefix_pngs or []))
        _cache_apple_assets(db, suffix_tokens, list(request.emoji_suffix_pngs or []))

    prefix_pngs, prefix_missing = _cached_apple_pngs(db, prefix_tokens)
    suffix_pngs, suffix_missing = _cached_apple_pngs(db, suffix_tokens)
    missing = list(dict.fromkeys(prefix_missing + suffix_missing))
    if missing:
        preview = " ".join(missing[:5])
        raise HTTPException(
            409,
            f"Apple emoji asset not installed for: {preview}. Open Style + FFmpeg once on a Mac to add it, then the Windows VA can use it.",
        )

    # Snapshot the pre-FFmpeg source on the first render. Every redo reuses that snapshot,
    # so changing the caption/style never burns new text on top of old text.
    if redo:
        ffmpeg_source_media_id = str(previous.get("ffmpeg_source_media_id") or job.video_source_media_id or "").strip()
        ffmpeg_source_url = str(previous.get("ffmpeg_source_url") or job.video_source_url or "").strip()
    else:
        ffmpeg_source_media_id = str(job.video_media_id or job.video_source_media_id or "").strip()
        ffmpeg_source_url = str(job.video_url or job.video_source_url or "").strip()

    job.stage = "finalizing_text"
    db.add(job)
    db.flush()
    task = tasks.enqueue_task(
        db,
        "apply_text_overlay",
        job_id=job.id,
        batch_id=job.batch_id,
        payload={
            "video_job_id": str(job.video_job_id or ""),
            "redo": redo,
            "ffmpeg_source_media_id": ffmpeg_source_media_id,
            "ffmpeg_source_url": ffmpeg_source_url,
            "headline": headline,
            "subheadline": subheadline,
            "preset": str(request.preset or "luxury_serif")[:40],
            "emoji_prefix": str(request.emoji_prefix or "")[:80],
            "emoji_suffix": str(request.emoji_suffix or "")[:80],
            "emoji_prefix_pngs": prefix_pngs,
            "emoji_suffix_pngs": suffix_pngs,
            "headline_color": str(request.headline_color or "white")[:30],
            "subheadline_color": str(request.subheadline_color or "white")[:30],
            "placement": str(request.placement or "middle")[:20],
        },
        priority=75,
        max_attempts=2,
        allow_duplicate=True,
    )
    db.commit()
    return {
        "ok": True,
        "status": "queued",
        "caption": headline,
        "redo": redo,
        "emoji_mode": "server_apple_cache" if (prefix_tokens or suffix_tokens) else "none",
        "task_id": task.id,
    }


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
        "ffmpeg_text_mode": "manual_styled_redo",
        "ffmpeg_color_emoji": True,
        "ffmpeg_apple_emoji": "persistent server cache seeded from Apple browser",
    }


@router.get("/api/kling/accounts", dependencies=[Depends(require_api_key)])
def kling_accounts_status():
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
