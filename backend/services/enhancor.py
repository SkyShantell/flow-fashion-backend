from __future__ import annotations

import requests

from backend.config import settings


def _headers():
    cfg = settings()
    return {
        "x-api-key": cfg.enhancor_api_key,
        "Content-Type": "application/json",
    }


def submit_seedance_video(
    prompt: str,
    product_images: list[str],
    start_video: str,
    webhook_url: str = "",
    duration: str = "5",
) -> dict:
    cfg = settings()
    if not cfg.enhancor_api_key:
        raise RuntimeError("Missing ENHANCOR_API_KEY")
    if not start_video:
        raise RuntimeError("SEEDANCE_BLACK_VIDEO_URL is required for every Shoe Showcase video.")
    if not product_images or not product_images[0]:
        raise RuntimeError("The approved Flow opener is required for Seedance.")

    body = {
        "type": "image-to-video",
        "mode": "ugc",
        "prompt": prompt,
        "duration": duration,
        "resolution": "1080p",
        "aspect_ratio": "9:16",
        "products": product_images,
        "fast_mode": False,
        "videos": [start_video],
    }
    if webhook_url:
        body["webhook_url"] = webhook_url

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
