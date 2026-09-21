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
    start_video: str = "",
    webhook_url: str = "",
    duration: str = "5",
) -> dict:
    cfg = settings()
    if not cfg.enhancor_api_key:
        raise RuntimeError("Missing ENHANCOR_API_KEY")

    body = {
        "type": "image-to-video",
        "mode": "ugc",
        "prompt": prompt,
        "duration": duration,
        "resolution": "1080p",
        "aspect_ratio": "9:16",
        "webhook_url": webhook_url,
        "products": product_images,
        "fast_mode": False,
    }

    if start_video:
        body["videos"] = [start_video]

    resp = requests.post(
        f"{cfg.enhancor_base}/queue",
        headers=_headers(),
        json=body,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Enhancor queue failed: HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    request_id = data.get("requestId")
    if not request_id:
        raise RuntimeError("Enhancor returned no requestId")

    return {"job_id": str(request_id), "status": "queued"}


def get_seedance_status(request_id: str) -> dict:
    cfg = settings()
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

    return {"status": "processing"}
