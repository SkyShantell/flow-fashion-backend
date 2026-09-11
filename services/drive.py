from __future__ import annotations

import base64
import hashlib
import re

import requests

from backend.config import settings


def safe_name(text: str, fallback: str = "product") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return (text[:80] or fallback)


def archive_bytes(data: bytes, mime_type: str, filename: str, kind: str, *, batch_name: str, product_name: str, batch_date: str, description: str = "") -> tuple[dict | None, str]:
    cfg = settings()
    if not cfg.google_drive_archive_webhook_url or not cfg.google_drive_archive_secret:
        return None, "Google Drive archive is not configured."
    if len(data) > 32 * 1024 * 1024:
        return None, f"{filename} is larger than 32 MB; use local download for this file."
    body = {
        "secret": cfg.google_drive_archive_secret,
        "filename": filename,
        "mime_type": mime_type,
        "kind": kind,
        "batch_name": batch_name,
        "product_name": product_name,
        "batch_date": batch_date,
        "description": description,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    try:
        resp = requests.post(cfg.google_drive_archive_webhook_url, json=body, timeout=240, allow_redirects=True)
        if resp.status_code >= 400:
            return None, f"Drive archive HTTP {resp.status_code}: {resp.text[:300]}"
        payload = resp.json()
        if not payload.get("ok"):
            return None, str(payload.get("error") or "Google Drive archive rejected the upload.")
        return payload, ""
    except Exception as exc:
        return None, f"Google Drive archive failed: {exc}"


def media_filename(index: int, product_name: str, media_id: str, kind: str, resolution: str = "") -> str:
    media_tag = safe_name(media_id, "media")[-18:]
    ext = "jpg" if kind == "image" else "mp4"
    res = f"_{safe_name(resolution)}" if resolution else ""
    return f"{index:02d}_{safe_name(product_name)}_{media_tag}{res}.{ext}"


def reference_filename(index: int, ref_idx: int, ref_url: str) -> str:
    tag = hashlib.sha1(str(ref_url).encode("utf-8")).hexdigest()[:10]
    return f"{index:02d}_ref_{ref_idx:02d}_{tag}.jpg"
