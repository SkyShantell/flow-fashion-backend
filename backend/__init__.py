from __future__ import annotations

import re

__all__ = []

_HOOK_STYLE_RE = re.compile(r"\s*·\s*Hook\s*([1-5])\s*$", re.IGNORECASE)

_MATERIAL_TEXTURE_LOCK = (
    "MATERIAL + TEXTURE LOCK: Preserve the exact visible material from the product references. "
    "Match surface texture, weave/knit/nap/grain/mesh, thickness, stiffness or flex, sheen, stretch, drape, wrinkles, stitching, edge construction and material transitions. "
    "Do not substitute a visually similar material, smooth or flatten the texture, or change how the material behaves under light and movement. "
    "If a reference or listing explicitly identifies the material, treat that material as authoritative; otherwise do not invent a fiber, leather type, or composition that is not supported by the references."
)


def hook_index_from_motion_style(style: str | None) -> int:
    match = _HOOK_STYLE_RE.search(str(style or ""))
    if not match:
        return 1
    try:
        return max(1, min(5, int(match.group(1))))
    except Exception:
        return 1


def strip_hook_from_motion_style(style: str | None) -> str:
    return _HOOK_STYLE_RE.sub("", str(style or "")).strip()


def _with_material_texture_lock(prompt: str) -> str:
    value = str(prompt or "").strip()
    if not value or "MATERIAL + TEXTURE LOCK:" in value:
        return value
    return f"{value} {_MATERIAL_TEXTURE_LOCK}"


# Prompt functions are wrapped here because backend.__init__ runs before backend.tasks/API
# import helpers from backend.prompts. That makes the same material/texture lock apply to
# Flow clothing images, clothing videos, shoe stills, and legacy shoe video helpers without
# duplicating the rule throughout the large prompt file.
try:
    from . import prompts as _prompts

    _original_normalize_motion_style = _prompts.normalize_motion_style
    _base_motion_styles = list(_prompts.MOTION_STYLES)

    for _style in _base_motion_styles:
        if _style == _prompts.SHOE_SHOWCASE_MOTION:
            continue
        for _index in range(1, 6):
            _encoded = f"{_style} · Hook {_index}"
            if _encoded not in _prompts.MOTION_STYLES:
                _prompts.MOTION_STYLES.append(_encoded)

    def _normalize_motion_style_with_hook(style: str | None, creator_profile: str = "Male") -> str:
        return _original_normalize_motion_style(strip_hook_from_motion_style(style), creator_profile)

    _prompts.normalize_motion_style = _normalize_motion_style_with_hook

    for _prompt_name in (
        "image_prompt",
        "video_prompt",
        "shoe_showcase_image_prompt",
        "shoe_showcase_video_prompt",
        "shoe_editorial_frame_prompt",
        "shoe_editorial_clip_prompt",
    ):
        _original_prompt = getattr(_prompts, _prompt_name, None)
        if not callable(_original_prompt):
            continue

        def _locked_prompt(*args, __original=_original_prompt, **kwargs):
            return _with_material_texture_lock(__original(*args, **kwargs))

        setattr(_prompts, _prompt_name, _locked_prompt)
except Exception:
    # Never prevent the package from importing if prompt helpers change during a rolling deploy.
    pass
