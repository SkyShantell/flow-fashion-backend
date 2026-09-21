from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote

import requests

from backend.config import settings


def _headers():
    cfg = settings()
    return {
        "x-api-key": cfg.enhancor_api_key,
        "Content-Type": "application/json",
    }


def signed_token(purpose: str) -> str:
    secret = settings().enhancor_callback_secret
    if not secret:
        raise RuntimeError("Missing ENHANCOR_CALLBACK_SECRET")
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def reference_url(job_id: str, index: int) -> str:
    base = settings().enhancor_public_base_url
    if not base:
        raise RuntimeError("Missing ENHANCOR_PUBLIC_BASE_URL")
    token = signed_token(f"image:{job_id}:{index}")
    return f"{base}/api/enhancor/reference/{quote(job_id, safe='')}/{index}?token={token}"


def webhook_url(job_id: str) -> str:
    base = settings().enhancor_public_base_url
    if not base:
        raise RuntimeError("Missing ENHANCOR_PUBLIC_BASE_URL")
    token = signed_token(f"webhook:{job_id}")
    return f"{base}/api/enhancor/webhook/{quote(job_id, safe='')}?token={token}"


def submit_seedance_video(
    prompt: str,
    product_images: list[str],
    start_video: str,
    webhook_url: str = "",
    duration: str = "8",
) -> dict:
    cfg = settings()
    if not cfg.enhancor_api_key:
        raise RuntimeError("Missing ENHANCOR_API_KEY")
    if not start_video:
        raise RuntimeError("SEEDANCE_BLACK_VIDEO_URL is required for every Shoe Showcase video.")
    if not product_images or not product_images[0]:
        raise RuntimeError("The approved Flow opener is required for Seedance.")
    if not webhook_url:
        raise RuntimeError("Seedance requires a webhook URL.")

    body = {
        "type": "image-to-video",
        "mode": "multi_reference",
        "prompt": prompt,
        "duration": duration,
        "resolution": "720p",
        "aspect_ratio": "9:16",
        "images": product_images,
        "fast_mode": True,
        "full_access": False,
        "is_uncensored": False,
        "videos": [start_video],
        "webhook_url": webhook_url,
    }

    resp = requests.post(
        f"{cfg.enhancor_base}/queue",
        headers=_headers(),
        json=body,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Enhancor queue failed: HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if data.get("success") is not True:
        raise RuntimeError(f"Enhancor queue was not successful: {str(data.get('error') or data.get('message') or data)[:500]}")
    request_id = data.get("requestId")
    if not request_id:
        raise RuntimeError("Enhancor returned no requestId")

    return {"job_id": str(request_id), "status": "queued"}


def get_seedance_status(request_id: str) -> dict:
    cfg = settings()
    if not cfg.enhancor_api_key:
        raise RuntimeError("Missing ENHANCOR_API_KEY")
    resp = requests.post(
        f"{cfg.enhancor_base}/status",
        headers=_headers(),
        json={"request_id": request_id},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Enhancor status failed: HTTP {resp.status_code}")

    data = resp.json()
    status = str(data.get("status", "")).upper()

    if data.get("success") is False:
        raise RuntimeError(f"Enhancor status request failed: {str(data.get('error') or data.get('message') or data)[:500]}")
    if status == "COMPLETED":
        return {
            "status": "completed",
            "video_url": data.get("result"),
            "thumbnail_url": data.get("thumbnail"),
        }
    if status == "FAILED":
        return {
            "status": "failed",
            "error": data.get("error", "Seedance failed"),
        }

    if status in {"PENDING", "IN_QUEUE", "IN_PROGRESS"}:
        return {"status": "processing"}
    raise RuntimeError(f"Unexpected Enhancor status: {status or '<empty>'}")
