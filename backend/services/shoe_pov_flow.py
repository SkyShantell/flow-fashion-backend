from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import quote

from backend.config import settings
from backend.shoe_pov_prompts import image_reference_caption


FINAL_FAILURES = {"failed", "cancelled", "canceled"}
IN_FLIGHT = {"created", "pending", "queued", "processing", "running", "in_progress"}


class ResponseLike(Protocol):
    status_code: int
    headers: dict[str, str]
    text: str
    content: bytes

    def json(self) -> Any: ...


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        timeout: int,
    ) -> ResponseLike: ...


class RequestsTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        timeout: int,
    ) -> ResponseLike:
        import requests

        try:
            return requests.request(method, url, headers=headers, json=json, data=data, timeout=timeout)
        except requests.Timeout as exc:
            raise TimeoutError("Flow request timed out.") from exc
        except requests.RequestException as exc:
            raise ConnectionError(str(exc)) from exc


@dataclass(frozen=True)
class ErrorDecision:
    retryable: bool
    wait_seconds: int
    alert_human: bool
    moderation: bool = False


class FlowPovProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        retryable: bool = False,
        wait_seconds: int = 0,
        alert_human: bool = False,
        moderation: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.wait_seconds = wait_seconds
        self.alert_human = alert_human
        self.moderation = moderation


def _is_moderation(text: str) -> bool:
    lowered = str(text or "").lower()
    return "unsafe_generation" in lowered or "moderat" in lowered


def error_decision(status_code: int, body: str = "", retry_after: str | None = None) -> ErrorDecision:
    status = int(status_code or 0)
    moderation = status in {400, 500} and _is_moderation(body)
    if moderation:
        return ErrorDecision(False, 0, False, True)
    if status == 400:
        return ErrorDecision(False, 0, False)
    if status in {401, 402, 404}:
        return ErrorDecision(False, 0, True)
    if status == 403:
        return ErrorDecision(True, 120, True)
    if status == 408:
        return ErrorDecision(True, 30, False)
    if status == 429:
        try:
            wait = max(1, int(float(str(retry_after or "60"))))
        except Exception:
            wait = 60
        return ErrorDecision(True, wait, False)
    if status == 500:
        return ErrorDecision(True, 30, False)
    if status == 503:
        return ErrorDecision(True, 60, False)
    if status == 596:
        return ErrorDecision(True, 300, True)
    if status >= 500 or status == 0:
        return ErrorDecision(True, 60, False)
    return ErrorDecision(False, 0, False)


def _payload_text(response: ResponseLike) -> str:
    try:
        payload = response.json()
    except Exception:
        return str(response.text or "")[:2200]
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("message") or payload.get("detail")
        if isinstance(error, dict):
            error = error.get("message") or error.get("error") or str(error)
        if error:
            return str(error)[:2200]
    return str(payload)[:2200]


def _status(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    value = payload.get("status") or response.get("status") or ""
    return str(value).strip().lower()


def _operation_failed(payload: dict[str, Any]) -> bool:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    operations = response.get("operations") if isinstance(response, dict) else None
    if not isinstance(operations, list):
        return False
    return any("failed" in str(item.get("status") if isinstance(item, dict) else item).lower() for item in operations)


def _job_id(payload: dict[str, Any]) -> str:
    return str(payload.get("jobId") or payload.get("jobid") or payload.get("id") or "").strip()


def _media_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [payload]
    if isinstance(payload.get("response"), dict):
        containers.insert(0, payload["response"])
    for container in containers:
        media = container.get("media")
        if isinstance(media, list):
            return [item for item in media if isinstance(item, dict)]
        if isinstance(media, dict):
            return [media]
    return []


def extract_media(payload: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for item in _media_items(payload):
        if kind == "image":
            image = item.get("image") if isinstance(item.get("image"), dict) else item
            generated = image.get("generatedImage") if isinstance(image.get("generatedImage"), dict) else image
            media_id = generated.get("mediaGenerationId") or item.get("mediaGenerationId")
            url = generated.get("fifeUrl") or generated.get("imageUrl") or generated.get("url") or item.get("imageUrl")
            encoded = generated.get("encodedImage") or item.get("encodedImage")
            if media_id or url or encoded:
                result = {"media_id": str(media_id or ""), "url": str(url or ""), "encoded": str(encoded or "")}
                if result["encoded"]:
                    try:
                        raw = base64.b64decode(result["encoded"], validate=False)
                        result["mime"] = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                    except Exception:
                        result["mime"] = "application/octet-stream"
                return result
        else:
            media_id = item.get("mediaGenerationId")
            url = item.get("videoUrl") or item.get("url")
            if media_id or url:
                return {
                    "media_id": str(media_id or ""),
                    "url": str(url or ""),
                    "thumbnail_url": str(item.get("thumbnailUrl") or item.get("thumbnail") or ""),
                    "duration": str(item.get("duration") or ""),
                    "aspect_ratio": str(item.get("aspectRatio") or item.get("aspect_ratio") or ""),
                }
    direct_media = payload.get("mediaGenerationId")
    direct_url = payload.get("videoUrl") if kind == "video" else payload.get("imageUrl")
    if direct_media or direct_url:
        return {"media_id": str(direct_media or ""), "url": str(direct_url or "")}
    return None


def parse_job(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    media = extract_media(payload, kind)
    status = _status(payload)
    if media and (not status or status in {"completed", "complete", "succeeded", "success"}):
        return {"status": "completed", **media}
    if status in FINAL_FAILURES or _operation_failed(payload):
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        return {"status": "failed", "error": str(payload.get("error") or response.get("error") or "Flow job failed.")}
    if status in {"completed", "complete", "succeeded", "success"}:
        if media:
            return {"status": "completed", **media}
        return {"status": "failed", "error": "Flow marked the job completed but returned no media."}
    return {"status": "processing", "provider_status": status or "unknown"}


class FlowPovClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        cfg = settings()
        self.token = str(token if token is not None else cfg.useapi_token).strip()
        self.base_url = str(base_url or cfg.flow_base).strip().rstrip("/")
        self.transport = transport or RequestsTransport()
        self.sleep = sleep
        self.monotonic = monotonic

    def _headers(self, *, json_content: bool = False, content_type: str = "") -> dict[str, str]:
        if not self.token:
            raise FlowPovProviderError("Missing USEAPI_TOKEN", alert_human=True)
        headers = {"Authorization": f"Bearer {self.token}"}
        if json_content:
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str = "",
        timeout: int,
    ) -> dict[str, Any]:
        try:
            response = self.transport.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(json_content=json_body is not None, content_type=content_type),
                json=json_body,
                data=data,
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise FlowPovProviderError("Flow request timed out.", retryable=True, wait_seconds=30) from exc
        except (ConnectionError, OSError) as exc:
            raise FlowPovProviderError(str(exc), retryable=True, wait_seconds=30) from exc
        if int(response.status_code) >= 400:
            body = _payload_text(response)
            decision = error_decision(response.status_code, body, response.headers.get("Retry-After"))
            raise FlowPovProviderError(
                f"Flow HTTP {response.status_code}: {body}",
                status_code=int(response.status_code),
                retryable=decision.retryable,
                wait_seconds=decision.wait_seconds,
                alert_human=decision.alert_human,
                moderation=decision.moderation,
            )
        try:
            payload = response.json() if response.content else {}
        except Exception as exc:
            raise FlowPovProviderError("Flow returned invalid JSON.", retryable=True, wait_seconds=30) from exc
        if not isinstance(payload, dict):
            raise FlowPovProviderError("Flow returned a non-object response.", retryable=True, wait_seconds=30)
        return payload

    def upload_product_asset(self, raw: bytes, mime: str, email: str) -> str:
        account = str(email or "").strip()
        if not account:
            raise ValueError("A connected Google Flow account email is required.")
        payload = self._request(
            "POST",
            f"/assets/{quote(account, safe='')}",
            data=raw,
            content_type=mime or "image/jpeg",
            timeout=120,
        )
        media = payload.get("mediaGenerationId")
        if isinstance(media, dict):
            media = media.get("mediaGenerationId")
        if not media:
            raise FlowPovProviderError("Flow uploaded the product photo but returned no mediaGenerationId.", retryable=True, wait_seconds=30)
        return str(media)

    def submit_image(
        self,
        *,
        prompt: str,
        product_media_ids: list[str],
        email: str,
        model: str = "nano-banana-pro",
    ) -> dict[str, Any]:
        refs = [str(value).strip() for value in product_media_ids if str(value).strip()][:4]
        if not refs:
            raise ValueError("Shoes POV image generation requires at least one product reference.")
        body: dict[str, Any] = {
            "prompt": f"{image_reference_caption(len(refs))}\n\n{str(prompt).strip()}",
            "model": model,
            "aspectRatio": "9:16",
            "count": 1,
            "email": str(email or "").strip(),
        }
        if not body["email"]:
            raise ValueError("A connected Google Flow account email is required.")
        for index, media_id in enumerate(refs, start=1):
            body[f"reference_{index}"] = media_id
        payload = self._request("POST", "/images", json_body=body, timeout=120)
        media = extract_media(payload, "image")
        if media:
            return {"status": "completed", **media, "model": model}
        job_id = _job_id(payload)
        if not job_id:
            raise FlowPovProviderError("Flow image submit returned neither media nor a job ID.", retryable=True, wait_seconds=30)
        return {"status": "processing", "job_id": job_id, "model": model}

    def get_job(self, job_id: str, *, kind: str) -> dict[str, Any]:
        job = str(job_id or "").strip().strip("\"'")
        if not job:
            raise ValueError("A Flow job ID is required.")
        payload = self._request("GET", f"/jobs/{quote(job, safe=':@+-._')}", timeout=45)
        return parse_job(payload, kind)

    def wait_for_job(self, job_id: str, *, kind: str, deadline_seconds: int, poll_seconds: int) -> dict[str, Any]:
        started = self.monotonic()
        while True:
            result = self.get_job(job_id, kind=kind)
            if result["status"] == "completed":
                return result
            if result["status"] == "failed":
                raise FlowPovProviderError(str(result.get("error") or "Flow job failed."))
            if self.monotonic() - started >= deadline_seconds:
                raise FlowPovProviderError(f"Flow {kind} job exceeded its {deadline_seconds}-second deadline.", retryable=True, wait_seconds=30, alert_human=True)
            self.sleep(poll_seconds)

    def generate_image(
        self,
        *,
        prompt: str,
        product_media_ids: list[str],
        email: str,
        deadline_seconds: int = 90,
    ) -> dict[str, Any]:
        # Shoes POV is intentionally locked to the heavier model. Do not silently
        # downgrade future renders to nano-banana-2 or nano-banana-2-lite.
        model = "nano-banana-pro"
        for attempt in range(2):
            try:
                submitted = self.submit_image(prompt=prompt, product_media_ids=product_media_ids, email=email, model=model)
                if submitted["status"] == "completed":
                    return submitted
                completed = self.wait_for_job(
                    submitted["job_id"],
                    kind="image",
                    deadline_seconds=deadline_seconds,
                    poll_seconds=3,
                )
                return {**completed, "model": model}
            except FlowPovProviderError as exc:
                if attempt == 0 and exc.moderation:
                    model = "nano-banana-pro"
                    continue
                raise
        raise FlowPovProviderError("Flow image generation failed after the moderation retry.")

    def submit_video(self, *, prompt: str, start_image_media_id: str, email: str) -> dict[str, Any]:
        start_media = str(start_image_media_id or "").strip()
        if not start_media:
            raise ValueError("The approved Shoes POV image media ID is required.")
        account = str(email or "").strip()
        if not account:
            raise ValueError("A connected Google Flow account email is required.")
        body = {
            "prompt": str(prompt or "").strip(),
            "model": "omni-flash",
            "duration": 8,
            "aspectRatio": "portrait",
            "async": True,
            "resolution": "720p",
            "startImage": start_media,
            "email": account,
        }
        payload = self._request("POST", "/videos", json_body=body, timeout=120)
        media = extract_media(payload, "video")
        if media:
            return {"status": "completed", **media}
        job_id = _job_id(payload)
        if not job_id:
            raise FlowPovProviderError("Omni submitted without returning a job ID.", retryable=True, wait_seconds=30)
        return {"status": "processing", "job_id": job_id}

    def wait_for_video(self, job_id: str, *, deadline_seconds: int = 1800) -> dict[str, Any]:
        return self.wait_for_job(job_id, kind="video", deadline_seconds=deadline_seconds, poll_seconds=10)

    def submit_upscale(self, *, media_generation_id: str, resolution: str = "1080p") -> dict[str, Any]:
        media_id = str(media_generation_id or "").strip()
        if not media_id:
            raise ValueError("The completed Flow video media ID is required for upscale.")
        body = {"mediaGenerationId": media_id, "resolution": resolution, "async": True}
        last_error: FlowPovProviderError | None = None
        for attempt in range(3):
            try:
                payload = self._request("POST", "/videos/upscale", json_body=body, timeout=120)
                media = extract_media(payload, "video")
                if media:
                    return {"status": "completed", **media}
                job_id = _job_id(payload)
                if not job_id:
                    raise FlowPovProviderError("Flow upscale returned neither media nor a job ID.", retryable=True, wait_seconds=20)
                return {"status": "processing", "job_id": job_id}
            except FlowPovProviderError as exc:
                last_error = exc
                if attempt >= 2 or not exc.retryable:
                    raise
                self.sleep(20 * (attempt + 1))
        raise last_error or FlowPovProviderError("Flow upscale was unavailable.")

    def wait_for_upscale(self, job_id: str, *, deadline_seconds: int = 900) -> dict[str, Any]:
        return self.wait_for_job(job_id, kind="video", deadline_seconds=deadline_seconds, poll_seconds=10)


__all__ = [
    "ErrorDecision",
    "FlowPovClient",
    "FlowPovProviderError",
    "RequestsTransport",
    "error_decision",
    "extract_media",
    "parse_job",
]
