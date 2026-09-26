from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.config import settings
from backend.shoe_pov_image_gate import RequestsVisionTransport, VisionTransport
from backend.shoe_pov_prompts import normalize_format


VIDEO_GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["shoe", "motion"],
    "properties": {
        "shoe": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "brandingChanged",
                "patternOrColourChanged",
                "soleChanged",
                "closuresChanged",
                "handHoldsShoe",
                "shoeOnFoot",
                "extraHandOrFingers",
                "faceOrPhoneOrMirror",
                "extraShoes",
                "where",
                "notes",
            ],
            "properties": {
                "brandingChanged": {"type": "boolean"},
                "patternOrColourChanged": {"type": "boolean"},
                "soleChanged": {"type": "boolean"},
                "closuresChanged": {"type": "boolean"},
                "handHoldsShoe": {"type": "boolean"},
                "shoeOnFoot": {"type": "boolean"},
                "extraHandOrFingers": {"type": "boolean"},
                "faceOrPhoneOrMirror": {"type": "boolean"},
                "extraShoes": {"type": "boolean"},
                "where": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
        "motion": {
            "type": "object",
            "additionalProperties": False,
            "required": ["roomChanges", "jumpCut", "notes"],
            "properties": {
                "roomChanges": {"type": "boolean"},
                "jumpCut": {"type": "boolean"},
                "notes": {"type": "string"},
            },
        },
    },
}


@dataclass(frozen=True)
class VideoGateDecision:
    passed: bool
    action: str
    reasons: tuple[str, ...]
    raw: dict[str, Any]


class VideoGateError(RuntimeError):
    pass


def build_video_gate_prompt(*, format_name: str, product_details: str, seconds: float = 8.0) -> str:
    fmt = normalize_format(format_name)
    if fmt == "worn":
        frame_words = "the creator's own two feet wearing the shoes, filmed looking down from where they sit; no hand in frame"
        where = "both on the feet"
        extra_hand_words = "any hand or fingers in frame at all"
    else:
        frame_words = "the creator's own two hands holding the pair of shoes over a car seat, lap or open box; no foot in frame"
        where = "both in the hands"
        extra_hand_words = "more than two hands in frame, a hand with other than five fingers, or a deformed hand"
    gap = "a third of a second"
    return (
        "SHOE CHECK\n"
        f"Product notes: {str(product_details or '').strip()}\n"
        f"This is a first-person video: {frame_words}. Exactly two shoes of the product exist, {where}, never one on a foot "
        "and one in a hand. Judge both shoes against the PRODUCT PHOTO(S) in every frame, and the frame rules. "
        "brandingChanged: a logo, wordmark or printed text on the shoe is missing, garbled, misspelled, duplicated, moved or "
        "different from the product photo in any frame. patternOrColourChanged: the pattern or colourway differs from the "
        "product photo or drifts between frames. soleChanged: sole colour, thickness or tread differs. closuresChanged: laces, "
        "straps or buckles differ in count, colour or position. handHoldsShoe: in any frame a hand grips a shoe that is not on "
        "a foot. shoeOnFoot: in any frame a shoe of the product is worn on a foot. "
        f"extraHandOrFingers: {extra_hand_words}. faceOrPhoneOrMirror: any face, any phone, any mirror or reflection, any second "
        "person in any frame. extraShoes: any shoe beyond the two of the pair. Motion blur on the fast move is NOT a change; "
        "judge the frames where the shoes are steady. Be strict on branding and text.\n\n"
        "MOTION CHECK\n"
        "Judge whether the video stays in one place and one shot. roomChanges is true if the video cuts to a different place at "
        "any point — the surface, walls, floor, furniture or lighting are a different place in some frames than in the first "
        "frame (the camera moving closer or further in the same place is NOT a change). jumpCut is true if between two "
        f"consecutive frames the framing, camera position, product position or surroundings jump in a way nothing could move in {gap} "
        "(same place but the shot visibly restarts) — a smooth camera move or a hand moving the product is NOT a jump cut. "
        f"The inspected video duration is {seconds:g} seconds. Return JSON exactly as requested by the supplied schema."
    )


def read_video_gate_decision(payload: dict[str, Any], *, format_name: str) -> VideoGateDecision:
    fmt = normalize_format(format_name)
    shoe = payload.get("shoe") if isinstance(payload.get("shoe"), dict) else {}
    motion = payload.get("motion") if isinstance(payload.get("motion"), dict) else {}
    reasons: list[str] = []
    mapping = (
        ("brandingChanged", "changes the logo or printed text on the shoe"),
        ("patternOrColourChanged", "changes the shoe's pattern or colour"),
        ("soleChanged", "changes the sole"),
        ("closuresChanged", "changes the laces, straps or buckles"),
    )
    for key, reason in mapping:
        if bool(shoe.get(key)):
            reasons.append(reason)
    hand_holds = bool(shoe.get("handHoldsShoe"))
    on_foot = bool(shoe.get("shoeOnFoot"))
    if hand_holds and on_foot:
        reasons.append("mixes the poses, with a shoe in a hand and a shoe on a foot")
    elif fmt == "worn" and hand_holds:
        reasons.append("shows a hand holding a shoe")
    elif fmt == "held" and on_foot:
        reasons.append("shows a shoe on a foot")
    if bool(shoe.get("extraHandOrFingers")):
        reasons.append("shows a hand in frame" if fmt == "worn" else "shows an extra hand or wrong fingers")
    if bool(shoe.get("faceOrPhoneOrMirror")):
        reasons.append("shows a face, phone or mirror")
    if bool(shoe.get("extraShoes")):
        reasons.append("shows a third shoe")
    if bool(motion.get("roomChanges")):
        reasons.append("cuts to a different place mid-video")
    elif bool(motion.get("jumpCut")):
        reasons.append("cuts, with a jump cut mid-video")
    deduped = tuple(dict.fromkeys(reasons))
    return VideoGateDecision(not deduped, "pass" if not deduped else "rerender", deduped, payload)


def _run_ffmpeg(command: list[str], *, timeout: int = 120) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise VideoGateError(f"FFmpeg could not start: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoGateError(f"FFmpeg exceeded its {timeout}-second deadline.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown FFmpeg error").strip()[-1800:]
        raise VideoGateError(f"FFmpeg failed ({result.returncode}): {detail}")


def extract_video_sheets(video_bytes: bytes, *, format_name: str, seconds: float = 8.0, ffmpeg_binary: str = "ffmpeg") -> dict[str, bytes]:
    fmt = normalize_format(format_name)
    with tempfile.TemporaryDirectory(prefix="shoe-pov-gate-") as folder:
        root = Path(folder)
        source = root / "input.mp4"
        timeline = root / "timeline.jpg"
        shoes = root / "shoes.jpg"
        source.write_bytes(video_bytes)
        _run_ffmpeg([
            ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-t", str(seconds),
            "-vf", "fps=3,scale=360:-1,tile=6x4", "-frames:v", "1", str(timeline),
        ])
        crop_y = "ih*0.4" if fmt == "worn" else "ih*0.2"
        _run_ffmpeg([
            ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-t", str(seconds),
            "-vf", f"fps=6/{seconds:g},crop=iw*0.8:ih*0.5:iw*0.1:{crop_y},scale=720:-1,tile=3x2",
            "-frames:v", "1", str(shoes),
        ])
        if not timeline.is_file() or not timeline.stat().st_size:
            raise VideoGateError("FFmpeg produced no timeline sheet.")
        if not shoes.is_file() or not shoes.stat().st_size:
            raise VideoGateError("FFmpeg produced no shoe-crop sheet.")
        return {"timeline": timeline.read_bytes(), "shoes": shoes.read_bytes()}


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output") or []:
        if isinstance(item, dict):
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
    raise VideoGateError("Vision gate returned no text output.")


class OpenAIVideoGateClient:
    def __init__(self, *, api_key: str | None = None, model: str | None = None, transport: VisionTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        cfg = settings()
        self.api_key = str(api_key if api_key is not None else cfg.openai_api_key).strip()
        self.model = str(model or cfg.shoe_pov_vision_model).strip()
        self.transport = transport or RequestsVisionTransport()
        self.sleep = sleep

    def inspect(self, *, product_images: list[tuple[bytes, str]], timeline_sheet: bytes, shoe_sheet: bytes, product_details: str, format_name: str, seconds: float = 8.0) -> VideoGateDecision:
        if not self.api_key:
            raise VideoGateError("OPENAI_API_KEY is not configured; the video gate must hold this item.")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": build_video_gate_prompt(format_name=format_name, product_details=product_details, seconds=seconds)}]
        for index, (raw, mime) in enumerate(product_images[:3], start=1):
            content.extend([{"type": "input_text", "text": f"PRODUCT PHOTO {index}"}, {"type": "input_image", "image_url": _data_url(raw, mime), "detail": "high"}])
        content.extend([
            {"type": "input_text", "text": "VIDEO TIMELINE — consecutive frames are one third of a second apart"},
            {"type": "input_image", "image_url": _data_url(timeline_sheet, "image/jpeg"), "detail": "high"},
            {"type": "input_text", "text": "SHOE CROPS — six frames spread over the whole video"},
            {"type": "input_image", "image_url": _data_url(shoe_sheet, "image/jpeg"), "detail": "high"},
        ])
        body = {
            "model": self.model,
            "temperature": 0,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": "shoe_pov_video_gate", "strict": True, "schema": VIDEO_GATE_SCHEMA}},
        }
        for attempt in range(2):
            try:
                response = self.transport.request("POST", "https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json=body, timeout=120)
            except Exception as exc:
                if attempt == 0:
                    self.sleep(5)
                    continue
                raise VideoGateError(f"Vision gate request failed: {exc}") from exc
            if response.status_code == 429 and attempt == 0:
                try:
                    wait = max(1, int(float(response.headers.get("Retry-After") or "10")))
                except Exception:
                    wait = 10
                self.sleep(wait)
                continue
            if response.status_code >= 400:
                raise VideoGateError(f"Vision gate HTTP {response.status_code}: {response.text[:1200]}")
            try:
                result = json.loads(_response_text(response.json()))
            except Exception as exc:
                raise VideoGateError("Vision gate returned invalid JSON.") from exc
            if not isinstance(result, dict):
                raise VideoGateError("Vision gate returned a non-object result.")
            return read_video_gate_decision(result, format_name=format_name)
        raise VideoGateError("Vision gate request failed.")


__all__ = [
    "OpenAIVideoGateClient",
    "VIDEO_GATE_SCHEMA",
    "VideoGateDecision",
    "VideoGateError",
    "build_video_gate_prompt",
    "extract_video_sheets",
    "read_video_gate_decision",
]
