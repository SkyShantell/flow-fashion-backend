from __future__ import annotations

import re

__all__ = []

_HOOK_STYLE_RE = re.compile(r"\s*·\s*Hook\s*([1-5])\s*$", re.IGNORECASE)


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


# The hook picker stores only a tiny 1-5 selector inside the existing motion-style field.
# Keep it invisible to the actual video prompt builder by normalizing back to the base style.
# This avoids a database migration while still persisting the user's hook choice per product.
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
except Exception:
    # Never prevent the package from importing if prompt helpers change during a rolling deploy.
    pass
