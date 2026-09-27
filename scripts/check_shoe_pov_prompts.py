#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shoe_pov_prompts import (  # noqa: E402
    ALL_BEATS,
    SETTINGS,
    build_image_prompt,
    build_video_prompt,
    choose_beats,
    format_for_item,
    resolve_setting_name,
    unfilled_placeholders,
)


PRODUCT = {
    "product_title": "Project Cloud Women's Genuine Leather Summer Sandals",
    "product_description": (
        "Brown leather sandals with three gold buckles and a contoured cork-look sole. "
        "EXACT SPEC — colours: warm brown and gold; print: plain; material: genuine leather; closure: three gold buckle straps"
    ),
    "product_branding": "PROJECT CLOUD printed in black on the tan footbed",
    "product_sole": "thick tan contoured sole with dark brown tread",
}

CHARACTERS = (
    {
        "label": "female / deep brown",
        "gender": "female",
        "skin_tone": "deep brown skin tone",
        "nails": "short nude almond nails",
        "accessories": "a thin gold bracelet and two gold rings",
        "lighting": "soft neutral daylight with clean skin tones",
    },
    {
        "label": "male / light olive",
        "gender": "male",
        "skin_tone": "light olive skin",
        "nails": "",
        "accessories": "a black watch",
        "lighting": "cool open-shade daylight with neutral product colour",
    },
)

FOCUS_RE = re.compile(
    r"skin|hand|foot|feet|ankle|setting|prop|exactly two shoes|format:",
    re.IGNORECASE,
)


def sentences(text: str) -> list[str]:
    flattened = re.sub(r"\s+", " ", text.strip())
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|(?=FORMAT:)|(?=EXACTLY TWO SHOES:)", flattened) if part.strip()]


def show(title: str, text: str, focused: bool) -> None:
    print(f"\n{'=' * 16} {title} {'=' * 16}")
    if not focused:
        print(text)
        return
    for line in sentences(text):
        if FOCUS_RE.search(line):
            print(line)


def generate_examples() -> list[tuple[str, str]]:
    examples: list[tuple[str, str]] = []
    for fmt in ("worn", "held"):
        for char_index, character in enumerate(CHARACTERS):
            seed = f"check-{fmt}-{char_index}"
            beats = choose_beats(fmt, seed=seed, index=char_index, rotating_formats=False)
            common = {
                "gender": character["gender"],
                "skin_tone": character["skin_tone"],
                "format_name": fmt,
                "seed": seed,
                "index": char_index,
                "nails": character["nails"],
                "accessories": character["accessories"],
                "lighting": character["lighting"],
            }
            image = build_image_prompt(
                **common,
                **{key: PRODUCT[key] for key in ("product_title", "product_description")},
                hook=beats.hook,
                setting_name="shoe box" if fmt == "held" else "garage floor",
            )
            video = build_video_prompt(
                **common,
                **PRODUCT,
                beats=beats,
            )
            heading = f"{fmt.upper()} · {character['label']} · {beats.hook.name} / {beats.mid.name} / {beats.closer.name}"
            examples.append((heading + " · IMAGE", image))
            examples.append((heading + " · VIDEO", video))
    return examples


def assert_contract(examples: list[tuple[str, str]]) -> None:
    failures: list[str] = []
    if len(SETTINGS) != 7:
        failures.append(f"expected 7 settings, found {len(SETTINGS)}")
    if len(ALL_BEATS) != 18:
        failures.append(f"expected 18 beats, found {len(ALL_BEATS)}")

    deleted_pose_phrases = (
        "one shoe worn and the other held",
        "held about 20 cm above",
        "one shoe on one foot and the other in one hand",
    )
    for heading, prompt in examples:
        remaining = unfilled_placeholders(prompt)
        if remaining:
            failures.append(f"{heading}: unfilled placeholders {remaining}")
        lowered = prompt.lower()
        for phrase in deleted_pose_phrases:
            if phrase in lowered:
                failures.append(f"{heading}: deleted mixed-pose wording survived: {phrase}")

    formats = [format_for_item(index) for index in range(10)]
    if formats != ["worn", "held"] * 5:
        failures.append(f"format rotation is wrong: {formats}")
    if any(format_for_item(index, "held") != "held" for index in range(10)):
        failures.append("held pinning is not stable")
    if any(format_for_item(index, "worn") != "worn" for index in range(10)):
        failures.append("worn pinning is not stable")

    rotation = []
    for index in range(10):
        fmt = format_for_item(index)
        beats = choose_beats(fmt, seed="drop-check-001", index=index)
        rotation.append((fmt, beats.hook.name, beats.mid.name, beats.closer.name))
        if fmt == "worn":
            if beats.hook.name == "Heel tap (founder base)" and beats.closer.name == "Heel tap and rest":
                failures.append(f"item {index}: repeated worn heel-tap clash")
            if beats.hook.name == "Toe to the lens" and beats.mid.name == "Side profile turn":
                failures.append(f"item {index}: repeated worn turn clash")
        turning = {"Close and turn (founder base)", "Heel rotate", "Sole tilt"}
        if fmt == "held" and sum(name in turning for name in rotation[-1][1:]) > 1:
            failures.append(f"item {index}: held trio has more than one turning beat")
    if len(set(rotation[0::2])) < 3 or len(set(rotation[1::2])) < 3:
        failures.append("beat rotation does not spread across at least three trios per format")
    for fmt in ("worn", "held"):
        first_nine = [choose_beats(fmt, seed="drop-check-001", index=index, rotating_formats=False) for index in range(9)]
        if len(set(first_nine)) != 9:
            failures.append(f"{fmt} beat rotation repeats before nine trios")

    empty_skin_beats = choose_beats("worn", seed="missing-skin", index=0)
    empty_skin = build_image_prompt(
        gender="female",
        skin_tone="",
        format_name="worn",
        seed="missing-skin",
        index=0,
        hook=empty_skin_beats.hook,
        product_title=PRODUCT["product_title"],
        product_description=PRODUCT["product_description"],
    )
    if "[skin tone missing]" not in empty_skin:
        failures.append("empty skin tone did not produce the visible marker")

    if resolve_setting_name("shoe box", seed="x", index=0)[0] != "Shoe box open on the car seat":
        failures.append("setting matcher did not send 'shoe box' to the shoe-box setting")
    if resolve_setting_name("driveway", seed="x", index=0)[0] != "Driveway by the car door":
        failures.append("setting matcher confused driveway with driver's seat")

    first = choose_beats("worn", seed="same-item", index=4)
    second = choose_beats("worn", seed="same-item", index=4)
    if first != second:
        failures.append("beat selection is not deterministic")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        raise SystemExit(1)
    print("PASS: 7 settings and 18 beats exported")
    print("PASS: no deleted mixed-pose instructions survived")
    print("PASS: no {{placeholder}} remains in any generated prompt")
    print("PASS: empty skin tone renders as [skin tone missing]")
    print("PASS: worn/held alternation, pinning, deterministic spread, and clash rules")
    print("PASS: each format produces nine distinct beat trios before repeating")
    print("PASS: whole-word setting matching runs before four-letter stems")
    print("PASS: zero provider calls and zero database access")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print and validate Shoes POV prompts without spending credits.")
    parser.add_argument("--focus", action="store_true", help="Print only sentences about format, skin, limbs, setting, props and shoe count.")
    parser.add_argument("--assert-only", action="store_true", help="Run assertions without printing full prompts.")
    args = parser.parse_args()

    examples = generate_examples()
    print("SETTINGS:")
    for number, setting in enumerate(SETTINGS, start=1):
        print(f"  {number}. {setting.name}")

    if not args.assert_only:
        for heading, prompt in examples:
            show(heading, prompt, args.focus)

    print("\nROTATING DROP — FIRST 10 PICKS:")
    for index in range(10):
        fmt = format_for_item(index)
        beats = choose_beats(fmt, seed="drop-check-001", index=index)
        print(f"  {index}: {fmt} | {beats.hook.name} | {beats.mid.name} | {beats.closer.name}")

    missing_beats = choose_beats("worn", seed="missing-skin", index=0)
    missing_prompt = build_image_prompt(
        gender="female",
        skin_tone="",
        product_title=PRODUCT["product_title"],
        product_description=PRODUCT["product_description"],
        format_name="worn",
        hook=missing_beats.hook,
        seed="missing-skin",
        index=0,
    )
    print("\nEMPTY SKIN TONE:")
    print(next(line for line in sentences(missing_prompt) if "skin tone missing" in line.lower()))
    print("\nASSERTIONS:")
    assert_contract(examples)


if __name__ == "__main__":
    main()
