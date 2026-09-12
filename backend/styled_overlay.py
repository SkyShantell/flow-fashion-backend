from __future__ import annotations

import io
import os
import subprocess
import tempfile
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont


FONT_TIKTOK = "/usr/local/share/fonts/TikTokSans.ttf"
FONT_SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
FONT_SERIF_ITALIC = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"
FONT_SERIF_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
FONT_SANS_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PRESETS = {
    "luxury_serif": {
        "label": "Luxury Serif",
        "description": "Italic serif headline with classic serif subline",
        "headline_font": FONT_SERIF_ITALIC,
        "subheadline_font": FONT_SERIF,
        "headline_scale": 0.050,
        "subheadline_scale": 0.031,
        "uppercase_subheadline": True,
    },
    "big_editorial": {
        "label": "Big Editorial",
        "description": "Large fashion-magazine serif with a refined second line",
        "headline_font": FONT_SERIF,
        "subheadline_font": FONT_SERIF,
        "headline_scale": 0.066,
        "subheadline_scale": 0.031,
        "uppercase_subheadline": False,
    },
    "serif_pop": {
        "label": "Serif + Pop",
        "description": "Editorial serif headline with a bold social-style second line",
        "headline_font": FONT_SERIF,
        "subheadline_font": FONT_SANS_BOLD,
        "headline_scale": 0.056,
        "subheadline_scale": 0.033,
        "uppercase_subheadline": False,
    },
    "clean_social": {
        "label": "Clean Social",
        "description": "Bold clean headline with simple TikTok-style supporting text",
        "headline_font": FONT_SANS_BOLD,
        "subheadline_font": FONT_TIKTOK,
        "headline_scale": 0.042,
        "subheadline_scale": 0.030,
        "uppercase_subheadline": False,
    },
}

COLORS = {
    "white": "#FFFFFF",
    "cream": "#F7F0E7",
    "soft_pink": "#F0B6DE",
    "warm_brown": "#B5794D",
    "black": "#111111",
}

PLACEMENTS = {
    "upper": 0.24,
    "middle": 0.46,
    "lower": 0.68,
}

_TWEMOJI_CACHE = Path("/tmp/flow_twemoji")
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"


def overlay_options() -> dict:
    return {
        "presets": [
            {"id": key, "label": value["label"], "description": value["description"]}
            for key, value in PRESETS.items()
        ],
        "colors": [{"id": key, "hex": value} for key, value in COLORS.items()],
        "placements": [
            {"id": "upper", "label": "Upper"},
            {"id": "middle", "label": "Middle"},
            {"id": "lower", "label": "Lower"},
        ],
    }


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    chosen = path if path and os.path.exists(path) else FONT_SERIF
    return ImageFont.truetype(chosen, max(12, int(size)))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, stroke_width: int = 0) -> tuple[int, int]:
    if not text:
        return 0, 0
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return max(0, box[2] - box[0]), max(0, box[3] - box[1])


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    path: str,
    desired_size: int,
    max_width: int,
    stroke_width: int = 0,
) -> ImageFont.FreeTypeFont:
    size = max(18, int(desired_size))
    while size > 18:
        font = _font(path, size)
        width, _ = _text_size(draw, text, font, stroke_width)
        if width <= max_width:
            return font
        size -= 2
    return _font(path, 18)


def _emoji_codepoint(token: str) -> str:
    # Twemoji filenames omit the emoji presentation selector FE0F.
    return "-".join(f"{ord(ch):x}" for ch in token if ord(ch) != 0xFE0F)


def _emoji_asset(token: str, size: int) -> Image.Image | None:
    token = str(token or "").strip()
    if not token:
        return None
    code = _emoji_codepoint(token)
    if not code:
        return None
    try:
        _TWEMOJI_CACHE.mkdir(parents=True, exist_ok=True)
        cache_path = _TWEMOJI_CACHE / f"{code}.png"
        if not cache_path.exists():
            response = requests.get(f"{_TWEMOJI_BASE}/{code}.png", timeout=8)
            response.raise_for_status()
            if len(response.content) < 100:
                return None
            cache_path.write_bytes(response.content)
        image = Image.open(cache_path).convert("RGBA")
        target = max(18, int(size))
        image.thumbnail((target, target), Image.Resampling.LANCZOS)
        return image
    except Exception:
        return None


def _emoji_tokens(value: str) -> list[str]:
    # Space-separated emoji gives predictable multi-codepoint handling without depending
    # on a separate grapheme library. Typical input: "🤎 🍂 🍁 🐆".
    return [x for x in str(value or "").strip().split() if x]


def _inline_headline(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    color: str,
    prefix: str,
    suffix: str,
    emoji_size: int,
    stroke_width: int,
) -> int:
    prefix_images = [img for token in _emoji_tokens(prefix) if (img := _emoji_asset(token, emoji_size)) is not None]
    suffix_images = [img for token in _emoji_tokens(suffix) if (img := _emoji_asset(token, emoji_size)) is not None]
    text_w, text_h = _text_size(draw, text, font, stroke_width)
    gap = max(7, int(emoji_size * 0.14))
    group_w = text_w
    if prefix_images:
        group_w += sum(img.width for img in prefix_images) + gap * len(prefix_images)
    if suffix_images:
        group_w += sum(img.width for img in suffix_images) + gap * len(suffix_images)
    x = max(20, int((canvas.width - group_w) / 2))
    mid_y = y + max(text_h, emoji_size) // 2

    for img in prefix_images:
        canvas.alpha_composite(img, (x, int(mid_y - img.height / 2)))
        x += img.width + gap

    draw.text(
        (x, y),
        text,
        font=font,
        fill=color,
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 90),
    )
    x += text_w + (gap if suffix_images else 0)

    for index, img in enumerate(suffix_images):
        canvas.alpha_composite(img, (x, int(mid_y - img.height / 2)))
        x += img.width + (gap if index < len(suffix_images) - 1 else 0)

    return max(text_h, emoji_size)


def _video_size(path: Path) -> tuple[int, int]:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        value = (proc.stdout or "").strip().splitlines()[0]
        width, height = [int(x) for x in value.split("x", 1)]
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return 1080, 1920


def render_styled_overlay(
    video_bytes: bytes,
    *,
    headline: str,
    subheadline: str = "",
    preset: str = "luxury_serif",
    emoji_prefix: str = "",
    emoji_suffix: str = "",
    headline_color: str = "white",
    subheadline_color: str = "white",
    placement: str = "middle",
) -> bytes:
    if not video_bytes:
        raise RuntimeError("No video bytes were supplied to the styled overlay renderer.")

    style = PRESETS.get(str(preset or ""), PRESETS["luxury_serif"])
    headline = " ".join(str(headline or "").split()).strip()
    subheadline = " ".join(str(subheadline or "").split()).strip()
    if not headline and not subheadline:
        raise RuntimeError("Add at least one line of text before sending to FFmpeg.")
    if style.get("uppercase_subheadline") and subheadline:
        subheadline = subheadline.upper()

    h_color = COLORS.get(str(headline_color or "white"), COLORS["white"])
    s_color = COLORS.get(str(subheadline_color or "white"), COLORS["white"])
    placement_ratio = PLACEMENTS.get(str(placement or "middle"), PLACEMENTS["middle"])

    with tempfile.TemporaryDirectory(prefix="flow_styled_overlay_") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.mp4"
        overlay_path = root / "overlay.png"
        output_path = root / "output.mp4"
        input_path.write_bytes(video_bytes)

        width, height = _video_size(input_path)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        max_width = int(width * 0.90)
        stroke_width = max(1, int(height * 0.0008))

        headline_font = _fit_font(
            draw,
            headline,
            str(style["headline_font"]),
            int(height * float(style["headline_scale"])),
            max_width,
            stroke_width,
        )
        sub_font = _fit_font(
            draw,
            subheadline,
            str(style["subheadline_font"]),
            int(height * float(style["subheadline_scale"])),
            max_width,
            stroke_width,
        ) if subheadline else None

        _, headline_h = _text_size(draw, headline, headline_font, stroke_width)
        _, sub_h = _text_size(draw, subheadline, sub_font, stroke_width) if sub_font else (0, 0)
        emoji_size = max(24, int(getattr(headline_font, "size", 50) * 0.78))
        headline_block_h = max(headline_h, emoji_size if (emoji_prefix or emoji_suffix) else headline_h)
        line_gap = max(8, int(height * 0.006)) if subheadline else 0
        total_h = headline_block_h + line_gap + sub_h
        top_y = int(height * placement_ratio - total_h / 2)
        top_y = max(int(height * 0.08), min(top_y, height - total_h - int(height * 0.08)))

        used_h = _inline_headline(
            canvas,
            draw,
            y=top_y,
            text=headline,
            font=headline_font,
            color=h_color,
            prefix=emoji_prefix,
            suffix=emoji_suffix,
            emoji_size=emoji_size,
            stroke_width=stroke_width,
        )

        if subheadline and sub_font:
            sub_w, _ = _text_size(draw, subheadline, sub_font, stroke_width)
            sub_x = max(20, int((width - sub_w) / 2))
            draw.text(
                (sub_x, top_y + used_h + line_gap),
                subheadline,
                font=sub_font,
                fill=s_color,
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 90),
            )

        canvas.save(overlay_path, "PNG")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-loop", "1", "-i", str(overlay_path),
            "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not output_path.exists():
            error = (proc.stderr or proc.stdout or "FFmpeg overlay failed.")[-3000:]
            raise RuntimeError(f"FFmpeg styled overlay failed: {error}")
        return output_path.read_bytes()
