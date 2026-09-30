from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from urllib.parse import quote

import requests

from backend.config import settings


log = logging.getLogger("flow-drive")
_ARCHIVE_SLOTS = threading.BoundedSemaphore(2)
_TRANSIENT_HTTP_STATUSES = {404, 408, 409, 425, 429, 500, 502, 503, 504}


def _usable_payload(payload: dict) -> dict:
    """Fill in stable Drive URLs when Apps Script returns only a file ID."""
    out = dict(payload or {})
    file_id = str(out.get("file_id") or out.get("fileId") or out.get("id") or "").strip()
    if not file_id:
        return out
    encoded = quote(file_id, safe="")
    out["file_id"] = file_id
    out["view_url"] = str(out.get("view_url") or out.get("viewUrl") or f"https://drive.google.com/file/d/{encoded}/view").strip()
    out["download_url"] = str(out.get("download_url") or out.get("downloadUrl") or f"https://drive.google.com/uc?export=download&id={encoded}").strip()
    return out


def safe_name(text: str, fallback: str = "product") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return (text[:80] or fallback)


def archive_bytes(data: bytes, mime_type: str, filename: str, kind: str, *, batch_name: str, product_name: str, batch_date: str, description: str = "", attempts: int = 4) -> tuple[dict | None, str]:
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
    attempts = max(1, min(4, int(attempts or 1)))
    delays = (2, 5, 12)
    last_error = "Google Drive archive failed."
    for attempt in range(1, attempts + 1):
        retryable = True
        try:
            # Apps Script becomes unreliable when several large base64 uploads arrive at
            # once. Two slots preserve throughput without creating an upload stampede.
            with _ARCHIVE_SLOTS:
                resp = requests.post(cfg.google_drive_archive_webhook_url, json=body, timeout=240, allow_redirects=True)
            if resp.status_code >= 400:
                last_error = f"Drive archive HTTP {resp.status_code}: {resp.text[:300]}"
                retryable = resp.status_code in _TRANSIENT_HTTP_STATUSES
            else:
                try:
                    payload = resp.json()
                except Exception:
                    payload = None
                    last_error = f"Drive archive returned a non-JSON response: {resp.text[:300]}"
                if payload is not None:
                    if not payload.get("ok"):
                        last_error = str(payload.get("error") or "Google Drive archive rejected the upload.")
                        retryable = any(token in last_error.lower() for token in ("timeout", "tempor", "quota", "rate", "try again"))
                    else:
                        payload = _usable_payload(payload)
                        if payload.get("file_id") and payload.get("download_url"):
                            return payload, ""
                        last_error = "Drive saved the file but returned no file ID."
            if not retryable or attempt >= attempts:
                break
        except Exception as exc:
            last_error = f"Google Drive archive failed: {exc}"
            if attempt >= attempts:
                break
        delay = delays[min(attempt - 1, len(delays) - 1)]
        log.warning("Drive archive retry · file=%s · attempt=%s/%s · retry_in=%ss · error=%s", filename, attempt, attempts, delay, last_error[:500])
        time.sleep(delay)
    return None, last_error


def media_filename(index: int, product_name: str, media_id: str, kind: str, resolution: str = "") -> str:
    media_tag = safe_name(media_id, "media")[-18:]
    ext = "jpg" if kind == "image" else "mp4"
    res = f"_{safe_name(resolution)}" if resolution else ""
    return f"{index:02d}_{safe_name(product_name)}_{media_tag}{res}.{ext}"


def reference_filename(index: int, ref_idx: int, ref_url: str) -> str:
    tag = hashlib.sha1(str(ref_url).encode("utf-8")).hexdigest()[:10]
    return f"{index:02d}_ref_{ref_idx:02d}_{tag}.jpg"
