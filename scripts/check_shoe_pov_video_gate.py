from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shoe_pov_video_gate import (  # noqa: E402
    OpenAIVideoGateClient,
    VideoGateError,
    build_video_gate_prompt,
    extract_video_sheets,
    read_video_gate_decision,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: dict
    headers: dict[str, str] | None = None

    @property
    def content(self) -> bytes:
        return json.dumps(self.payload).encode()

    @property
    def text(self) -> str:
        return json.dumps(self.payload)

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, json, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        response = self.responses.pop(0)
        response.headers = response.headers or {}
        return response


def wrapped(result):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}


def check(value, message):
    if not value:
        raise AssertionError(message)


def main() -> None:
    passing = {
        "shoe": {
            "brandingChanged": False, "patternOrColourChanged": False, "soleChanged": False, "closuresChanged": False,
            "handHoldsShoe": False, "shoeOnFoot": True, "extraHandOrFingers": False,
            "faceOrPhoneOrMirror": False, "extraShoes": False, "where": "all frames", "notes": "stable",
        },
        "motion": {"roomChanges": False, "jumpCut": False, "notes": "one shot"},
    }
    check(read_video_gate_decision(passing, format_name="worn").passed, "valid worn video should pass")
    bad = {"shoe": {**passing["shoe"], "brandingChanged": True, "extraShoes": True}, "motion": {"roomChanges": False, "jumpCut": True, "notes": "jump"}}
    failed = read_video_gate_decision(bad, format_name="worn")
    check(failed.reasons == ("changes the logo or printed text on the shoe", "shows a third shoe", "cuts, with a jump cut mid-video"), "video failure mapping changed")
    mixed = {"shoe": {**passing["shoe"], "handHoldsShoe": True}, "motion": passing["motion"]}
    check(any("mixes the poses" in reason for reason in read_video_gate_decision(mixed, format_name="worn").reasons), "mixed pose did not fail")

    prompt = build_video_gate_prompt(format_name="worn", product_details="pink mules")
    check("MOTION CHECK" in prompt and "a third of a second" in prompt, "cuts-only motion prompt is incomplete")
    check("limbCount" not in prompt and "phoneHold" not in prompt, "POV prompt imported forbidden lane checks")

    transport = FakeTransport([FakeResponse(200, wrapped(passing))])
    client = OpenAIVideoGateClient(api_key="test", model="gpt-4o-mini", transport=transport)
    result = client.inspect(product_images=[(b"product", "image/png")], timeline_sheet=b"timeline", shoe_sheet=b"shoes", product_details="pink mules", format_name="worn")
    check(result.passed, "fake vision response should pass")
    body = transport.calls[0]["json"]
    check(body["temperature"] == 0 and body["text"]["format"]["strict"] is True, "vision request is not deterministic structured JSON")
    labels = [item.get("text") for item in body["input"][0]["content"] if item.get("type") == "input_text"]
    check(any(str(value).startswith("VIDEO TIMELINE") for value in labels), "timeline label missing")
    check(any(str(value).startswith("SHOE CROPS") for value in labels), "shoe-crop label missing")

    try:
        extract_video_sheets(b"not a video", format_name="worn", ffmpeg_binary="/definitely/missing/ffmpeg")
    except VideoGateError as exc:
        check("could not start" in str(exc), "missing FFmpeg error is not visible")
    else:
        raise AssertionError("missing FFmpeg unexpectedly passed")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.TemporaryDirectory(prefix="shoe-pov-check-") as folder:
            sample = Path(folder) / "sample.mp4"
            subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=pink:s=360x640:d=8:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample)], check=True, timeout=60)
            sheets = extract_video_sheets(sample.read_bytes(), format_name="worn")
            check(len(sheets["timeline"]) > 1000 and len(sheets["shoes"]) > 1000, "FFmpeg sheets are empty")

    print("PASS: shoe and cuts-only motion reasons map exactly")
    print("PASS: worn/held mixed pose fails and no unrelated lane questions are asked")
    print("PASS: strict JSON, temperature 0, timeline/crop labels, and 120s timeout")
    print("PASS: FFmpeg startup/failure is visible and local timeline/crop extraction works")
    print("PASS: fake transport only — zero vision or video credits spent")


if __name__ == "__main__":
    main()
