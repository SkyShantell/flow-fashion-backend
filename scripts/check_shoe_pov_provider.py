#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.shoe_pov_flow import (  # noqa: E402
    FlowPovClient,
    FlowPovProviderError,
    error_decision,
    extract_media,
    parse_job,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeTransport:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Fake provider received an unexpected call: {method} {url}")
        return self.responses.pop(0)


@dataclass
class FakeClock:
    now: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    responses = [
        FakeResponse(200, {"mediaGenerationId": {"mediaGenerationId": "product_media_1"}}),
        FakeResponse(200, {"jobId": "image_job_1", "status": "created"}),
        FakeResponse(200, {"status": "pending"}),
        FakeResponse(200, {"response": {"media": [{"image": {"generatedImage": {"mediaGenerationId": "image_media_1", "encodedImage": base64.b64encode(png).decode("ascii")}}}]}}),
        FakeResponse(200, {"jobid": "video_job_1", "status": "created"}),
        FakeResponse(200, {"status": "IN_PROGRESS"}),
        FakeResponse(200, {"status": "completed", "response": {"media": [{"mediaGenerationId": "video_media_720", "videoUrl": "https://example.test/video-720.mp4", "duration": "8s", "aspectRatio": "9:16"}]}}),
        FakeResponse(503, {"error": "temporary upstream failure"}),
        FakeResponse(503, {"error": "temporary upstream failure"}),
        FakeResponse(200, {"jobId": "upscale_job_1", "status": "created"}),
        FakeResponse(200, {"status": "processing"}),
        FakeResponse(200, {"response": {"status": "completed", "media": [{"mediaGenerationId": "video_media_1080", "videoUrl": "https://example.test/video-1080.mp4"}]}}),
    ]
    transport = FakeTransport(responses)
    clock = FakeClock()
    client = FlowPovClient(
        token="fake-token",
        base_url="https://fake-flow.test/v1/google-flow",
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    product_media = client.upload_product_asset(b"jpeg bytes", "image/jpeg", "creator@example.com")
    check(product_media == "product_media_1", "nested upload mediaGenerationId was not read")

    image = client.generate_image(
        prompt="FORMAT: WORN. test prompt",
        product_media_ids=[product_media],
        email="creator@example.com",
    )
    check(image["media_id"] == "image_media_1", "async image polling did not return media")
    check(image["mime"] == "image/png", "base64 PNG signature was not detected")

    video_submit = client.submit_video(
        prompt="FORMAT: WORN. test video prompt",
        start_image_media_id=image["media_id"],
        email="creator@example.com",
    )
    check(video_submit["job_id"] == "video_job_1", "video submit did not return its job ID")
    video = client.wait_for_video(video_submit["job_id"])
    check(video["media_id"] == "video_media_720", "video poll did not read response.media")
    check(video["duration"] == "8s" and video["aspect_ratio"] == "9:16", "video metadata was not retained")

    upscale_submit = client.submit_upscale(media_generation_id=video["media_id"])
    check(upscale_submit["job_id"] == "upscale_job_1", "upscale retry did not eventually submit")
    upscale = client.wait_for_upscale(upscale_submit["job_id"])
    check(upscale["media_id"] == "video_media_1080", "upscale polling did not return 1080 media")
    check(not transport.responses, "fake response queue was not fully consumed")

    upload_call = transport.calls[0]
    check(upload_call["url"].endswith("/assets/creator%40example.com"), "asset upload did not use the account-specific route")
    check(upload_call["data"] == b"jpeg bytes" and upload_call["json"] is None, "asset upload was not a raw-body upload")
    check(upload_call["headers"].get("Content-Type") == "image/jpeg", "asset upload MIME was not declared")

    image_call = transport.calls[1]
    image_body = image_call["json"]
    check(image_body["model"] == "nano-banana-2", "default image model is not nano-banana-2")
    check(image_body["aspectRatio"] == "9:16" and image_body["count"] == 1, "image shape/count is wrong")
    check(image_body["reference_1"] == "product_media_1", "product reference was not placed in slot 1")
    check(image_body["prompt"].startswith("@reference_1 is the exact product;"), "short Flow reference caption is missing")

    video_call = transport.calls[4]
    video_body = video_call["json"]
    expected_video = {
        "model": "omni-flash",
        "duration": 8,
        "aspectRatio": "portrait",
        "async": True,
        "resolution": "720p",
        "startImage": "image_media_1",
        "email": "creator@example.com",
    }
    for key, value in expected_video.items():
        check(video_body.get(key) == value, f"video body has wrong {key}: {video_body.get(key)!r}")
    check(not any(key.startswith("referenceImage_") for key in video_body), "Flow video incorrectly includes references beside startImage")

    upscale_calls = [call for call in transport.calls if call["url"].endswith("/videos/upscale")]
    check(len(upscale_calls) == 3, "upscale did not retry transient 5xx exactly twice")
    for call in upscale_calls:
        check("email" not in call["json"], "upscale body contains forbidden email field")
        check(call["json"] == {"mediaGenerationId": "video_media_720", "resolution": "1080p", "async": True}, "upscale body changed")
    check(20 in clock.sleeps and 40 in clock.sleeps, "upscale retries did not use growing waits")

    submit_calls = [call for call in transport.calls if call["method"] == "POST" and "/assets/" not in call["url"]]
    poll_calls = [call for call in transport.calls if call["method"] == "GET"]
    check(all(call["timeout"] == 120 for call in submit_calls), "a provider submit did not use the 120-second timeout")
    check(all(call["timeout"] == 45 for call in poll_calls), "a provider poll did not use the 45-second timeout")
    check(3 in clock.sleeps and 10 in clock.sleeps, "image/video polling intervals were not 3s/10s")

    moderation_transport = FakeTransport([
        FakeResponse(500, {"error": "generation was moderated"}),
        FakeResponse(200, {"media": [{"image": {"generatedImage": {"mediaGenerationId": "moderation_retry_image", "imageUrl": "https://example.test/image.png"}}}]}),
    ])
    moderation_client = FlowPovClient(
        token="fake-token",
        base_url="https://fake-flow.test/v1/google-flow",
        transport=moderation_transport,
        sleep=lambda _seconds: None,
    )
    moderation_image = moderation_client.generate_image(
        prompt="safe product prompt",
        product_media_ids=["product_media_1", "product_media_2"],
        email="creator@example.com",
    )
    check(moderation_image["model"] == "nano-banana-pro", "moderation retry did not switch to nano-banana-pro")
    check(moderation_transport.calls[0]["json"]["model"] == "nano-banana-2", "first moderation attempt used the wrong model")
    check(moderation_transport.calls[1]["json"]["model"] == "nano-banana-pro", "second moderation attempt used the wrong model")
    check(moderation_transport.calls[1]["json"]["reference_1"] == "product_media_1", "moderation retry lost product slot 1")

    expected_errors = {
        400: (False, 0, False),
        401: (False, 0, True),
        402: (False, 0, True),
        403: (True, 120, True),
        404: (False, 0, True),
        408: (True, 30, False),
        429: (True, 77, False),
        500: (True, 30, False),
        503: (True, 60, False),
        596: (True, 300, True),
    }
    for status, expected in expected_errors.items():
        decision = error_decision(status, "ordinary provider error", "77" if status == 429 else None)
        actual = (decision.retryable, decision.wait_seconds, decision.alert_human)
        check(actual == expected, f"HTTP {status} mapping is {actual}, expected {expected}")
    check(error_decision(400, "PUBLIC_ERROR_UNSAFE_GENERATION").moderation, "400 unsafe generation was not classified as moderation")
    check(error_decision(500, "request moderated").moderation, "500 moderated response was not classified as moderation")

    media_without_status = extract_media({"media": [{"mediaGenerationId": "root-video", "videoUrl": "https://example.test/root.mp4"}]}, "video")
    check(media_without_status and media_without_status["media_id"] == "root-video", "root media without status was not read")
    parsed_without_status = parse_job({"media": [{"mediaGenerationId": "root-video", "videoUrl": "https://example.test/root.mp4"}]}, "video")
    check(parsed_without_status["status"] == "completed", "media without status was not treated as completed")
    operation_failure = parse_job({"status": "processing", "response": {"operations": [{"status": "FAILED"}]}}, "video")
    check(operation_failure["status"] == "failed", "FAILED response operation was not treated as failed")

    try:
        FlowPovClient(token="", base_url="https://fake-flow.test", transport=FakeTransport([])).submit_video(
            prompt="x", start_image_media_id="image", email="creator@example.com"
        )
    except FlowPovProviderError as exc:
        check(exc.alert_human, "missing token did not request human attention")
    else:
        raise AssertionError("missing token did not fail")

    print("PASS: fake image submit -> pending poll -> completed media")
    print("PASS: fake video submit -> pending poll -> completed 8s 9:16 media")
    print("PASS: upscale retries twice, reaches 1080p job, and never sends email")
    print("PASS: submits use 120s timeout; polls use 45s timeout; polling is 3s/10s")
    print("PASS: Flow start-frame video sends no referenceImage fields")
    print("PASS: moderation retries exactly once on nano-banana-pro")
    print("PASS: HTTP 400/401/402/403/404/408/429/500/503/596 decisions match the handoff")
    print("PASS: nested/root media, PNG sniffing, missing status, and FAILED operations")
    print("PASS: fake transport only — zero network calls and zero credits spent")


if __name__ == "__main__":
    main()
