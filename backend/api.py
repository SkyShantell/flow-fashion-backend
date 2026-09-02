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
from backend.models import Batch, ProductJob, QueueTask, SavedAvatar
from backend.schemas import (
    ApproveJobRequest,
    AvatarOut,
    BatchOut,
    CreateBatchRequest,
    ImportProductsRequest,
    ImportScannerRequest,
    JobOut,
    RegenerateJobRequest,
    RegenerateVideoRequest,
    EditorialRegenerateRequest,
    RetryJobRequest,
    SelectProductRefsRequest,
    SaveAvatarRequest,
    UpdateJobSettingsRequest,
)
from backend.services import sheets
from backend.prompts import MOTION_STYLES, SCENES, SHOE_SHOWCASE_MOTION, SHOE_SHOWCASE_SCENE, default_motion_style, video_prompt, shoe_showcase_video_prompt
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
    for shot in list(job.editorial_shots or []):
        if isinstance(shot, dict):
            for key in ("image_error", "video_error", "upscale_error"):
                if shot.get(key):
                    return str(shot.get(key))
    return job.image_error or job.video_error or job.upscale_error or job.drive_error


def _editorial_list(job: ProductJob) -> list[dict]:
    return [dict(x) for x in list(job.editorial_shots or []) if isinstance(x, dict)]


def _editorial_item(job: ProductJob, shot: str) -> dict:
    shot = str(shot or "").upper()
    for item in _editorial_list(job):
        if str(item.get("shot") or "").upper() == shot:
            return item
    raise HTTPException(400, f"Editorial frame {shot} is not initialized yet")


def _editorial_update(job: ProductJob, shot: str, **updates) -> None:
    shot = str(shot or "").upper()
    items = _editorial_list(job)
    found = False
    out = []
    for item in items:
        item = dict(item)
        if str(item.get("shot") or "").upper() == shot:
            item.update(updates)
            found = True
        out.append(item)
    if not found:
        role = {"A": "opening", "B": "showcase", "C": "detail"}.get(shot, "shot")
        out.append({"shot": shot, "role": role, **updates})
    job.editorial_shots = out


def _editorial_images_ready(job: ProductJob) -> bool:
    items = _editorial_list(job)
    return len(items) == 3 and all(str(x.get("image_status") or "") == "completed" for x in items)


def _editorial_upscales_ready(job: ProductJob) -> bool:
    items = _editorial_list(job)
    return len(items) == 3 and all(str(x.get("upscale_status") or "") == "completed" for x in items)


FOCUS_VALUES = {"outfit", "shirt", "hoodie", "pants", "shoes", "handbag"}


def _scene_pool(batch: Batch) -> list[str]:
    values = [str(x).strip() for x in list(batch.scene_pool or []) if str(x).strip()]
    return values or [batch.scene or "Modern apartment mirror"]


def _motion_pool(batch: Batch) -> list[str]:
    values = [str(x).strip() for x in list(batch.motion_pool or []) if str(x).strip()]
    return values or [batch.video_style or default_motion_style(batch.creator_profile or "Male")]


def _assign_defaults(batch: Batch, index: int) -> tuple[str, str]:
    scenes = _scene_pool(batch)
    motions = _motion_pool(batch)
    return scenes[index % len(scenes)], motions[index % len(motions)]


def job_out(job: ProductJob) -> JobOut:
    batch = getattr(job, "batch", None)
    scene = job.scene_override or (batch.scene if batch else None) or "Modern apartment mirror"
    motion_style = job.motion_style_override or (batch.video_style if batch else None) or default_motion_style(batch.creator_profile if batch else "Male")
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
        editorial_shots=list(job.editorial_shots or []),
        stage=job.stage or "",
        approved=bool(job.approved),
        image_status=job.image_status or "pending",
        image_url=job.image_url,
        video_status=job.video_status or "pending",
        upscale_status=job.upscale_status or "pending",
        video_url=job.video_url or job.drive_video_download_url,
        video_resolution=job.video_resolution,
        drive_video_url=job.drive_video_url,
        drive_video_download_url=job.drive_video_download_url,
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
        mode=batch.mode or "fashion_tryon",
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


@app.get("/avatars", response_model=list[AvatarOut], dependencies=[Depends(require_api_key)])
def list_saved_avatars(db: Session = Depends(get_db)):
    rows = db.query(SavedAvatar).order_by(SavedAvatar.created_at.desc()).all()
    return [AvatarOut(id=row.id, name=row.name, image_b64=row.image_b64, image_mime=row.image_mime or "image/jpeg") for row in rows]


@app.post("/avatars", response_model=AvatarOut, dependencies=[Depends(require_api_key)])
def save_avatar(req: SaveAvatarRequest, db: Session = Depends(get_db)):
    name = str(req.name or "Saved avatar").strip()[:160] or "Saved avatar"
    image_b64 = str(req.image_b64 or "").strip()
    if not image_b64:
        raise HTTPException(400, "Avatar image is required")
    try:
        base64.b64decode(image_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Avatar image is not valid base64")
    row = SavedAvatar(name=name, image_b64=image_b64, image_mime=req.image_mime or "image/jpeg")
    db.add(row)
    db.commit()
    db.refresh(row)
    return AvatarOut(id=row.id, name=row.name, image_b64=row.image_b64, image_mime=row.image_mime or "image/jpeg")


@app.delete("/avatars/{avatar_id}", dependencies=[Depends(require_api_key)])
def delete_saved_avatar(avatar_id: str, db: Session = Depends(get_db)):
    row = db.get(SavedAvatar, avatar_id)
    if not row:
        raise HTTPException(404, "Avatar not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/batches", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def create_batch(req: CreateBatchRequest, db: Session = Depends(get_db)):
    mode = "shoe_showcase" if str(req.mode or "").strip().lower() == "shoe_showcase" else "fashion_tryon"
    if mode == "shoe_showcase":
        requested_scenes = [SHOE_SHOWCASE_SCENE]
        requested_motions = [SHOE_SHOWCASE_MOTION]
        creator_profile = "Male" if str(req.creator_profile or "").lower().startswith("m") else "Female"
        avatar_b64 = None
    else:
        requested_scenes = [x for x in req.scene_pool if x in SCENES and x != SHOE_SHOWCASE_SCENE] or ([req.scene] if req.scene in SCENES and req.scene != SHOE_SHOWCASE_SCENE else ["Modern apartment mirror"])
        default_motion = default_motion_style(req.creator_profile)
        requested_motions = [x for x in req.motion_pool if x in MOTION_STYLES and x != SHOE_SHOWCASE_MOTION] or ([req.video_style] if req.video_style in MOTION_STYLES and req.video_style != SHOE_SHOWCASE_MOTION else [default_motion])
        creator_profile = req.creator_profile
        avatar_b64 = req.avatar_b64
        if not avatar_b64:
            raise HTTPException(400, "Fashion Try-On batches require an avatar image")
    batch = Batch(
        name=req.name,
        mode=mode,
        scene=requested_scenes[0],
        scene_pool=requested_scenes,
        creator_profile=creator_profile,
        video_style=requested_motions[0],
        motion_pool=requested_motions,
        auto_approve=req.auto_approve,
        avatar_b64=avatar_b64,
        avatar_mime=req.avatar_mime or "image/jpeg",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.post("/batches/form", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def create_batch_form(
    name: str = Form("Flow batch"),
    mode: str = Form("fashion_tryon"),
    scene: str = Form("Modern apartment mirror"),
    creator_profile: str = Form("Male"),
    video_style: str = Form("Academy — Boss / Calm"),
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
    resolved_mode = "shoe_showcase" if str(mode or "").strip().lower() == "shoe_showcase" else "fashion_tryon"
    if resolved_mode == "shoe_showcase":
        resolved_scene = SHOE_SHOWCASE_SCENE
        resolved_motion = SHOE_SHOWCASE_MOTION
        avatar_b64 = None
        creator_profile = "Male" if str(creator_profile or "").lower().startswith("m") else "Female"
    else:
        resolved_scene = scene if scene in SCENES and scene != SHOE_SHOWCASE_SCENE else "Modern apartment mirror"
        resolved_motion = video_style if video_style in MOTION_STYLES and video_style != SHOE_SHOWCASE_MOTION else default_motion_style(creator_profile)
        if not avatar_b64:
            raise HTTPException(400, "Fashion Try-On batches require an avatar image")
    batch = Batch(
        name=name,
        mode=resolved_mode,
        scene=resolved_scene,
        scene_pool=[resolved_scene],
        creator_profile=creator_profile,
        video_style=resolved_motion,
        motion_pool=[resolved_motion],
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
    batch = db.get(Batch, job.batch_id)
    shoe_mode = bool(batch and (batch.mode or "fashion_tryon") == "shoe_showcase")

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

    if shoe_mode:
        job.focus = "shoes"
        job.scene_override = SHOE_SHOWCASE_SCENE
        job.motion_style_override = SHOE_SHOWCASE_MOTION
    else:
        if req.focus is not None:
            focus = str(req.focus).strip().lower()
            if focus not in FOCUS_VALUES:
                raise HTTPException(400, "Unknown product type")
            job.focus = focus
        if req.scene is not None:
            scene = str(req.scene).strip()
            if scene not in SCENES or scene == SHOE_SHOWCASE_SCENE:
                raise HTTPException(400, "Unknown background setting")
            job.scene_override = scene
        if req.motion_style is not None:
            motion = str(req.motion_style).strip()
            if motion not in MOTION_STYLES or motion == SHOE_SHOWCASE_MOTION:
                raise HTTPException(400, "Unknown motion style")
            job.motion_style_override = motion

    job.selected_refs = refs
    # Force Flow reference uploads to be rebuilt when the user changes photos.
    job.flow_product_ref_ids = []
    job.ref_signature = None
    job.approved = False
    job.editorial_shots = []
    job.image_status = "pending"
    job.image_error = None
    job.image_job_id = None
    job.image_media_id = None
    job.image_url = None
    job.video_status = "pending"
    job.video_job_id = None
    job.video_source_media_id = None
    job.video_source_url = None
    job.upscale_status = "pending"
    job.upscale_job_id = None
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.drive_video_id = None
    job.drive_video_url = None
    job.drive_video_download_url = None
    job.stage = "ready_for_image"
    db.add(job)
    db.flush()

    if req.start_generation:
        if shoe_mode:
            job.stage = "editorial_frames_queued"
            job.image_status = "processing"
            db.add(job)
            enqueue_task(db, "generate_editorial_frame", job_id=job.id, batch_id=job.batch_id, payload={"shot": "A"}, priority=20, max_attempts=2, allow_duplicate=True)
        else:
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
    if (batch.mode or "fashion_tryon") == "shoe_showcase":
        return shoe_showcase_video_prompt(job, creator_profile=batch.creator_profile or "Female")
    return video_prompt(
        job,
        creator_profile=batch.creator_profile or "Male",
        video_style=job.motion_style_override or batch.video_style or default_motion_style(batch.creator_profile or "Male"),
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



@app.post("/jobs/{job_id}/editorial/frames/{shot}/regenerate", response_model=JobOut, dependencies=[Depends(require_api_key)])
def regenerate_editorial_frame(job_id: str, shot: str, req: EditorialRegenerateRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    batch = db.get(Batch, job.batch_id)
    if not batch or (batch.mode or "fashion_tryon") != "shoe_showcase":
        raise HTTPException(400, "This job is not a Shoe Showcase")
    shot = str(shot or "").upper()
    if shot not in {"A", "B", "C"}:
        raise HTTPException(400, "shot must be A, B or C")

    active = db.query(QueueTask).filter(
        QueueTask.job_id == job.id,
        QueueTask.task_type.in_(["generate_editorial_frame", "submit_editorial_clip", "poll_editorial_clip", "submit_editorial_upscale", "poll_editorial_upscale", "stitch_editorial_video"]),
        QueueTask.status.in_(["queued", "running"]),
    ).first()
    if active:
        raise HTTPException(409, "This shoe is still processing. Wait for the current task to finish first.")

    # Changing a start frame invalidates the final edit. If A changes, B/C are also regenerated
    # because they use A as a consistency reference.
    targets = ["A", "B", "C"] if shot == "A" else [shot]
    for target in targets:
        _editorial_update(
            job, target,
            image_status="pending", image_media_id="", image_url="", image_error="",
            video_status="pending", video_job_id="", video_media_id="", video_url="", video_error="",
            upscale_status="pending", upscale_job_id="", upscaled_media_id="", upscaled_url="", upscale_error="",
        )
    job.approved = False
    job.image_status = "processing"
    job.image_error = None
    if shot == "A":
        job.image_job_id = None
        job.image_media_id = None
        job.image_url = None
        job.image_seed = None
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.drive_video_id = None
    job.drive_video_url = None
    job.drive_video_download_url = None
    job.stage = f"editorial_frame_{shot.lower()}_queued"
    db.add(job)
    db.flush()
    enqueue_task(
        db, "generate_editorial_frame", job_id=job.id, batch_id=job.batch_id,
        payload={"shot": shot, "instruction": str(req.instruction or "").strip()},
        priority=20, max_attempts=2, allow_duplicate=True,
    )
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/editorial/generate-video", response_model=JobOut, dependencies=[Depends(require_api_key)])
def generate_editorial_video(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    batch = db.get(Batch, job.batch_id)
    if not batch or (batch.mode or "fashion_tryon") != "shoe_showcase":
        raise HTTPException(400, "This job is not a Shoe Showcase")
    if not _editorial_images_ready(job):
        raise HTTPException(400, "All three editorial frames must finish before generating the video")
    active = db.query(QueueTask).filter(
        QueueTask.job_id == job.id,
        QueueTask.task_type.in_(["submit_video", "submit_editorial_clip", "poll_editorial_clip", "submit_editorial_upscale", "poll_editorial_upscale", "stitch_editorial_video"]),
        QueueTask.status.in_(["queued", "running"]),
    ).first()
    if active:
        raise HTTPException(409, "This editorial video is already processing")
    job.approved = True
    job.video_status = "pending"
    job.upscale_status = "pending"
    job.video_error = None
    job.upscale_error = None
    job.stage = "ready_for_editorial_video"
    db.add(job)
    db.flush()
    enqueue_task(db, "submit_video", job_id=job.id, batch_id=job.batch_id, priority=30, max_attempts=2, allow_duplicate=True)
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/editorial/clips/{shot}/regenerate", response_model=JobOut, dependencies=[Depends(require_api_key)])
def regenerate_editorial_clip(job_id: str, shot: str, req: EditorialRegenerateRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    batch = db.get(Batch, job.batch_id)
    if not batch or (batch.mode or "fashion_tryon") != "shoe_showcase":
        raise HTTPException(400, "This job is not a Shoe Showcase")
    shot = str(shot or "").upper()
    if shot not in {"A", "B", "C"}:
        raise HTTPException(400, "shot must be A, B or C")
    item = _editorial_item(job, shot)
    if str(item.get("image_status") or "") != "completed" or not item.get("image_media_id"):
        raise HTTPException(400, f"Editorial frame {shot} is not ready")

    active = db.query(QueueTask).filter(
        QueueTask.job_id == job.id,
        QueueTask.task_type.in_(["submit_editorial_clip", "poll_editorial_clip", "submit_editorial_upscale", "poll_editorial_upscale", "stitch_editorial_video"]),
        QueueTask.status.in_(["queued", "running"]),
    ).first()
    if active:
        raise HTTPException(409, "This editorial video is still processing")

    _editorial_update(
        job, shot,
        video_status="pending", video_job_id="", video_media_id="", video_url="", video_error="",
        upscale_status="pending", upscale_job_id="", upscaled_media_id="", upscaled_url="", upscale_error="",
    )
    job.approved = True
    job.video_status = "processing"
    job.upscale_status = "processing"
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.drive_video_id = None
    job.drive_video_url = None
    job.drive_video_download_url = None
    job.stage = f"editorial_clip_{shot.lower()}_queued"
    db.add(job)
    db.flush()
    enqueue_task(
        db, "submit_editorial_clip", job_id=job.id, batch_id=job.batch_id,
        payload={"shot": shot, "prompt_override": str(req.prompt or "").strip()},
        priority=30, max_attempts=2, allow_duplicate=True,
    )
    db.commit()
    db.refresh(job)
    return job_out(job)


@app.post("/jobs/{job_id}/editorial/rebuild", response_model=JobOut, dependencies=[Depends(require_api_key)])
def rebuild_editorial_video(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    batch = db.get(Batch, job.batch_id)
    if not batch or (batch.mode or "fashion_tryon") != "shoe_showcase":
        raise HTTPException(400, "This job is not a Shoe Showcase")
    if not _editorial_upscales_ready(job):
        raise HTTPException(400, "All three editorial clips must be ready before rebuilding the cut")
    job.video_status = "processing"
    job.video_media_id = None
    job.video_url = None
    job.video_resolution = None
    job.drive_video_id = None
    job.drive_video_url = None
    job.drive_video_download_url = None
    job.stage = "editorial_ready_to_stitch"
    db.add(job)
    db.flush()
    enqueue_task(db, "stitch_editorial_video", job_id=job.id, batch_id=job.batch_id, priority=70, max_attempts=3, allow_duplicate=True)
    db.commit()
    db.refresh(job)
    return job_out(job)

@app.post("/jobs/{job_id}/retry", response_model=JobOut, dependencies=[Depends(require_api_key)])
def retry_job(job_id: str, req: RetryJobRequest, db: Session = Depends(get_db)):
    job = db.get(ProductJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    step = (req.step or "auto").lower()
    batch = db.get(Batch, job.batch_id)
    if batch and (batch.mode or "fashion_tryon") == "shoe_showcase" and step == "auto":
        for item in _editorial_list(job):
            shot = str(item.get("shot") or "").upper()
            if str(item.get("image_status") or "") == "failed":
                job.image_status = "processing"
                job.image_error = None
                job.stage = f"editorial_frame_{shot.lower()}_queued"
                db.add(job); db.flush()
                enqueue_task(db, "generate_editorial_frame", job_id=job.id, batch_id=job.batch_id, payload={"shot": shot}, priority=15, max_attempts=3, allow_duplicate=True)
                db.commit(); db.refresh(job); return job_out(job)
        for item in _editorial_list(job):
            shot = str(item.get("shot") or "").upper()
            if str(item.get("video_status") or "") == "failed" or str(item.get("upscale_status") or "") == "failed":
                _editorial_update(job, shot, video_status="pending", video_error="", upscale_status="pending", upscale_error="")
                job.video_status = "processing"; job.upscale_status = "processing"; job.stage = f"editorial_clip_{shot.lower()}_queued"
                db.add(job); db.flush()
                enqueue_task(db, "submit_editorial_clip", job_id=job.id, batch_id=job.batch_id, payload={"shot": shot}, priority=15, max_attempts=3, allow_duplicate=True)
                db.commit(); db.refresh(job); return job_out(job)
        if _editorial_upscales_ready(job) and job.video_status != "completed":
            enqueue_task(db, "stitch_editorial_video", job_id=job.id, batch_id=job.batch_id, priority=15, max_attempts=3, allow_duplicate=True)
            job.stage = "editorial_ready_to_stitch"; db.add(job); db.commit(); db.refresh(job); return job_out(job)
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


@app.post("/batches/{batch_id}/generate-images", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def generate_all_images(batch_id: str, db: Session = Depends(get_db)):
    """Queue every product whose references were explicitly reviewed/saved."""
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).order_by(ProductJob.created_at.asc()).all()
    queued = 0
    for job in jobs:
        # `ready_for_image` is only reached after the operator saves the product-photo picker.
        # This prevents bulk generation from silently using SociaVault's automatic defaults.
        if job.stage != "ready_for_image" or not list(job.selected_refs or []):
            continue
        if job.image_status in {"processing", "completed"}:
            continue
        job.image_status = "pending"
        job.image_error = None
        job.stage = "queued_image"
        db.add(job)
        db.flush()
        enqueue_task(db, "generate_image", job_id=job.id, batch_id=job.batch_id, priority=20, max_attempts=2)
        queued += 1
    if not queued:
        raise HTTPException(400, "No reviewed products are waiting for image generation. Save product photos first.")
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


@app.post("/batches/{batch_id}/generate-videos", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def generate_all_videos(batch_id: str, db: Session = Depends(get_db)):
    """Approve and queue video generation for every completed image that has not started video yet."""
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    jobs = db.query(ProductJob).filter(ProductJob.batch_id == batch.id).order_by(ProductJob.created_at.asc()).all()
    queued = 0
    for job in jobs:
        if job.image_status != "completed":
            continue
        if job.video_status not in {"pending", ""} or job.upscale_status == "completed":
            continue
        # Clicking Generate all videos is the operator's batch-level approval action.
        job.approved = True
        job.video_error = None
        job.stage = "ready_for_video"
        db.add(job)
        db.flush()
        enqueue_task(db, "submit_video", job_id=job.id, batch_id=job.batch_id, priority=30, max_attempts=2)
        queued += 1
    if not queued:
        raise HTTPException(400, "No completed images are waiting for video generation.")
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)


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
