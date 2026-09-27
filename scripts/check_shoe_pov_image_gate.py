from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shoe_pov_image_gate import (  # noqa: E402
    ImageGateError,
    OpenAIImageGateClient,
    build_image_gate_prompt,
    image_guard_note,
    read_gate_decision,
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
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def request(self, method, url, *, headers, json, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        response = self.responses.pop(0)
        response.headers = response.headers or {}
        return response


def wrapped(result: dict) -> dict:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}


def check(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> None:
    passing = {
        "differences": [],
        "confidence": 0.97,
        "frame": {"shoesInHands": 0, "shoesOnFeet": 2, "shoesTotal": 2, "handsVisible": False, "feetVisible": True, "copiesProductPhoto": False},
    }
    decision = read_gate_decision(passing, format_name="worn")
    check(decision.passed and decision.action == "pass", "valid worn frame should pass")

    copied = {**passing, "frame": {**passing["frame"], "copiesProductPhoto": True}}
    bad = read_gate_decision(copied, format_name="worn")
    check(not bad.passed and bad.action == "rerender", "copied listing scene should rerender")
    check(any(reason.startswith("FRAME:") for reason in bad.reasons), "copied listing scene needs a FRAME reason")
    check(image_guard_note(format_name="worn", reasons=bad.reasons).startswith("FRAME GUARD"), "FRAME failure must select frame guard")

    uncertain = read_gate_decision({**copied, "confidence": 0.4}, format_name="worn")
    check(uncertain.action == "hold", "low-confidence failure must hold")

    held_wrong = read_gate_decision({**passing, "frame": {**passing["frame"], "shoesInHands": 1, "shoesOnFeet": 1, "feetVisible": True}}, format_name="held")
    check(any("mixes the poses" in reason for reason in held_wrong.reasons), "mixed pose must fail")

    transport = FakeTransport([FakeResponse(429, {"error": "rate limited"}, {"Retry-After": "1"}), FakeResponse(200, wrapped(passing))])
    waits: list[float] = []
    client = OpenAIImageGateClient(api_key="test", model="gpt-4o-mini", transport=transport, sleep=waits.append)
    result = client.inspect(product_images=[(b"product", "image/png")], rendered_image=(b"render", "image/jpeg"), seller_description="pink shoes", format_name="worn")
    check(result.passed, "fake provider response should pass")
    check(len(transport.calls) == 2 and waits == [1], "429 should retry once using Retry-After")
    request = transport.calls[-1]["json"]
    check(request["temperature"] == 0, "gate temperature must be zero")
    check(request["text"]["format"]["strict"] is True, "gate must use strict structured output")
    check(request["input"][0]["content"][-2]["text"] == "RENDERED STILL", "render label must precede final image")
    check(transport.calls[-1]["timeout"] == 120, "vision timeout must be explicit")

    try:
        OpenAIImageGateClient(api_key="").inspect(product_images=[(b"x", "image/png")], rendered_image=(b"y", "image/png"), seller_description="x", format_name="worn")
    except ImageGateError as exc:
        check("must hold" in str(exc), "missing credential must explicitly hold")
    else:
        raise AssertionError("missing credential unexpectedly passed")

    prompt = build_image_gate_prompt(format_name="held", seller_description="pink shoes")
    check("one shoe in each hand" in prompt and "copiesProductPhoto" in prompt, "held gate prompt is incomplete")

    print("PASS: valid worn still passes")
    print("PASS: deliberate copied listing scene returns FRAME reason and frame guard")
    print("PASS: low-confidence failures hold instead of rerendering")
    print("PASS: mixed worn/held pose fails")
    print("PASS: strict JSON, temperature 0, image labels, timeout, and one 429 retry")
    print("PASS: unavailable gate holds; fake transport only — zero vision credits spent")


if __name__ == "__main__":
    main()
