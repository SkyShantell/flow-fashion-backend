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


KLING_MODELS = {
    "kling-v3-0",
    "kling-v3-0-turbo",
    "kling-v2-6",
    "kling-v2-5",
    "kling-v2-1",
    "kling-v2-1-master",
    "kling-v1-6",
    "kling-v1-5",
}
KLING_MODES = {"std", "pro", "4k"}
KLING_FINAL_FAILURES = {6, 7, 9, 50, 53, 54, 58}


def normalize_video_provider(value: str | None) -> str:
    return "kling" if str(value or "").strip().lower() == "kling" else "omni"


def normalize_kling_model(value: str | None) -> str:
    model = str(value or "kling-v3-0").strip().lower()
    return model if model in KLING_MODELS else "kling-v3-0"


def normalize_kling_mode(value: str | None, model: str | None = None) -> str:
    resolved_model = normalize_kling_model(model)
    mode = str(value or "pro").strip().lower()
    if mode not in KLING_MODES:
        mode = "pro"
    if resolved_model == "kling-v2-1-master":
        return "pro"
    if resolved_model != "kling-v3-0" and mode == "4k":
        return "pro"
    if resolved_model == "kling-v3-0-turbo" and mode == "4k":
        return "pro"
    return mode


def _kling_accounts_payload() -> dict:
    cfg = settings()
    if not cfg.useapi_token:
        raise RuntimeError("Missing USEAPI_TOKEN")
    payload = request_json(
        "GET", f"{cfg.kling_base}/accounts", headers=flow_headers(cfg.useapi_token), timeout=60, retries=1
    )
    if not isinstance(payload, dict):
        return {}
    return payload


def list_kling_accounts() -> list[dict]:
    """Return non-secret Kling account metadata configured in UseAPI."""
    payload = _kling_accounts_payload()
    out: list[dict] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        email = str(value.get("email") or key or "").strip()
        if not email:
            continue
        session = value.get("session") if isinstance(value.get("session"), dict) else {}
        out.append({
            "email": email,
            "auth_mode": str(value.get("authMode") or ""),
            "max_jobs": value.get("maxJobs"),
            "session_expiry": session.get("ExpireTimeUTC") or session.get("ExpireTime"),
        })
    return out


def resolve_kling_account_email(preferred: str | None = None) -> str:
    """Return a concrete Kling account. This avoids UseAPI's multi-account email requirement."""
    requested = normalize_account_email(preferred)
    accounts = list_kling_accounts()
    emails = [str(x.get("email") or "").strip() for x in accounts if x.get("email")]
    if requested:
        if emails and requested.lower() not in {x.lower() for x in emails}:
            raise RuntimeError(f"Kling account {requested} is not configured in UseAPI.")
        return requested
    if not emails:
        raise RuntimeError("No Kling account is configured in UseAPI.")
    return emails[0]


def upload_kling_asset(image_bytes: bytes, mime: str = "image/jpeg", email: str = "") -> dict:
    cfg = settings()
    if not cfg.useapi_token:
        raise RuntimeError("Missing USEAPI_TOKEN")
    image_bytes, mime = normalize_image_bytes(image_bytes, mime)
    account = resolve_kling_account_email(email)
    resp = requests.post(
        f"{cfg.kling_base}/assets/",
        params={"email": account},
        headers={**flow_headers(cfg.useapi_token), "Content-Type": mime},
        data=image_bytes,
        timeout=180,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Kling asset upload failed — HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json() if resp.content else {}
    url = str(payload.get("url") or payload.get("resourceUrl") or "").strip()
    if not url:
        raise RuntimeError("Kling uploaded the image but returned no asset URL.")
    return {
        "url": url,
        "file_name": str(payload.get("fileName") or ""),
        "email": account,
        "status": payload.get("status"),
    }


def _normalize_kling_duration(duration: int | None, model: str) -> str:
    value = int(duration or 5)
    if model in {"kling-v3-0", "kling-v3-0-turbo"}:
        return str(max(3, min(15, value)))
    return "10" if value >= 8 else "5"


def submit_kling_video(
    image_url: str,
    prompt: str,
    *,
    email: str = "",
    duration: int | None = None,
    model: str = "kling-v3-0",
    mode: str = "pro",
) -> dict:
    cfg = settings()
    account = resolve_kling_account_email(email)
    resolved_model = normalize_kling_model(model)
    resolved_mode = normalize_kling_mode(mode, resolved_model)
    body = {
        "email": account,
        "image": str(image_url),
        "prompt": str(prompt or "")[:2500],
        "duration": _normalize_kling_duration(duration, resolved_model),
        "model_name": resolved_model,
        "mode": resolved_mode,
    }
    if resolved_model == "kling-v3-0":
        body["enable_audio"] = False
    payload = request_json(
        "POST",
        f"{cfg.kling_base}/videos/image2video-frames",
        headers=flow_headers(cfg.useapi_token, True),
        json_body=body,
        timeout=180,
        retries=1,
    )
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_id = task.get("id") or payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError("Kling submitted without returning a task ID.")
    return {
        "job_id": str(task_id),
        "status": str(task.get("status_name") or payload.get("status_name") or "submitted").lower(),
        "email": account,
        "model": resolved_model,
        "mode": resolved_mode,
    }


def get_kling_task(task_id: str, email: str = "") -> dict:
    cfg = settings()
    jid = str(task_id or "").strip()
    if not jid:
        raise RuntimeError("Missing Kling task ID.")
    account = resolve_kling_account_email(email)
    return request_json(
        "GET",
        f"{cfg.kling_base}/tasks/{quote(jid, safe='')}",
        headers=flow_headers(cfg.useapi_token),
        params={"email": account},
        timeout=60,
        retries=1,
    )


def parse_kling_task(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    raw_status = payload.get("status")
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    if raw_status is None:
        raw_status = task.get("status")
    try:
        code = int(raw_status)
    except Exception:
        code = -1
    status_name = str(payload.get("status_name") or task.get("status_name") or "").lower()
    final = bool(payload.get("status_final") if payload.get("status_final") is not None else task.get("status_final"))
    result = {"status": "processing", "status_code": code, "status_name": status_name}
    if code == 99 or status_name in {"succeed", "success", "completed"}:
        result["status"] = "completed"
    elif code in KLING_FINAL_FAILURES or (final and status_name in {"failed", "error"}):
        result["status"] = "failed"
        result["error"] = str(payload.get("error") or payload.get("message") or task.get("message") or f"Kling task failed ({code}).")
        return result

    works = payload.get("works") if isinstance(payload.get("works"), list) else []
    for work in works:
        if not isinstance(work, dict):
            continue
        if str(work.get("contentType") or "").lower() not in {"", "video"}:
            continue
        resource = work.get("resource") if isinstance(work.get("resource"), dict) else {}
        cover = work.get("cover") if isinstance(work.get("cover"), dict) else {}
        if work.get("workId") is not None:
            result["work_id"] = str(work.get("workId"))
        if resource.get("resource"):
            result["video_url"] = str(resource.get("resource"))
        if cover.get("resource"):
            result["thumbnail_url"] = str(cover.get("resource"))
        if result.get("work_id") or result.get("video_url"):
            break
    return result


def get_kling_clean_url(work_id: str, email: str = "") -> str:
    cfg = settings()
    wid = str(work_id or "").strip()
    if not wid:
        return ""
    account = resolve_kling_account_email(email)
    last_error = None
    for attempt in range(4):
        try:
            payload = request_json(
                "GET",
                f"{cfg.kling_base}/assets/download",
                headers=flow_headers(cfg.useapi_token),
                params={"email": account, "workIds": wid, "fileTypes": "MP4"},
                timeout=90,
                retries=0,
            )
            url = str(payload.get("cdnUrl") or "").strip()
            if url:
                return url
            last_error = str(payload.get("error") or "Kling returned no clean download URL.")
        except Exception as exc:
            last_error = str(exc)
        if attempt < 3:
            time.sleep(3 + attempt * 2)
    if last_error:
        raise RuntimeError(last_error)
    return ""


def _account_summary(email: str, payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    credits_block = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    session_block = payload.get("sessionData") if isinstance(payload.get("sessionData"), dict) else {}
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
        if not isinstance(payload, dict):
            return ""
        # Image assets can come back in a few shapes depending on how Flow created them.
        direct = payload.get("url") or payload.get("fifeUrl") or payload.get("imageUrl") or payload.get("downloadUrl")
        if direct:
            return str(direct)
        for key in ("image", "generatedImage", "media", "asset", "response"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidate = nested.get("url") or nested.get("fifeUrl") or nested.get("imageUrl") or nested.get("downloadUrl")
                if candidate:
                    return str(candidate)
                generated = nested.get("generatedImage")
                if isinstance(generated, dict):
                    candidate = generated.get("fifeUrl") or generated.get("url") or generated.get("imageUrl")
                    if candidate:
                        return str(candidate)
        return ""
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
            body = resp.json()
            detail = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("error") or str(detail)
        except Exception:
            detail = resp.text[:300]

        detail_text = str(detail or "")
        # UseAPI's raw asset route currently accepts video IDs only. For stored image
        # assets (saved avatars / generated images), resolve the image CDN URL instead.
        if resp.status_code in {400, 404, 422} and ("got 'image'" in detail_text.lower() or 'got "image"' in detail_text.lower() or "type must be 'video'" in detail_text.lower()):
            image_url = resolve_asset_url(media_id)
            if image_url:
                try:
                    data, _mime = download_url(image_url, 240)
                    if data:
                        return data, ""
                except Exception as image_exc:
                    return None, f"Stored image asset fetch failed: {image_exc}"
            return None, detail_text or "Stored image asset URL could not be resolved."

        return None, detail_text or f"Raw asset fetch failed (HTTP {resp.status_code})."
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
