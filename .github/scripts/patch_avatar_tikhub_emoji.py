from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"Expected block not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Saved avatars: persist only external Flow asset IDs for new/migrated avatars.
replace_once(
    "backend/models.py",
    '    image_b64 = Column(Text, nullable=False)\n    image_mime = Column(String(80), default="image/jpeg")\n',
    '    # Legacy fallback only. New avatars are stored as external Flow assets.\n    image_b64 = Column(Text, nullable=False)\n    image_mime = Column(String(80), default="image/jpeg")\n    media_id = Column(String(500), nullable=True)\n    source_email = Column(String(320), nullable=True)\n',
)

replace_once(
    "backend/db.py",
    '        statements = [\n            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS mode VARCHAR(80) DEFAULT \'fashion_tryon\'",\n',
    '        statements = [\n            "ALTER TABLE saved_avatars ADD COLUMN IF NOT EXISTS media_id VARCHAR(500)",\n            "ALTER TABLE saved_avatars ADD COLUMN IF NOT EXISTS source_email VARCHAR(320)",\n            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS mode VARCHAR(80) DEFAULT \'fashion_tryon\'",\n',
)
replace_once(
    "backend/db.py",
    '    additions = {\n        "batches": {\n',
    '    additions = {\n        "saved_avatars": {\n            "media_id": "VARCHAR(500)",\n            "source_email": "VARCHAR(320)",\n        },\n        "batches": {\n',
)

replace_once(
    "backend/schemas.py",
    'class AvatarOut(BaseModel):\n    id: str\n    name: str\n    image_b64: str\n    image_mime: str\n',
    'class AvatarOut(BaseModel):\n    id: str\n    name: str\n    image_b64: str = ""  # legacy compatibility; normal reads use image_url\n    image_mime: str\n    image_url: str | None = None\n',
)
replace_once(
    "backend/schemas.py",
    '    avatar_b64: str | None = None\n    avatar_mime: str = "image/jpeg"\n',
    '    avatar_id: str | None = None\n    avatar_b64: str | None = None\n    avatar_mime: str = "image/jpeg"\n',
)

replace_once(
    "backend/api.py",
    'from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File, Form\n',
    'from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File, Form, Response\n',
)
replace_once(
    "backend/api.py",
    'from backend.services import sheets, useapi\n',
    'from backend.services import sheets, sociavault, useapi\n',
)

old_avatar_api = '''@app.get("/avatars", response_model=list[AvatarOut], dependencies=[Depends(require_api_key)])
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
'''
new_avatar_api = '''def _avatar_out(row: SavedAvatar) -> AvatarOut:
    return AvatarOut(
        id=row.id,
        name=row.name,
        image_b64="",
        image_mime=row.image_mime or "image/jpeg",
        image_url=f"/avatars/{row.id}/image",
    )


def _externalize_saved_avatar(db: Session, row: SavedAvatar, preferred_email: str = "") -> tuple[str, str]:
    media_id = str(row.media_id or "").strip()
    source_email = str(row.source_email or "").strip()
    if media_id:
        return media_id, source_email
    image_b64 = str(row.image_b64 or "").strip()
    if not image_b64:
        raise HTTPException(409, "Saved avatar has no recoverable image data")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception:
        raise HTTPException(409, "Saved avatar image data is invalid")
    try:
        uploaded = useapi.upload_asset(raw, row.image_mime or "image/jpeg", preferred_email)
    except Exception as exc:
        raise HTTPException(502, f"Could not store avatar externally: {exc}")
    row.media_id = str(uploaded.get("media_id") or "").strip() or None
    row.source_email = str(uploaded.get("email") or preferred_email or "").strip() or None
    row.image_mime = "image/jpeg"
    row.image_b64 = ""
    db.add(row)
    db.flush()
    if not row.media_id:
        raise HTTPException(502, "Avatar storage returned no media ID")
    return str(row.media_id), str(row.source_email or "")


@app.get("/avatars", response_model=list[AvatarOut], dependencies=[Depends(require_api_key)])
def list_saved_avatars(db: Session = Depends(get_db)):
    rows = (
        db.query(SavedAvatar)
        .options(load_only(SavedAvatar.id, SavedAvatar.name, SavedAvatar.image_mime, SavedAvatar.media_id, SavedAvatar.source_email, SavedAvatar.created_at))
        .order_by(SavedAvatar.created_at.desc())
        .all()
    )
    return [_avatar_out(row) for row in rows]


@app.get("/avatars/{avatar_id}/image", dependencies=[Depends(require_api_key)])
def saved_avatar_image(avatar_id: str, db: Session = Depends(get_db)):
    row = db.get(SavedAvatar, avatar_id)
    if not row:
        raise HTTPException(404, "Avatar not found")

    media_id = str(row.media_id or "").strip()
    if media_id:
        raw, error = useapi.download_raw_asset(media_id)
        if raw:
            return Response(content=raw, media_type=row.image_mime or "image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
        if not row.image_b64:
            raise HTTPException(502, error or "Stored avatar is temporarily unavailable")

    image_b64 = str(row.image_b64 or "").strip()
    if not image_b64:
        raise HTTPException(404, "Avatar image is unavailable")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception:
        raise HTTPException(409, "Saved avatar image data is invalid")

    # Lazy migration: viewing an old avatar moves it out of Postgres once, without a bulk memory spike.
    try:
        uploaded = useapi.upload_asset(raw, row.image_mime or "image/jpeg", "")
        row.media_id = str(uploaded.get("media_id") or "").strip() or None
        row.source_email = str(uploaded.get("email") or "").strip() or None
        if row.media_id:
            row.image_b64 = ""
            row.image_mime = "image/jpeg"
            db.add(row)
            db.commit()
    except Exception:
        # Preview still works from the legacy bytes; a later request can retry migration.
        pass
    return Response(content=raw, media_type=row.image_mime or "image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/avatars", response_model=AvatarOut, dependencies=[Depends(require_api_key)])
def save_avatar(req: SaveAvatarRequest, db: Session = Depends(get_db)):
    name = str(req.name or "Saved avatar").strip()[:160] or "Saved avatar"
    image_b64 = str(req.image_b64 or "").strip()
    if not image_b64:
        raise HTTPException(400, "Avatar image is required")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Avatar image is not valid base64")
    try:
        uploaded = useapi.upload_asset(raw, req.image_mime or "image/jpeg", "")
    except Exception as exc:
        raise HTTPException(502, f"Could not store avatar externally: {exc}")
    row = SavedAvatar(
        name=name,
        image_b64="",
        image_mime="image/jpeg",
        media_id=str(uploaded.get("media_id") or "").strip() or None,
        source_email=str(uploaded.get("email") or "").strip() or None,
    )
    if not row.media_id:
        raise HTTPException(502, "Avatar storage returned no media ID")
    db.add(row)
    db.commit()
    db.refresh(row)
    return _avatar_out(row)
'''
replace_once("backend/api.py", old_avatar_api, new_avatar_api)

old_create = '''@app.post("/batches", response_model=BatchOut, dependencies=[Depends(require_api_key)])
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
        avatar_name=(str(req.avatar_name or "").strip()[:160] or None),
        flow_account_email=(useapi.normalize_account_email(req.flow_account_email) or None),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)
'''
new_create = '''@app.post("/batches", response_model=BatchOut, dependencies=[Depends(require_api_key)])
def create_batch(req: CreateBatchRequest, db: Session = Depends(get_db)):
    mode = "shoe_showcase" if str(req.mode or "").strip().lower() == "shoe_showcase" else "fashion_tryon"
    avatar_b64 = None
    avatar_media_id = None
    avatar_source_email = None
    avatar_mime = req.avatar_mime or "image/jpeg"
    avatar_name = str(req.avatar_name or "").strip()[:160] or None
    preferred_email = useapi.normalize_account_email(req.flow_account_email)

    if mode == "shoe_showcase":
        requested_scenes = [SHOE_SHOWCASE_SCENE]
        requested_motions = [SHOE_SHOWCASE_MOTION]
        creator_profile = "Male" if str(req.creator_profile or "").lower().startswith("m") else "Female"
    else:
        requested_scenes = [x for x in req.scene_pool if x in SCENES and x != SHOE_SHOWCASE_SCENE] or ([req.scene] if req.scene in SCENES and req.scene != SHOE_SHOWCASE_SCENE else ["Modern apartment mirror"])
        default_motion = default_motion_style(req.creator_profile)
        requested_motions = [x for x in req.motion_pool if x in MOTION_STYLES and x != SHOE_SHOWCASE_MOTION] or ([req.video_style] if req.video_style in MOTION_STYLES and req.video_style != SHOE_SHOWCASE_MOTION else [default_motion])
        creator_profile = req.creator_profile
        avatar_b64 = req.avatar_b64
        if req.avatar_id:
            saved = db.get(SavedAvatar, str(req.avatar_id))
            if not saved:
                raise HTTPException(404, "Saved avatar not found")
            avatar_media_id, avatar_source_email = _externalize_saved_avatar(db, saved, preferred_email)
            avatar_b64 = None
            avatar_mime = saved.image_mime or "image/jpeg"
            avatar_name = saved.name
        if not avatar_b64 and not avatar_media_id:
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
        avatar_mime=avatar_mime,
        avatar_media_id=avatar_media_id,
        avatar_source_email=avatar_source_email,
        avatar_name=avatar_name,
        flow_account_email=(preferred_email or None),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_out(batch, db)
'''
replace_once("backend/api.py", old_create, new_create)

# Scanner rows keep their already-known product cover so TikHub failures have a local fallback.
replace_once(
    "backend/api.py",
    '        assigned_scene, assigned_motion = _assign_defaults(batch, next_index)\n        next_index += 1\n        job = ProductJob(\n            batch_id=batch.id,\n            product_url=link,\n',
    '        assigned_scene, assigned_motion = _assign_defaults(batch, next_index)\n        next_index += 1\n        scanner_image = sociavault.normalize_remote_url(rec.get("Product Image"))\n        job = ProductJob(\n            batch_id=batch.id,\n            product_url=link,\n',
)
replace_once(
    "backend/api.py",
    '            product_name=str(rec.get("Product Name") or "Unknown Product"),\n            stage="pending_import",\n',
    '            product_name=str(rec.get("Product Name") or "Unknown Product"),\n            listing_images=[scanner_image] if scanner_image else [],\n            selected_refs=[scanner_image] if scanner_image else [],\n            stage="pending_import",\n',
)

old_upload_avatar = '''def _upload_avatar_if_needed(db: Session, batch: Batch) -> str:
    selected_account = _batch_flow_account(batch)
    # Automatic can reuse any existing asset. A specific account must own the asset.
    if batch.avatar_media_id and (not selected_account or _same_account(batch.avatar_source_email, selected_account)):
        return batch.avatar_media_id
    if not batch.avatar_b64:
        raise RuntimeError("Batch has no avatar image. Add an avatar before generating.")
    raw = base64.b64decode(batch.avatar_b64)
    uploaded = useapi.upload_asset(raw, batch.avatar_mime or "image/jpeg", selected_account)
    batch.avatar_media_id = str(uploaded.get("media_id") or "")
    batch.avatar_source_email = str(uploaded.get("email") or selected_account or "").strip() or None
    db.add(batch)
    db.flush()
    return batch.avatar_media_id
'''
new_upload_avatar = '''def _upload_avatar_if_needed(db: Session, batch: Batch) -> str:
    selected_account = _batch_flow_account(batch)
    # Automatic can reuse any existing external asset. A pinned account must own the asset.
    if batch.avatar_media_id:
        if not selected_account or _same_account(batch.avatar_source_email, selected_account):
            return batch.avatar_media_id
        raw, error = useapi.download_raw_asset(str(batch.avatar_media_id))
        if not raw:
            raise RuntimeError(error or "Could not recover the externally stored avatar for account migration.")
        uploaded = useapi.upload_asset(raw, batch.avatar_mime or "image/jpeg", selected_account)
        batch.avatar_media_id = str(uploaded.get("media_id") or "")
        batch.avatar_source_email = str(uploaded.get("email") or selected_account or "").strip() or None
        db.add(batch)
        db.flush()
        return batch.avatar_media_id

    # Legacy batch fallback. Once uploaded, clear the duplicate base64 from this batch row.
    if not batch.avatar_b64:
        raise RuntimeError("Batch has no avatar image. Add an avatar before generating.")
    raw = base64.b64decode(batch.avatar_b64)
    uploaded = useapi.upload_asset(raw, batch.avatar_mime or "image/jpeg", selected_account)
    batch.avatar_media_id = str(uploaded.get("media_id") or "")
    batch.avatar_source_email = str(uploaded.get("email") or selected_account or "").strip() or None
    batch.avatar_b64 = None
    db.add(batch)
    db.flush()
    return batch.avatar_media_id
'''
replace_once("backend/tasks.py", old_upload_avatar, new_upload_avatar)

old_gb_import = '''    if region == "GB":
        data = tikhub.import_product(job.product_url, region="GB")
    else:
        data = sociavault.import_product(job.product_url, region=region)
'''
new_gb_import = '''    if region == "GB":
        try:
            data = tikhub.import_product(job.product_url, region="GB")
        except Exception as tikhub_exc:
            # Momentum/Scanner already captured a name + cover. Use that as the final UK fallback
            # instead of failing an otherwise usable product when TikHub rejects this specific ID.
            scanner_name = str(job.product_name or "Unknown Product").strip() or "Unknown Product"
            scanner_images = [str(x) for x in _as_list(job.listing_images) if str(x).strip()]
            if job.scanner_row_num and not scanner_images:
                scanner_rec, _scanner_error = sheets.scanner_row(int(job.scanner_row_num))
                if scanner_rec:
                    scanner_name = str(scanner_rec.get("Product Name") or scanner_name).strip() or scanner_name
                    scanner_image = sociavault.normalize_remote_url(scanner_rec.get("Product Image"))
                    if scanner_image:
                        scanner_images = [scanner_image]
            if not scanner_images:
                raise RuntimeError(
                    "TikHub could not resolve this UK product and the Scanner fallback has no product image. "
                    + str(tikhub_exc)[:600]
                )
            try:
                product_id = tikhub.extract_product_id(job.product_url)
            except Exception:
                product_id = hashlib.sha1(str(job.product_url).encode("utf-8")).hexdigest()[:20]
            data = {
                "product_id": product_id,
                "product_name": scanner_name,
                "sociavault_region": "GB",
                "listing_images": scanner_images[:18],
                "review_images": [],
                "selected_refs": scanner_images[: settings().max_product_refs],
                "focus": sociavault.classify_focus(scanner_name),
                "provider": "scanner_fallback",
            }
    else:
        data = sociavault.import_product(job.product_url, region=region)
'''
replace_once("backend/tasks.py", old_gb_import, new_gb_import)

# Let TikHub reviews rescue a product even when both detail endpoints are flaky.
replace_once("backend/services/tikhub.py", 'import re\nimport time\n', 'import logging\nimport re\nimport time\n')
replace_once(
    "backend/services/tikhub.py",
    'REVIEWS_V2 = f"{TIKHUB_BASE}/fetch_product_reviews_v2"\n',
    'REVIEWS_V2 = f"{TIKHUB_BASE}/fetch_product_reviews_v2"\n\nlog = logging.getLogger("flow-tikhub")\n',
)
remove_early_raise = '''    if v3_error and not listing_images:
        # Keep the useful upstream error if both product-detail routes failed.
        if v1_error:
            raise RuntimeError(f"TikHub product detail failed on V3 and V1. V3: {v3_error} | V1: {v1_error}")
        raise RuntimeError(v3_error)

'''
replace_once("backend/services/tikhub.py", remove_early_raise, "")
replace_once(
    "backend/services/tikhub.py",
    '    review_images: list[str] = []\n    try:\n',
    '    review_images: list[str] = []\n    review_error = ""\n    try:\n',
)
replace_once(
    "backend/services/tikhub.py",
    '    except Exception:\n        review_images = []\n\n    listing_images = _merge_unique(listing_images, limit=18)\n',
    '    except Exception as exc:\n        review_error = str(exc)\n        review_images = []\n\n    listing_images = _merge_unique(listing_images, limit=18)\n',
)
old_no_images = '''    if not listing_images and not review_images:
        raise RuntimeError("TikHub returned the UK product but no usable product images.")
'''
new_no_images = '''    if not listing_images and not review_images:
        if v3_error:
            log.warning("TikHub V3 failed · product=%s · %s", product_id, v3_error[:500])
        if v1_error:
            log.warning("TikHub V1 failed · product=%s · %s", product_id, v1_error[:500])
        elif v3_error:
            log.warning("TikHub V1 returned no usable gallery · product=%s", product_id)
        if review_error:
            log.warning("TikHub reviews failed · product=%s · %s", product_id, review_error[:500])
        detail_status = []
        if v3_error:
            detail_status.append("V3 failed")
        if v1_error:
            detail_status.append("V1 failed")
        elif v3_error:
            detail_status.append("V1 had no usable gallery")
        if review_error:
            detail_status.append("reviews failed")
        else:
            detail_status.append("reviews had no usable photos")
        raise RuntimeError("TikHub UK lookup exhausted fallbacks: " + "; ".join(detail_status))
'''
replace_once("backend/services/tikhub.py", old_no_images, new_no_images)

# Server-side safety: an emoji typed at the edge of the headline is treated as an emoji field.
provider = Path("backend/provider_api.py")
text = provider.read_text()
anchor = '''def _emoji_tokens(value: str, limit: int = 8) -> list[str]:
    return [token for token in str(value or "").strip().split() if token][:limit]


'''
insert = '''def _emoji_tokens(value: str, limit: int = 8) -> list[str]:
    return [token for token in str(value or "").strip().split() if token][:limit]


def _looks_like_emoji_token(token: str) -> bool:
    for char in str(token or ""):
        code = ord(char)
        if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
            return True
    return False


def _extract_edge_emojis(headline: str, prefix: str, suffix: str) -> tuple[str, str, str]:
    parts = str(headline or "").strip().split()
    prefix = str(prefix or "").strip()
    suffix = str(suffix or "").strip()
    if len(parts) > 1 and not prefix and _looks_like_emoji_token(parts[0]):
        prefix = parts.pop(0)
    if len(parts) > 1 and not suffix and _looks_like_emoji_token(parts[-1]):
        suffix = parts.pop()
    return " ".join(parts), prefix, suffix


'''
if anchor not in text:
    raise SystemExit("provider emoji anchor not found")
text = text.replace(anchor, insert, 1)
old_apply = '''    headline = " ".join(str(request.headline or caption_for_job(job)).split()).strip()[:120]
    subheadline = " ".join(str(request.subheadline or "").split()).strip()[:120]
    if not headline and not subheadline:
        raise HTTPException(400, "Add at least one line of text")

    prefix_tokens = _emoji_tokens(request.emoji_prefix)
    suffix_tokens = _emoji_tokens(request.emoji_suffix)
'''
new_apply = '''    headline = " ".join(str(request.headline or caption_for_job(job)).split()).strip()[:120]
    subheadline = " ".join(str(request.subheadline or "").split()).strip()[:120]
    headline, emoji_prefix, emoji_suffix = _extract_edge_emojis(
        headline,
        str(request.emoji_prefix or "")[:80],
        str(request.emoji_suffix or "")[:80],
    )
    if not headline and not subheadline:
        raise HTTPException(400, "Add at least one line of text")

    prefix_tokens = _emoji_tokens(emoji_prefix)
    suffix_tokens = _emoji_tokens(emoji_suffix)
'''
if old_apply not in text:
    raise SystemExit("provider apply emoji block not found")
text = text.replace(old_apply, new_apply, 1)
old_message = '            f"Apple emoji asset not installed for: {preview}. Open Style + FFmpeg once on a Mac to add it, then the Windows VA can use it.",'
new_message = '            f"Shared Apple emoji library is missing: {preview}. Open Flow Fashion once on a Mac to sync that emoji, then Windows uses the same Apple artwork.",'
if old_message not in text:
    raise SystemExit("provider missing-emoji message not found")
text = text.replace(old_message, new_message, 1)
old_payload = '            "emoji_prefix": str(request.emoji_prefix or "")[:80],\n            "emoji_suffix": str(request.emoji_suffix or "")[:80],'
new_payload = '            "emoji_prefix": emoji_prefix,\n            "emoji_suffix": emoji_suffix,'
if old_payload not in text:
    raise SystemExit("provider emoji payload not found")
text = text.replace(old_payload, new_payload, 1)
provider.write_text(text)

replace_once(
    "backend/manual_ffmpeg.py",
    '            "fashion_text_overlay_emoji_mode": "browser_system_png" if (emoji_prefix_pngs or emoji_suffix_pngs) else "fallback",\n',
    '            "fashion_text_overlay_emoji_mode": "server_apple_cache" if (emoji_prefix_pngs or emoji_suffix_pngs) else "none",\n',
)
