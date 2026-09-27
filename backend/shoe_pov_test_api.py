from __future__ import annotations

import base64
import hmac
import os

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from backend.config import settings
from backend.services.shoe_pov_flow import FlowPovClient, FlowPovProviderError
from backend.shoe_pov_image_gate import ImageGateError, OpenAIImageGateClient, image_guard_note
from backend.shoe_pov_prompts import WORN_HOOKS, build_image_prompt, build_video_prompt, choose_beats
from backend.shoe_pov_video_gate import OpenAIVideoGateClient, VideoGateError, extract_video_sheets


app = FastAPI(title="Shoes POV isolated gate and video test")


class ImageReference(BaseModel):
    mime: str = "image/png"
    base64_data: str = Field(min_length=1)


class ImageGateRequest(BaseModel):
    product_description: str = Field(min_length=1)
    format_name: str = "worn"
    references: list[ImageReference] = Field(min_length=1, max_length=4)
    rendered: ImageReference


class VideoRequest(BaseModel):
    start_image_media_id: str = Field(min_length=1)
    product_title: str = Field(min_length=1)
    product_description: str = Field(min_length=1)
    gender: str = "female"
    skin_tone: str = Field(min_length=1)
    seed: str = "tnf-thermoball-pink-worn-test-1"


class RerenderImageRequest(BaseModel):
    product_title: str = Field(min_length=1)
    product_description: str = Field(min_length=1)
    gender: str = "female"
    skin_tone: str = Field(min_length=1)
    seed: str = "tnf-thermoball-pink-worn-test-1"
    reasons: list[str] = Field(min_length=1)
    references: list[ImageReference] = Field(min_length=1, max_length=4)


class VideoGateRequest(BaseModel):
    video_url: str = Field(min_length=1)
    product_description: str = Field(min_length=1)
    format_name: str = "worn"
    references: list[ImageReference] = Field(min_length=1, max_length=3)


def _authorize(received: str) -> None:
    expected = os.getenv("SHOE_POV_TEST_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _decode(reference: ImageReference) -> tuple[bytes, str]:
    try:
        return base64.b64decode(reference.base64_data, validate=True), reference.mime
    except Exception as exc:
        raise HTTPException(status_code=400, detail="A reference is not valid base64") from exc


@app.get("/health")
def health() -> dict[str, bool]:
    cfg = settings()
    return {
        "ok": True,
        "useapi_configured": bool(cfg.useapi_token),
        "flow_account_configured": bool(cfg.google_flow_email),
        "openai_configured": bool(cfg.openai_api_key),
    }


@app.post("/run-image-gate")
def run_image_gate(request: ImageGateRequest, x_shoe_pov_test_token: str = Header(default="")) -> dict:
    _authorize(x_shoe_pov_test_token)
    try:
        decision = OpenAIImageGateClient().inspect(
            product_images=[_decode(reference) for reference in request.references],
            rendered_image=_decode(request.rendered),
            seller_description=request.product_description,
            format_name=request.format_name,
        )
    except ImageGateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "passed": decision.passed,
        "action": decision.action,
        "reasons": list(decision.reasons),
        "confidence": decision.confidence,
        "raw": decision.raw,
    }


@app.post("/run-video")
def run_video(request: VideoRequest, x_shoe_pov_test_token: str = Header(default="")) -> dict:
    _authorize(x_shoe_pov_test_token)
    cfg = settings()
    if not cfg.google_flow_email:
        raise HTTPException(status_code=503, detail="GOOGLE_FLOW_EMAIL is not configured")
    beats = choose_beats("worn", seed=request.seed, index=0)
    prompt = build_video_prompt(
        gender=request.gender,
        skin_tone=request.skin_tone,
        product_title=request.product_title,
        product_description=request.product_description,
        format_name="worn",
        beats=beats,
        seed=request.seed,
        index=0,
        key_detail="The North Face wordmark and half-dome logo",
        product_colour="pale pink",
        product_branding="The North Face wordmark and half-dome logo exactly as in the references and first frame",
        product_pattern_material="horizontal quilted insulated fabric",
        product_sole="gray speckled rubber traction outsole",
        product_closures="slip-on opening with no laces, straps or buckles",
        talking=False,
    )
    client = FlowPovClient()
    try:
        native = client.submit_video(prompt=prompt, start_image_media_id=request.start_image_media_id, email=cfg.google_flow_email)
        if native["status"] != "completed":
            native = client.wait_for_video(native["job_id"])
        upscaled = client.submit_upscale(media_generation_id=native["media_id"], resolution="1080p")
        if upscaled["status"] != "completed":
            upscaled = client.wait_for_upscale(upscaled["job_id"])
    except FlowPovProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"native": native, "upscaled": upscaled, "prompt": prompt}


@app.post("/run-rerender-image")
def run_rerender_image(request: RerenderImageRequest, x_shoe_pov_test_token: str = Header(default="")) -> dict:
    _authorize(x_shoe_pov_test_token)
    cfg = settings()
    if not cfg.google_flow_email:
        raise HTTPException(status_code=503, detail="GOOGLE_FLOW_EMAIL is not configured")
    prompt = build_image_prompt(
        gender=request.gender,
        skin_tone=request.skin_tone,
        product_title=request.product_title,
        product_description=request.product_description,
        format_name="worn",
        hook=WORN_HOOKS[0],
        seed=request.seed,
        index=0,
    )
    prompt = f"{prompt}\n\n{image_guard_note(format_name='worn', reasons=request.reasons)}"
    client = FlowPovClient()
    try:
        media_ids = [client.upload_product_asset(*_decode(reference), cfg.google_flow_email) for reference in request.references]
        result = client.generate_image(prompt=prompt, product_media_ids=media_ids, email=cfg.google_flow_email)
    except FlowPovProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**result, "prompt": prompt}


@app.post("/run-video-gate")
def run_video_gate(request: VideoGateRequest, x_shoe_pov_test_token: str = Header(default="")) -> dict:
    _authorize(x_shoe_pov_test_token)
    try:
        response = requests.get(request.video_url, timeout=180)
        response.raise_for_status()
        sheets = extract_video_sheets(response.content, format_name=request.format_name, seconds=8)
        decision = OpenAIVideoGateClient().inspect(
            product_images=[_decode(reference) for reference in request.references[:3]],
            timeline_sheet=sheets["timeline"],
            shoe_sheet=sheets["shoes"],
            product_details=request.product_description,
            format_name=request.format_name,
            seconds=8,
        )
    except (requests.RequestException, VideoGateError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"passed": decision.passed, "action": decision.action, "reasons": list(decision.reasons), "raw": decision.raw}
