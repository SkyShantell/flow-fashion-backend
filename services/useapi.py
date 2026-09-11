from __future__ import annotations

import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from PIL import Image

from backend.config import settings


def flow_headers(token: str | None = None, json_content: bool = False) -> dict:
    cfg = settings()
    headers = {"Authorization": f"Bearer {token or cfg.useapi_token}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def parse_error(resp: requests.Response) -> str:
    """Return the most useful provider error without exposing auth headers/tokens."""
    raw_text = (resp.text or "").strip()
    try:
        payload = resp.json()
    except Exception:
        return (raw_text or f"HTTP {resp.status_code}")[:2200]

    if not isinstance(payload, dict):
        return str(payload)[:2200]

    err = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or str(err)

    # useapi sometimes puts the useful Google/Flow failure details in nested
    # response/operations/media fields while the top-level error is only
    # "API error: 400". Include those fields so Railway logs reveal the cause.
    diagnostic = {}
    for key in ("code", "response", "operations", "media", "status", "jobId", "jobid"):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            diagnostic[key] = payload.get(key)

    parts = []
    if err:
        parts.append(str(err))
    if diagnostic:
        try:
            parts.append("details=" + json.dumps(diagnostic, ensure_ascii=False, default=str))
        except Exception:
            parts.append("details=" + str(diagnostic))
    if not parts:
        parts.append(str(payload))
    return " | ".join(parts)[:2200]


def request_json(method: str, url: str, *, headers=None, params=None, json_body=None, data=None, timeout=180, retries=2) -> dict:
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_body, data=data, timeout=timeout)
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            last_error = f"HTTP {resp.status_code}: {parse_error(resp)}"
            if resp.status_code in {429, 502, 503} and attempt < retries:
                time.sleep(3 + attempt * 3)
                continue
            raise RuntimeError(last_error)
        except requests.Timeout:
            last_error = "Request timed out."
            if attempt < retries:
                time.sleep(2)
                continue
            raise RuntimeError(last_error)
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(2)
                continue
            raise RuntimeError(last_error)
    raise RuntimeError(last_error or "Request failed")


def normalize_account_email(value: str | None) -> str:
    """Normalize the optional Flow account selector. Blank means automatic/load-balance."""
    return str(value or "").strip()


def _account_summary(email: str, payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    credits_block = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    session_block = payload.get("sessionData") if isinstance(payload.get("sessionData"), dict) else {}
    # Some API responses nest the actual account under response/account/data.
    for key in ("account", "response", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict) and (nested.get("health") is not None or nested.get("credits") is not None):
            payload = nested
            credits_block = payload.get("credits") if isinstance(payload.get("credits"), dict) else credits_block
            session_block = payload.get("sessionData") if isinstance(payload.get("sessionData"), dict) else session_block
            break
    credits_value = credits_block.get("credits") if credits_block else payload.get("credits")
    tier = credits_block.get("userPaygateTier") if credits_block else None
    if tier is None:
        tier = payload.get("userPaygateTier") or payload.get("paygateTier")
    return {
        "email": str(payload.get("email") or email),
        "health": str(payload.get("health") or "unknown"),
        "credits": credits_value if isinstance(credits_value, (int, float)) else None,
        "paygate_tier": str(tier or ""),
        "created": payload.get("created"),
        "session_expiry": session_block.get("expires") or payload.get("sessionExpiry") or payload.get("session_expiry"),
    }


def get_flow_account(email: str) -> dict:
    cfg = settings()
    email = normalize_account_email(email)
    if not email:
        raise RuntimeError("Flow account email is required.")
    payload = request_json(
        "GET",
        f"{cfg.flow_base}/accounts/{quote(email, safe='')}",
        headers=flow_headers(cfg.useapi_token),
        timeout=60,
        retries=1,
    )
    return _account_summary(email, payload)


def list_flow_accounts() -> list[dict]:
    """Return sanitized account status for every connected Google Flow account."""
    cfg = settings()
    if not cfg.useapi_token:
        raise RuntimeError("Missing USEAPI_TOKEN")
    payload = request_json(
        "GET",
        f"{cfg.flow_base}/accounts",
        headers=flow_headers(cfg.useapi_token),
        timeout=60,
        retries=1,
    )
    emails: list[str] = []
    if isinstance(payload, dict):
        # Documented shape is a map keyed by email. Also accept common wrapper/list shapes.
        candidate = payload.get("accounts")
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str):
                    emails.append(item)
                elif isinstance(item, dict) and item.get("email"):
                    emails.append(str(item["email"]))
        elif isinstance(candidate, dict):
            emails.extend(str(k) for k in candidate.keys())
        else:
            for key, value in payload.items():
                if "@" in str(key):
                    emails.append(str(key))
                elif isinstance(value, dict) and value.get("email"):
                    emails.append(str(value.get("email")))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                emails.append(item)
            elif isinstance(item, dict) and item.get("email"):
                emails.append(str(item["email"]))
    emails = list(dict.fromkeys(x.strip() for x in emails if str(x).strip()))
    if not emails:
        return []

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(emails))) as pool:
        futures = {pool.submit(get_flow_account, email): email for email in emails}
        for future in as_completed(futures):
            email = futures[future]
            try:
                results[email] = future.result()
            except Exception as exc:
                # Keep the account visible even if one detail call temporarily fails.
                results[email] = {
                    "email": email,
                    "health": "error",
                    "credits": None,
                    "paygate_tier": "",
                    "created": None,
                    "session_expiry": None,
                    "error": str(exc)[:500],
                }
    return [results[email] for email in emails]


def normalize_image_bytes(data: bytes, mime: str = "image/jpeg", max_side: int = 1800, quality: int = 92) -> tuple[bytes, str]:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            bg = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                bg.paste(image, mask=image.getchannel("A"))
            else:
                bg.paste(image.convert("RGB"))
            image = bg
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        if mime in {"image/jpeg", "image/png", "image/webp"}:
            return data, mime
        return data, "image/jpeg"


def upload_asset(image_bytes: bytes, mime: str, email: str = "") -> dict:
    """Upload an image asset. Blank email uses UseAPI automatic/load-balanced routing."""
    cfg = settings()
    if not cfg.useapi_token:
        raise RuntimeError("Missing USEAPI_TOKEN")
    image_bytes, mime = normalize_image_bytes(image_bytes, mime)
    selected_email = normalize_account_email(email)
    url = f"{cfg.flow_base}/assets"
    if selected_email:
        url += "/" + quote(selected_email, safe="")
    resp = requests.post(url, headers={**flow_headers(cfg.useapi_token), "Content-Type": mime}, data=image_bytes, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"Flow asset upload failed — HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    media = payload.get("mediaGenerationId")
    if isinstance(media, dict):
        media = media.get("mediaGenerationId")
    if not media:
        raise RuntimeError("Flow uploaded the asset but returned no mediaGenerationId.")
    used_email = str(payload.get("email") or selected_email or "").strip()
    return {"media_id": str(media), "email": used_email}


def upload_video_asset(video_bytes: bytes, email: str = "") -> dict:
    """Upload an already-rendered MP4; blank email lets UseAPI choose the account."""
    cfg = settings()
    if not cfg.useapi_token:
        raise RuntimeError("Missing USEAPI_TOKEN")
    if not video_bytes:
        raise RuntimeError("No video bytes to upload")
    selected_email = normalize_account_email(email)
    url = f"{cfg.flow_base}/assets"
    if selected_email:
        url += "/" + quote(selected_email, safe="")
    resp = requests.post(
        url,
        headers={**flow_headers(cfg.useapi_token), "Content-Type": "video/mp4"},
        data=video_bytes,
        timeout=240,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Flow video asset upload failed — HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    media = payload.get("mediaGenerationId")
    if isinstance(media, dict):
        media = media.get("mediaGenerationId")
    if not media:
        raise RuntimeError("Flow uploaded the stitched MP4 but returned no mediaGenerationId.")
    used_email = str(payload.get("email") or selected_email or "").strip()
    return {"media_id": str(media), "email": used_email}

def generate_image(prompt: str, refs: list[str], email: str = "") -> dict:
    cfg = settings()
    selected_email = normalize_account_email(email)
    body = {
        "model": cfg.image_model,
        "prompt": prompt,
        "aspectRatio": "9:16",
        "count": 1,
    }
    if selected_email:
        body["email"] = selected_email
    for i, ref in enumerate(refs[:10], start=1):
        body[f"reference_{i}"] = ref
    payload = request_json("POST", f"{cfg.flow_base}/images", headers=flow_headers(cfg.useapi_token, True), json_body=body, timeout=180, retries=1)
    media = payload.get("media") or []
    if not media:
        raise RuntimeError(f"{cfg.image_model} returned no image media.")
    generated = (((media[0] or {}).get("image") or {}).get("generatedImage") or {})
    media_id = generated.get("mediaGenerationId") or (media[0] or {}).get("mediaGenerationId")
    if not media_id:
        raise RuntimeError(f"{cfg.image_model} returned no generated image mediaGenerationId.")
    return {
        "job_id": payload.get("jobId") or payload.get("jobid"),
        "media_id": str(media_id),
        "url": generated.get("fifeUrl") or generated.get("url"),
        "encoded": generated.get("encodedImage"),
        "seed": generated.get("seed"),
        "email": str(payload.get("email") or selected_email or "").strip(),
    }


def submit_video(image_media_id: str, prompt: str, email: str = "", duration: int | None = None) -> dict:
    cfg = settings()
    selected_email = normalize_account_email(email)
    body = {
        "model": cfg.video_model,
        "prompt": prompt,
        "aspectRatio": "portrait",
        "duration": int(duration or cfg.video_duration),
        "resolution": cfg.video_native_resolution,
        "count": 1,
        "startImage": image_media_id,
        "async": True,
    }
    if selected_email:
        body["email"] = selected_email
    payload = request_json("POST", f"{cfg.flow_base}/videos", headers=flow_headers(cfg.useapi_token, True), json_body=body, timeout=90, retries=1)
    job_id = payload.get("jobid") or payload.get("jobId")
    if not job_id:
        raise RuntimeError("Omni submitted without returning a job ID.")
    return {"job_id": str(job_id), "status": payload.get("status") or "created", "email": str(payload.get("email") or selected_email or "").strip()}


def submit_upscale(media_generation_id: str, resolution: str | None = None) -> dict:
    cfg = settings()
    body = {
        "mediaGenerationId": media_generation_id,
        "resolution": resolution or cfg.video_final_resolution,
        "async": True,
    }
    payload = request_json("POST", f"{cfg.flow_base}/videos/upscale", headers=flow_headers(cfg.useapi_token, True), json_body=body, timeout=90, retries=1)
    job_id = payload.get("jobid") or payload.get("jobId")
    if not job_id:
        # The endpoint can be synchronous if async is ignored, so also accept direct media.
        media_id, video_url, thumb = media_from_job_response(payload)
        if media_id or video_url:
            return {"job_id": "", "status": "completed", "media_id": media_id, "url": video_url, "thumbnail_url": thumb}
        raise RuntimeError("Video upscale submitted without returning a job ID or media.")
    return {"job_id": str(job_id), "status": payload.get("status") or "created"}


def get_job(job_id: str) -> dict:
    cfg = settings()
    jid = str(job_id or "").strip().strip("\"'")
    if not jid:
        raise RuntimeError("Missing Flow job ID.")
    safe_jid = quote(jid, safe=":@+-._")
    return request_json("GET", f"{cfg.flow_base}/jobs/{safe_jid}", headers=flow_headers(cfg.useapi_token), timeout=60, retries=1)


def media_from_job_response(payload: dict) -> tuple[str, str, str]:
    response = payload.get("response") or payload
    media = response.get("media") or []
    if not media:
        return "", "", ""
    item = media[0] or {}
    return (
        str(item.get("mediaGenerationId") or ""),
        str(item.get("videoUrl") or item.get("url") or ""),
        str(item.get("thumbnailUrl") or ""),
    )


def parse_video_job(payload: dict) -> dict:
    status = str(payload.get("status") or "unknown").lower()
    result = {"status": status}
    if status == "failed":
        result["error"] = str(payload.get("error") or (payload.get("response") or {}).get("error") or "Video generation failed.")
        return result
    media_id, video_url, thumb = media_from_job_response(payload)
    if media_id:
        result["video_media_id"] = media_id
    if video_url:
        result["video_url"] = video_url
    if thumb:
        result["thumbnail_url"] = thumb
    return result


def resolve_asset_url(media_id: str) -> str:
    cfg = settings()
    if not media_id:
        return ""
    try:
        payload = request_json("GET", f"{cfg.flow_base}/assets/{quote(media_id, safe='')}", headers=flow_headers(cfg.useapi_token), timeout=60, retries=0)
        return str(payload.get("url") or "")
    except Exception:
        return ""


def download_url(url: str, timeout: int = 120) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content, (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0]


def download_raw_asset(media_id: str) -> tuple[bytes | None, str]:
    cfg = settings()
    if not media_id:
        return None, "No media ID is available."
    try:
        resp = requests.get(
            f"{cfg.flow_base}/assets/{quote(media_id, safe='')}",
            params={"raw": "true"},
            headers=flow_headers(cfg.useapi_token),
            timeout=240,
        )
        if resp.status_code == 200 and resp.content:
            return resp.content, ""
        if resp.status_code == 503:
            wait = resp.headers.get("Retry-After") or "a few"
            return None, f"Google is still preparing this file. Try again in {wait} seconds."
        try:
            detail = resp.json().get("error")
        except Exception:
            detail = resp.text[:300]
        return None, detail or f"Raw asset fetch failed (HTTP {resp.status_code})."
    except Exception as exc:
        return None, f"Raw asset fetch failed: {exc}"


def image_bytes_from_result(result: dict) -> bytes | None:
    if result.get("encoded"):
        try:
            return base64.b64decode(result["encoded"])
        except Exception:
            pass
    if result.get("url"):
        try:
            return download_url(result["url"], 120)[0]
        except Exception:
            pass
    return None
