from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.config import google_service_account_info, settings
from backend.db import SessionLocal, init_db
from backend.models import Batch, ProductJob, QueueTask
from backend.schemas import (
    ApproveJobRequest,
    BatchOut,
    CreateBatchRequest,
    ImportProductsRequest,
    ImportScannerRequest,
    JobOut,
    RegenerateJobRequest,
    RegenerateVideoRequest,
    RetryJobRequest,
    SelectProductRefsRequest,
    UpdateJobSettingsRequest,
)
from backend.services import sheets
from backend.prompts import MOTION_STYLES, SCENES, video_prompt
from backend.tasks import enqueue_task, run_one_claimed_task

app = FastAPI(title="Flow Try-On Factory Phase 1 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(x_api_key: Annotated[str | None, Header()] = None):
    cfg = settings()
    if cfg.api_key and x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def _job_error(job: ProductJob) -> str | None:
    return job.image_error or job.video_error or job.upscale_error or job.drive_error


FOCUS_VALUES = {"outfit", "shirt", "hoodie", "pants", "shoes", "handbag"}


def _scene_pool(batch: Batch) -> list[str]:
    values = [str(x).strip() for x in list(batch.scene_pool or []) if str(x).strip()]
    return values or [batch.scene or "Modern apartment mirror"]


def _motion_pool(batch: Batch) -> list[str]:
    values = [str(x).strip() for x in list(batch.motion_pool or []) if str(x).strip()]
    return values or [batch.video_style or "Calm"]


def _assign_defaults(batch: Batch, index: int) -> tuple[str, str]:
    scenes = _scene_pool(batch)
    motions = _motion_pool(batch)
    return scenes[index % len(scenes)], motions[index % len(motions)]


def job_out(job: ProductJob) -> JobOut:
    batch = getattr(job, "batch", None)
    scene = job.scene_override or (batch.scene if batch else None) or "Modern apartment mirror"
    motion_style = job.motion_style_override or (batch.video_style if batch else None) or "Calm"
    return JobOut(
        id=job.id,
        batch_id=job.batch_id,
        product_name=job.product_name,
        product_url=job.product_url,
        product_id=job.product_id,
        focus=job.focus,
        scene=scene,
        motion_style=motion_style,
        listing_images=list(job.listing_images or []),
        review_images=list(job.review_images or []),
        selected_refs=list(job.selected_refs or []),
        stage=job.stage or "",
        approved=bool(job.approved),
        image_status=job.image_status or "pending",
        image_url=job.image_url,
        video_status=job.video_status or "pending",
        upscale_status=job.upscale_status or "pending",
        video_url=job.video_url or job.drive_video_download_url,
        video_resolution=job.video_resolution,
        drive_video_url=job.drive_video_url,
        error=_job_error(job),
    )


def batch_out(batch: Batch, db: Session) -> BatchOut:
    jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).order_by(ProductJob.created_at.asc()).all()
    stages = Counter(j.stage or "unknown" for j in jobs)
    counts = {
        "products": len(jobs),
        "images_ready": sum(1 for j in jobs if j.image_status == "completed"),
        "approved": sum(1 for j in jobs if j.approved),
        "videos_ready": sum(1 for j in jobs if j.stage in {"video_complete", "complete"}),
        "archived": sum(1 for j in jobs if j.drive_video_id),
        "failed": sum(1 for j in jobs if j.stage == "failed"),
        "queued_tasks": db.query(QueueTask).filter(QueueTask.batch_id == batch.id, QueueTask.status == "queued").count(),
        "running_tasks": db.query(QueueTask).filter(QueueTask.batch_id == batch.id, QueueTask.status == "running").count(),
        "by_stage": dict(stages),
    }
    return BatchOut(
        id=batch.id,
        name=batch.name,
        scene=batch.scene,
        scene_pool=_scene_pool(batch),
        creator_profile=batch.creator_profile,
        video_style=batch.video_style,
        motion_pool=_motion_pool(batch),
        auto_approve=bool(batch.auto_approve),
        status=batch.status or "open",
        counts=counts,
        jobs=[job_out(j) for j in jobs],
    )


@app.get("/health")
def health():
    cfg = settings()
    return {
        "ok": True,
        "useapi": bool(cfg.useapi_token),
        "sociavault": bool(cfg.sociavault_api_key),
        "google_sheet": bool(cfg.google_sheet_auto_sync and cfg.google_sheet_url and google_service_account_info()),
        "drive_archive": bool(cfg.google_drive_archive_webhook_url and cfg.google_drive_archive_secret),
        "image_model": cfg.image_model,
        "video_model": cfg.video_model,
        "video_native_resolution": cfg.video_native_resolution,
        "video_final_resolution": cfg.video_final_resolution,
    }


@app.post("/batches", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def create_batch(req: CreateBatchRequest, db: Session = Depends(get_db)):
    requested_scenes = [x for x in req.scene_pool if x in SCENES] or ([req.scene] if req.scene in SCENES else ["Modern apartment mirror"])
    requested_motions = [x for x in req.motion_pool if x in MOTION_STYLES] or ([req.video_style] if req.video_style in MOTION_STYLES else ["Calm"])
    batch = Batch(
        name=req.name,
        scene=requested_scenes[0],
        scene_pool=requested_scenes,
        creator_profile=req.creator_profile,
        video_style=requested_motions[0],
        motion_pool=requested_motions,
        auto_approve=req.auto_approve,
        avatar_b64=req.avatar_b64,
        avatar_mime=req.avatar_mime or "image/jpeg",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.post("/batches/form", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def create_batch_form(
    name: str = Form("Flow batch"),
    scene: str = Form("Modern apartment mirror"),
    creator_profile: str = Form("Male"),
    video_style: str = Form("Calm"),
    auto_approve: bool = Form(False),
    avatar: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    avatar_b64 = None
    avatar_mime = "image/jpeg"
    if avatar:
        data = avatar.file.read()
        avatar_b64 = base64.b64encode(data).decode("ascii")
        avatar_mime = avatar.content_type or "image/jpeg"
    batch = Batch(
        name=name,
        scene=scene if scene in SCENES else "Modern apartment mirror",
        scene_pool=[scene if scene in SCENES else "Modern apartment mirror"],
        creator_profile=creator_profile,
        video_style=video_style if video_style in MOTION_STYLES else "Calm",
        motion_pool=[video_style if video_style in MOTION_STYLES else "Calm"],
        auto_approve=auto_approve,
        avatar_b64=avatar_b64,
        avatar_mime=avatar_mime,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.get("/batches", dependencies=[Depends(require_api_key)])
def list_batches(db: Session = Depends(get_db)):
    batches = db.query(Batch).order_by(Batch.created_at.desc()).limit(100).all()
    return [batch_out(b, db) for b in batches]


@app.get("/batches/{batch_id}", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch_out(batch, db)


@app.post("/batches/{batch_id}/products", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def import_products(batch_id: str, req: ImportProductsRequest, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    links = []
    for link in req.links:
        link = str(link or "").strip()
        if link and link not in links:
            links.append(link)
    if not links:
        raise HTTPException(400, "No product links supplied")

    existing_jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).order_by(ProductJob.created_at.asc()).all()
    existing = {j.product_url for j in existing_jobs}
    next_index = len(existing_jobs)
    for link in links[: settings().max_batch_links]:
        if link in existing:
            continue
        assigned_scene, assigned_motion = _assign_defaults(batch, next_index)
        next_index += 1
        job = ProductJob(batch_id=batch.id, product_url=link, stage="pending_import", scene_override=assigned_scene, motion_style_override=assigned_motion)
        db.add(job)
        db.flush()
        enqueue_task(db, "import_product", job_id=job.id, batch_id=batch.id, payload={"start_generation": req.start_generation}, priority=10, max_attempts=3)
    batch.updated_at = datetime.now(timezone.utc)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.get("/scanner/pending", dependencies=[Depends(require_api_key)])
def scanner_pending(max_items: int = 50):
    rows, error = sheets.scanner_pending()
    if error:
        raise HTTPException(500, error)
    return rows[:max_items]


@app.post("/batches/{batch_id}/scanner/import", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def import_from_scanner(batch_id: str, req: ImportScannerRequest, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    pending, error = sheets.scanner_pending()
    if error:
        raise HTTPException(500, error)
    if req.row_nums:
        wanted = {int(x) for x in req.row_nums}
        selected = [r for r in pending if int(r.get("_row_num") or 0) in wanted]
    else:
        selected = pending[: max(1, int(req.max_items or 10))]
    if not selected:
        raise HTTPException(400, "No pending Scanner Queue rows selected")

    row_nums = [int(r.get("_row_num")) for r in selected if int(r.get("_row_num") or 0) >= 2]
    mark_error = sheets.mark_scanner_rows(row_nums, "Importing", batch.id)

    existing_jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).order_by(ProductJob.created_at.asc()).all()
    existing = {j.product_url for j in existing_jobs}
    next_index = len(existing_jobs)
    for rec in selected:
        link = str(rec.get("Product Link") or "").strip()
        if not link or link in existing:
            continue
        assigned_scene, assigned_motion = _assign_defaults(batch, next_index)
        next_index += 1
        job = ProductJob(
            batch_id=batch.id,
            product_url=link,
            scene_override=assigned_scene,
            motion_style_override=assigned_motion,
            product_name=str(rec.get("Product Name") or "Unknown Product"),
            stage="pending_import",
            scanner_row_num=int(rec.get("_row_num") or 0) or None,
            scanner_creators=str(rec.get("Creators") or ""),
            scanner_creator_count=int(float(str(rec.get("Creator Count") or "0").replace(",", ""))) if str(rec.get("Creator Count") or "").replace(",", "").replace(".", "").isdigit() else None,
            scanner_video_count=int(float(str(rec.get("Video Count") or "0").replace(",", ""))) if str(rec.get("Video Count") or "").replace(",", "").replace(".", "").isdigit() else None,
            scanner_combined_views=int(float(str(rec.get("Combined Views") or "0").replace(",", ""))) if str(rec.get("Combined Views") or "").replace(",", "").replace(".", "").isdigit() else None,
        )
        db.add(job)
        db.flush()
        enqueue_task(db, "import_product", job_id=job.id, batch_id=batch.id, payload={"start_generation": req.start_generation}, priority=10, max_attempts=3)
    batch.updated_at = datetime.now(timezone.utc)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    if mark_error:
        # Keep this nonfatal because queueing succeeded.
        out = batch_out(batch, db).dict()
        out["scanner_mark_warning"] = mark_error
        return out
    return batch_out(batch, db)


@app.post("/jobs/{job_id}/references", response_model=JobOut, dependencies=[Depends(require_api_key)])
def select_product_references(job_id: str, req: SelectProductRefsRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    refs = []
    for raw in req.refs:
        ref = str(raw or "").strip()
        if ref and ref not in refs:
            refs.append(ref)

    if not refs:
        raise HTTPException(400, "Select at least one product photo")
    if len(refs) > settings().max_product_refs:
        raise HTTPException(400, f"Select no more than {settings().max_product_refs} product photos")

    allowed = {str(x) for x in list(job.listing_images or []) + list(job.review_images or []) if str(x).strip()}
    invalid = [ref for ref in refs if allowed and ref not in allowed]
    if invalid:
        raise HTTPException(400, "One or more selected photos are not from this imported product")

    if req.focus is not None:
        focus = str(req.focus).strip().lower()
        if focus not in FOCUS_VALUES:
            raise HTTPException(400, "Unknown product type")
        job.focus = focus
    if req.scene is not None:
        scene = str(req.scene).strip()
        if scene not in SCENES:
            raise HTTPException(400, "Unknown background setting")
        job.scene_override = scene
    if req.motion_style is not None:
        motion = str(req.motion_style).strip()
        if motion not in MOTION_STYLES:
            raise HTTPException(400, "Unknown motion style")
        job.motion_style_override = motion

    job.selected_refs = refs
    # Force Flow reference uploads to be rebuilt when the user changes photos.
    job.flow_product_ref_ids = []
    job.ref_signature = None
    job.approved = False
    job.image_status = "pending"
    job.image_error = None
    job.image_job_id = None
    job.image_media_id = None
    job.image_url = None
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.stage = "ready_for_image"
    db.add(job)
    db.flush()

    if req.start_generation:
        job.stage = "queued_image"
        db.add(job)
        enqueue_task(db, "generate_image", job_id=job.id, batch_id=job.batch_id, priority=20, max_attempts=2, allow_duplicate=True)

    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/production-settings", response_model=JobOut, dependencies=[Depends(require_api_key)])
def update_job_production_settings(job_id: str, req: UpdateJobSettingsRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    image_started = job.image_status in {"processing", "completed"} or job.stage in {"generating_image", "awaiting_approval", "ready_for_video", "submitting_video", "video_processing", "upscaling", "complete"}
    if image_started and (req.focus is not None or req.scene is not None):
        raise HTTPException(409, "Product type/background are locked after image generation starts. Regenerate the image to change them.")

    if req.focus is not None:
        focus = str(req.focus).strip().lower()
        if focus not in FOCUS_VALUES:
            raise HTTPException(400, "Unknown product type")
        job.focus = focus
    if req.scene is not None:
        scene = str(req.scene).strip()
        if scene not in SCENES:
            raise HTTPException(400, "Unknown background setting")
        job.scene_override = scene
    if req.motion_style is not None:
        motion = str(req.motion_style).strip()
        if motion not in MOTION_STYLES:
            raise HTTPException(400, "Unknown motion style")
        job.motion_style_override = motion

    db.add(job)
    db.commit()
    db.refresh(job)
    return job_out(job)


def _default_video_prompt(job: ProductJob, db: Session) -> str:
    batch = db.get(Batch, job.batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return video_prompt(
        job,
        creator_profile=batch.creator_profile or "Male",
        video_style=job.motion_style_override or batch.video_style or "Calm",
    )


def _last_video_prompt(job: ProductJob, db: Session) -> tuple[str, str]:
    task = (
        db.query(QueueTask)
        .filter(QueueTask.job_id == job.id, QueueTask.task_type == "submit_video")
        .order_by(QueueTask.created_at.desc())
        .first()
    )
    payload = dict(task.payload or {}) if task else {}
    used = str(payload.get("prompt_used") or payload.get("prompt_override") or "").strip()
    if used:
        return used, "last_used"
    return _default_video_prompt(job, db), "default"


@app.get("/jobs/{job_id}/video-prompt", dependencies=[Depends(require_api_key)])
def get_video_prompt(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    default = _default_video_prompt(job, db)
    used, source = _last_video_prompt(job, db)
    return {
        "job_id": job.id,
        "default_prompt": default,
        "prompt_used": used,
        "source": source,
        "can_regenerate": bool(job.image_status == "completed" and job.approved),
    }


@app.post("/jobs/{job_id}/regenerate-video", response_model=JobOut, dependencies=[Depends(require_api_key)])
def regenerate_video(job_id: str, req: RegenerateVideoRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.image_status != "completed" or not job.image_media_id:
        raise HTTPException(400, "A completed try-on image is required before regenerating video.")
    if not job.approved:
        raise HTTPException(400, "Approve the image before regenerating video.")

    active = (
        db.query(QueueTask)
        .filter(
            QueueTask.job_id == job.id,
            QueueTask.task_type.in_(["submit_video", "poll_video", "submit_upscale", "poll_upscale"]),
            QueueTask.status.in_(["queued", "running"]),
        )
        .first()
    )
    if active:
        raise HTTPException(409, "This video is still processing. Wait for the current attempt to finish before regenerating it.")

    prompt = str(req.prompt or "").strip() or _default_video_prompt(job, db)

    # Keep the approved still image, but reset only the video/upscale/archive-video pipeline.
    job.video_status = "pending"
    job.video_job_id = None
    job.video_source_media_id = None
    job.video_source_url = None
    job.video_source_resolution = None
    job.thumbnail_url = None
    job.video_error = None
    job.upscale_status = "pending"
    job.upscale_job_id = None
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.upscale_error = None
    job.drive_video_id = None
    job.drive_video_url = None
    job.drive_video_download_url = None
    job.drive_error = None
    job.stage = "queued_video_regen"
    db.add(job)
    db.flush()
    enqueue_task(
        db,
        "submit_video",
        job_id=job.id,
        batch_id=job.batch_id,
        payload={"prompt_override": prompt, "regenerated": True},
        priority=25,
        max_attempts=2,
        allow_duplicate=True,
    )
    enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=300, max_attempts=2, allow_duplicate=True)
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/approve", response_model=JobOut, dependencies=[Depends(require_api_key)])
def approve_job(job_id: str, req: ApproveJobRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.approved = bool(req.approved)
    if job.approved and job.image_status == "completed":
        job.stage = "ready_for_video"
        if req.start_video:
            enqueue_task(db, "submit_video", job_id=job.id, batch_id=job.batch_id, priority=30, max_attempts=2)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/regenerate", response_model=JobOut, dependencies=[Depends(require_api_key)])
def regenerate_job(job_id: str, req: RegenerateJobRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.regen_instruction = req.instruction or ""
    job.last_regen_instruction = req.instruction or ""
    job.approved = False
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.stage = "pending_image_regen"
    db.add(job)
    db.flush()
    enqueue_task(db, "generate_image", job_id=job.id, batch_id=job.batch_id, priority=20, max_attempts=2, allow_duplicate=True)
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/retry", response_model=JobOut, dependencies=[Depends(require_api_key)])
def retry_job(job_id: str, req: RetryJobRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    step = (req.step or "auto").lower()
    if step == "auto":
        if job.stage in {"pending_import", "importing"} or not job.product_id:
            step = "import"
        elif job.image_status != "completed":
            step = "image"
        elif not job.approved:
            raise HTTPException(400, "Approve the image before retrying video.")
        elif job.video_status != "completed":
            step = "video"
        elif job.upscale_status != "completed":
            step = "upscale"
        else:
            step = "archive"
    mapping = {
        "import": "import_product",
        "image": "generate_image",
        "video": "submit_video",
        "upscale": "submit_upscale",
        "archive": "archive_media",
        "sheet": "sync_sheet",
    }
    task_type = mapping.get(step)
    if not task_type:
        raise HTTPException(400, "step must be auto/import/image/video/upscale/archive/sheet")
    job.stage = f"retry_{step}"
    if step == "image":
        job.image_status = "pending"
        job.image_error = None
    elif step == "video":
        job.video_status = "pending"
        job.video_error = None
    elif step == "upscale":
        job.upscale_status = "pending"
        job.upscale_error = None
    db.add(job)
    db.flush()
    enqueue_task(db, task_type, job_id=job.id, batch_id=job.batch_id, priority=15, max_attempts=3, allow_duplicate=True)
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/batches/{batch_id}/sync-sheet", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def sync_batch_sheet(batch_id: str, db: Session = Depends(get_db)):
    """Queue Google Sheet sync for every product without changing production stages."""
    cfg = settings()
    if not cfg.google_sheet_auto_sync or not cfg.google_sheet_url or not google_service_account_info():
        raise HTTPException(400, "Google Sheets auto-sync is not configured on the API service.")
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).all()
    if not jobs:
        raise HTTPException(400, "This batch has no products to sync.")
    for job in jobs:
        # Earlier builds surfaced sheet failures through drive_error. Clear only that legacy message.
        if job.drive_error and "Google Sheets" in str(job.drive_error):
            job.drive_error = None
        db.add(job)
        db.flush()
        enqueue_task(db, "sync_sheet", job_id=job.id, batch_id=job.batch_id, priority=5, max_attempts=3, allow_duplicate=True)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.post("/batches/{batch_id}/retry-failed", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def retry_failed(batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id, ProductJob.stage == "failed").all()
    for job in jobs:
        if not job.product_id:
            task_type = "import_product"
        elif job.image_status == "failed":
            task_type = "generate_image"
        elif job.video_status == "failed":
            task_type = "submit_video"
        elif job.upscale_status == "failed":
            task_type = "submit_upscale"
        else:
            task_type = "archive_media"
        job.stage = "queued_retry"
        db.add(job)
        enqueue_task(db, task_type, job_id=job.id, batch_id=job.batch_id, priority=15, max_attempts=3, allow_duplicate=True)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_api_key)])
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job_out(job)


@app.get("/tasks", dependencies=[Depends(require_api_key)])
def tasks(status: str = "queued", limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(QueueTask)
    if status != "all":
        q = q.filter(QueueTask.status == status)
    rows = q.order_by(QueueTask.created_at.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "job_id": t.job_id,
            "batch_id": t.batch_id,
            "attempts": t.attempts,
            "run_after": t.run_after.isoformat() if t.run_after else None,
            "error": t.error,
        }
        for t in rows
    ]


@app.post("/worker/run-once", dependencies=[Depends(require_api_key)])
def worker_run_once():
    return {"result": run_one_claimed_task()}
