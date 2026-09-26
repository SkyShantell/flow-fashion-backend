from __future__ import annotations

import base64
import hmac
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from backend.config import settings
from backend.services.shoe_pov_flow import FlowPovClient, FlowPovProviderError
from backend.shoe_pov_prompts import WORN_HOOKS, build_image_prompt


app = FastAPI(title="Shoes POV isolated image test")


class ImageReference(BaseModel):
    mime: str = "image/png"
    base64_data: str = Field(min_length=1)


class WornImageRequest(BaseModel):
    product_title: str = Field(min_length=1)
    product_description: str = ""
    gender: str = "female"
    skin_tone: str = Field(min_length=1)
    seed: str = "isolated-paid-image-test"
    references: list[ImageReference] = Field(min_length=1, max_length=4)


def _authorize(received: str) -> None:
    expected = os.getenv("SHOE_POV_TEST_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> dict[str, bool]:
    cfg = settings()
    return {
        "ok": True,
        "useapi_configured": bool(cfg.useapi_token),
        "flow_account_configured": bool(cfg.google_flow_email),
    }


@app.post("/run-worn-image")
def run_worn_image(
    request: WornImageRequest,
    x_shoe_pov_test_token: str = Header(default=""),
) -> dict[str, str]:
    _authorize(x_shoe_pov_test_token)
    cfg = settings()
    if not cfg.google_flow_email:
        raise HTTPException(status_code=503, detail="GOOGLE_FLOW_EMAIL is not configured")

    client = FlowPovClient()
    media_ids: list[str] = []
    try:
        for reference in request.references:
            try:
                raw = base64.b64decode(reference.base64_data, validate=True)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="A product reference is not valid base64") from exc
            media_ids.append(client.upload_product_asset(raw, reference.mime, cfg.google_flow_email))

        prompt = build_image_prompt(
            gender=request.gender,
            skin_tone=request.skin_tone,
            product_title=request.product_title,
            product_description=request.product_description or request.product_title,
            format_name="worn",
            hook=WORN_HOOKS[0],
            seed=request.seed,
            index=0,
        )
        result = client.generate_image(
            prompt=prompt,
            product_media_ids=media_ids,
            email=cfg.google_flow_email,
        )
    except FlowPovProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "status": str(result.get("status") or "completed"),
        "media_id": str(result.get("media_id") or ""),
        "url": str(result.get("url") or ""),
        "encoded": str(result.get("encoded") or ""),
        "mime": str(result.get("mime") or ""),
        "model": str(result.get("model") or ""),
        "prompt": prompt,
    }
