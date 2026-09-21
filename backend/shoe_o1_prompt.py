from __future__ import annotations

import re


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def shoe_o1_video_prompt(job, *, creator_profile: str = "Female", reference_count: int | None = None) -> str:
    """Shoe showcase prompt with the approved opener and product references."""
    if reference_count is None:
        reference_count = 1 + min(6, len([x for x in list(getattr(job, "selected_refs", None) or []) if str(x).strip()]))
    reference_count = max(1, min(7, int(reference_count or 1)))
    extra_tags = [f"@image_{i}" for i in range(2, reference_count + 1)]
    extra_rule = ""
    if extra_tags:
        extra_rule = (
            "Use " + ", ".join(extra_tags) +
            " only as shoe-detail references; ignore their people, poses and backgrounds. "
        )
    hand = "woman's hand" if str(creator_profile or "Female").lower().startswith("f") else "man's hand"

    prompt = _clean(f"""
        9:16 vertical, 5 seconds. Use the supplied approved Flow start image @image_1 as the exact first frame. Begin by perfectly matching @image_1, then move. {extra_rule}
        PRODUCT LOCK: preserve the exact shoe from @image_1 throughout: exact color, materials, silhouette, toe, sole/tread, heel, stitching, laces/closures, hardware, branding and proportions. No morphing, recoloring, redesign, duplicate shoes or invented features.
        MATERIAL LOCK: preserve exact grain/nap/weave/mesh, thickness, sheen, flex and material transitions; never substitute or smooth the visible texture.
        VISIBILITY: no face, no upper body, no extra people. Hand-held shots show no person above the forearm. On-foot shots stay tightly product-focused. Silent. No generated text, captions, subtitles, watermarks or added logos.
        Environment: same dark luxury car as @image_1, black leather and subtle gloss-black/chrome trim, moody ambient light. Premium editorial phone-camera realism; shoe stays large in frame.
        SHOT 1 · 0:00–0:02: Start exactly on @image_1. The {hand} lifts, tilts and repositions the shoe with clear controlled energy; subtle camera push/reframe.
        CUT · SHOT 2 · 0:02–0:04: New angle in the same car. Active hand-held rotation or on-foot angle, whichever best suits the shoe. Clearly show front-to-side profile and upper shape with stylish, realistic motion.
        CUT · SHOT 3 · 0:04–0:05: Close detail/hero finish. Reveal only a real visible feature—sole/tread, heel, stitching, tongue, laces, zipper, lining or texture—then finish on a strong three-quarter hero angle.
    """)
    if len(prompt) > 1700:
        raise RuntimeError(f"Shoe showcase prompt exceeded 1700 characters ({len(prompt)} chars).")
    return prompt
