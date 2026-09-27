from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.config import settings
from backend.shoe_pov_prompts import normalize_format


IMAGE_GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["differences", "confidence", "frame"],
    "properties": {
        "differences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "severity"],
                "properties": {
                    "text": {"type": "string"},
                    "severity": {"type": "string", "enum": ["major", "minor"]},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "frame": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "shoesInHands",
                "shoesOnFeet",
                "shoesTotal",
                "handsVisible",
                "feetVisible",
                "copiesProductPhoto",
            ],
            "properties": {
                "shoesInHands": {"type": "integer", "minimum": 0, "maximum": 2},
                "shoesOnFeet": {"type": "integer", "minimum": 0, "maximum": 2},
                "shoesTotal": {"type": "integer", "minimum": 0},
                "handsVisible": {"type": "boolean"},
                "feetVisible": {"type": "boolean"},
                "copiesProductPhoto": {"type": "boolean"},
            },
        },
    },
}


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    action: str
    reasons: tuple[str, ...]
    confidence: float
    raw: dict[str, Any]


class ResponseLike(Protocol):
    status_code: int
    headers: dict[str, str]
    text: str
    content: bytes

    def json(self) -> Any: ...


class VisionTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: int,
    ) -> ResponseLike: ...


class RequestsVisionTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: int,
    ) -> ResponseLike:
        import requests

        return requests.request(method, url, headers=headers, json=json, timeout=timeout)


class ImageGateError(RuntimeError):
    pass


def build_image_gate_prompt(*, format_name: str, seller_description: str, lighting: str = "") -> str:
    fmt = normalize_format(format_name)
    grade = f'THE STILL IS DELIBERATELY GRADED: "{lighting}". Judge hue and pattern, never brightness, warmth or shade alone.\n' if str(lighting or "").strip() else ""
    if fmt == "worn":
        view = (
            "THE STILL IS A FIRST-PERSON PHOTO OF THE CREATOR'S OWN FEET, looking down from where they sit: "
            "BOTH feet wear the shoe, no hand is in frame, no shoe is held. Nothing is mirrored — printed text reads "
            "normally. Compare BOTH worn shoes against the product photo; both must be the product."
        )
        copy_tail = "a phone shot of the creator's own two feet in the shoes and the ground"
    else:
        view = (
            "THE STILL IS A FIRST-PERSON PHOTO OF THE CREATOR'S OWN HANDS holding the pair of shoes over a car seat, "
            "lap or open shoe box: one shoe in each hand, no foot and no worn shoe in frame. Nothing is mirrored — printed "
            "text reads normally. Compare BOTH held shoes against the product photo; both must be the product."
        )
        copy_tail = "a phone shot of the creator's own two hands holding the pair over the seat or box"
    return (
        "You are a quality inspector for a fashion shop. Compare the garment worn in the RENDERED STILL against the "
        "PRODUCT PHOTO(S). List every difference and grade it.\n"
        f"{grade}"
        f"Product notes: {str(seller_description or '').strip()}\n"
        "THE PRODUCT IS THE SHOES: compare only that garment; everything else worn with it is styling and is ignored.\n"
        f"{view}\n"
        "FOR SHOES the logo and printed text must be legible at close range — this is a hard requirement: a logo, wordmark "
        "or printed word that is unreadable, garbled, misspelled, missing, duplicated or on a different part of the shoe than "
        "in the product photo is MAJOR; so is a sole of another colour or thickness, different laces/straps/buckles, a pattern "
        "or colourway that differs, or two shoes that do not match each other.\n"
        "MAJOR = changes what is being sold: closure type or length, how it is worn versus the product photo, colour or shade, "
        "a print/graphic/logo that is missing, added, replaced by a different design, or on a different product area, overall "
        "length or silhouette, or a set rendered as a single piece or vice versa. MINOR = small details a buyer would forgive: "
        "pocket count, stitching, subtle texture or sheen, fit looseness, small trim. Ignore the person, room, lighting, pose, "
        "phone, and anything worn with it that is not the product. Return JSON exactly as requested by the supplied schema. "
        "For frame.copiesProductPhoto, mark true when the still repeats a product photo's scene, camera angle, background, "
        f"model or legs instead of {copy_tail}."
    )


def read_gate_decision(payload: dict[str, Any], *, format_name: str) -> GateDecision:
    fmt = normalize_format(format_name)
    confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0)))
    reasons: list[str] = []
    for item in payload.get("differences") or []:
        if isinstance(item, dict) and str(item.get("severity") or "").lower() == "major":
            text = str(item.get("text") or "").strip()
            if text:
                reasons.append(text)

    frame = payload.get("frame") if isinstance(payload.get("frame"), dict) else {}
    in_hands = int(frame.get("shoesInHands") or 0)
    on_feet = int(frame.get("shoesOnFeet") or 0)
    total = int(frame.get("shoesTotal") or 0)
    hands_visible = bool(frame.get("handsVisible"))
    feet_visible = bool(frame.get("feetVisible"))
    copied = bool(frame.get("copiesProductPhoto"))

    if in_hands > 0 and on_feet > 0:
        reasons.append("FRAME: mixes the poses, with a shoe in a hand and a shoe on a foot")
    if fmt == "worn":
        if on_feet != 2:
            reasons.append(f"FRAME: expected two feet wearing the shoes, found {on_feet}")
        if in_hands > 0:
            reasons.append("FRAME: shows a hand holding a shoe")
        elif hands_visible:
            reasons.append("FRAME: shows a hand in frame")
    else:
        if in_hands != 2:
            reasons.append(f"FRAME: expected two shoes held in the hands, found {in_hands}")
        if on_feet > 0:
            reasons.append("FRAME: shows a shoe on a foot")
        elif feet_visible:
            reasons.append("FRAME: shows a foot in frame")
    if total > 2:
        reasons.append(f"FRAME: shows {total} shoes in the image")
    if copied:
        reasons.append("FRAME: copied the listing scene instead of the required phone shot")

    deduped = tuple(dict.fromkeys(reasons))
    if not deduped:
        action = "pass"
    elif confidence < 0.6:
        action = "hold"
    else:
        action = "rerender"
    return GateDecision(not deduped, action, deduped, confidence, payload)


def image_guard_note(*, format_name: str, reasons: list[str] | tuple[str, ...]) -> str:
    fmt = normalize_format(format_name)
    joined = "; ".join(str(value).strip() for value in reasons if str(value).strip())
    if any(str(value).startswith("FRAME:") for value in reasons):
        if fmt == "worn":
            middle = "a phone shot looking down at the creator's own TWO feet, BOTH wearing the shoe, resting on the ground, NO hand in frame and NO shoe held; exactly two shoes, both on the feet"
        else:
            middle = "a phone shot of the creator's own TWO hands holding the pair at chest height over the seat, lap or open box, one shoe in each hand, NO foot and NO worn shoe in frame; exactly two shoes, both in the hands"
        return (
            f"FRAME GUARD — the previous render was not the frame described above ({joined}). Draw the FRAME exactly: "
            f"{middle}, never three, never one on a foot and one in a hand. The product photos are references for the SHOE "
            "ONLY: do not copy their scene, camera angle, background, model, legs or pose."
        )
    return (
        f"PRODUCT FIDELITY — the previous render got the shoe wrong ({joined}). Match the product photo on these points on "
        "BOTH shoes — nothing is mirrored, the logo and printed text read exactly as in the photo; where the written "
        "description and the photo disagree, the photo wins. Do not redesign, simplify or change the logo, colour, pattern, "
        "sole or closures."
    )


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(raw).decode('ascii')}"


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    raise ImageGateError("Vision gate returned no text output.")


class OpenAIImageGateClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        transport: VisionTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = settings()
        self.api_key = str(api_key if api_key is not None else cfg.openai_api_key).strip()
        self.model = str(model or cfg.shoe_pov_vision_model).strip()
        self.transport = transport or RequestsVisionTransport()
        self.sleep = sleep

    def inspect(
        self,
        *,
        product_images: list[tuple[bytes, str]],
        rendered_image: tuple[bytes, str],
        seller_description: str,
        format_name: str,
        lighting: str = "",
    ) -> GateDecision:
        if not self.api_key:
            raise ImageGateError("OPENAI_API_KEY is not configured; the image gate must hold this item.")
        if not product_images:
            raise ValueError("The image gate requires at least one product photo.")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": build_image_gate_prompt(format_name=format_name, seller_description=seller_description, lighting=lighting)}]
        for index, (raw, mime) in enumerate(product_images[:4], start=1):
            content.extend([
                {"type": "input_text", "text": f"PRODUCT PHOTO {index}"},
                {"type": "input_image", "image_url": _data_url(raw, mime), "detail": "high"},
            ])
        content.extend([
            {"type": "input_text", "text": "RENDERED STILL"},
            {"type": "input_image", "image_url": _data_url(rendered_image[0], rendered_image[1]), "detail": "high"},
        ])
        body = {
            "model": self.model,
            "temperature": 0,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "shoe_pov_image_gate",
                    "strict": True,
                    "schema": IMAGE_GATE_SCHEMA,
                }
            },
        }
        last_error = ""
        for attempt in range(2):
            try:
                response = self.transport.request(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=body,
                    timeout=120,
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0:
                    self.sleep(5)
                    continue
                raise ImageGateError(f"Vision gate request failed: {last_error}") from exc
            if response.status_code == 429 and attempt == 0:
                try:
                    wait = max(1, int(float(response.headers.get("Retry-After") or "10")))
                except Exception:
                    wait = 10
                self.sleep(wait)
                continue
            if response.status_code >= 400:
                raise ImageGateError(f"Vision gate HTTP {response.status_code}: {response.text[:1200]}")
            try:
                result = json.loads(_response_text(response.json()))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ImageGateError("Vision gate returned invalid JSON.") from exc
            if not isinstance(result, dict):
                raise ImageGateError("Vision gate returned a non-object result.")
            return read_gate_decision(result, format_name=format_name)
        raise ImageGateError(last_error or "Vision gate request failed.")


__all__ = [
    "GateDecision",
    "IMAGE_GATE_SCHEMA",
    "ImageGateError",
    "OpenAIImageGateClient",
    "build_image_gate_prompt",
    "image_guard_note",
    "read_gate_decision",
]
