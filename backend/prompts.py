from __future__ import annotations

import re

SCENES = {
    "Modern apartment mirror": "a realistic modern upscale apartment with a large full-length mirror, warm ceiling lighting, neutral furniture, and believable lived-in details",
    "Walk-in closet": "a realistic upscale walk-in closet with dark wood shelving, folded clothes, soft warm recessed lighting, and a large full-length mirror",
    "Luxury bathroom mirror": "a realistic upscale bathroom with dark stone surfaces, a large clean mirror, warm ceiling light, and subtle hotel-like details",
    "Penthouse at night": "a realistic modern penthouse at night with floor-to-ceiling windows, city lights, warm recessed lighting, polished floor, and a large full-length mirror",
    "Casual bedroom mirror": "a believable lived-in modern bedroom with a full-length mirror, neatly made bed, dresser, a few normal personal items, and soft natural or warm indoor light",
    "Modern hotel room mirror": "a believable modern hotel room with a full-length mirror, neutral bedding, luggage bench, warm lamps, and clean but not overly staged details",
    "Warm living room mirror": "a realistic warm apartment living room with a full-length mirror, neutral sofa, wood furniture, a few books or normal decor items, and soft natural indoor light",
    "Apartment entryway mirror": "a realistic contemporary apartment entryway with a full-length mirror, console table, shoes or a small bag near the door, warm practical lighting, and normal lived-in detail",
}

# Academy names intentionally preserve the terminology from the user's AI Shop Academy PDF.
MALE_ACADEMY_STYLES = [
    "Academy — Boss / Calm",
    "Academy — High-Energy",
    "Academy — Rapid-Fire / Flashy",
]
FEMALE_ACADEMY_STYLES = [
    "Academy — Elegant / Calm",
    "Academy — High-Energy",
    "Academy — Flash-Lit",
]
CUSTOM_MOTION_STYLES = [
    "Custom — Casual UGC",
    "Custom — Fit Check",
    "Custom — Detail Focus",
]
# Keep old labels valid so existing batches created before V6 continue working.
LEGACY_MOTION_STYLES = ["Calm", "Casual UGC", "Fit Check", "Detail Focus", "Streetwear", "High-energy", "Flashy"]
MOTION_STYLES = list(dict.fromkeys(MALE_ACADEMY_STYLES + FEMALE_ACADEMY_STYLES + CUSTOM_MOTION_STYLES + LEGACY_MOTION_STYLES))


def clean_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def face_block_rule() -> str:
    return (
        "CRITICAL FACE RULE: this is a mirror selfie and the smartphone must stay directly in front of the model's face, blocking the face completely. "
        "No eyes, eyebrows, nose, lips, cheeks, jawline, facial skin, or recognizable facial features may be visible around the phone. "
        "Hair may be visible, but the face itself must be 100% obscured by the phone. Do not lower, offset, tilt away, or move the phone aside."
    )


def default_motion_style(creator_profile: str = "Male") -> str:
    return "Academy — Elegant / Calm" if str(creator_profile or "").lower().startswith("f") else "Academy — Boss / Calm"


def normalize_motion_style(style: str | None, creator_profile: str = "Male") -> str:
    """Map pre-V6 labels onto the closest Academy/custom preset without breaking old batches."""
    profile = str(creator_profile or "Male").lower()
    value = str(style or "").strip()
    if value in MALE_ACADEMY_STYLES + FEMALE_ACADEMY_STYLES + CUSTOM_MOTION_STYLES:
        # A batch can be switched between creator genders after creation. If an Academy label belongs
        # to the other gender, use the equivalent energy level for the active creator profile.
        if profile.startswith("f") and value == "Academy — Boss / Calm":
            return "Academy — Elegant / Calm"
        if not profile.startswith("f") and value == "Academy — Elegant / Calm":
            return "Academy — Boss / Calm"
        if profile.startswith("f") and value == "Academy — Rapid-Fire / Flashy":
            return "Academy — High-Energy"
        if not profile.startswith("f") and value == "Academy — Flash-Lit":
            return "Academy — Boss / Calm"
        return value

    legacy = value.lower()
    if legacy == "calm" or not legacy:
        return default_motion_style(creator_profile)
    if legacy == "high-energy":
        return "Academy — High-Energy"
    if legacy == "flashy":
        return "Academy — Flash-Lit" if profile.startswith("f") else "Academy — Rapid-Fire / Flashy"
    if legacy == "casual ugc":
        return "Custom — Casual UGC"
    if legacy == "fit check":
        return "Custom — Fit Check"
    if legacy == "detail focus":
        return "Custom — Detail Focus"
    if legacy == "streetwear":
        return "Custom — Casual UGC"
    return default_motion_style(creator_profile)


def image_prompt(job, *, scene: str, refs_count: int, creator_profile: str = "Male") -> str:
    focus = getattr(job, "focus", None) or "outfit"
    product = getattr(job, "product_name", None) or "clothing"
    ref_mentions = " ".join(f"@reference_{i}" for i in range(2, refs_count + 1))
    scene_text = SCENES.get(scene, SCENES["Modern apartment mirror"])
    profile = (creator_profile or "Male").strip().lower()

    if focus == "pants":
        framing = "full-length, from phone/hair level down to toes, with the bottoms/pants clearly visible from waist to hem"
        focus_rule = "The bottoms/pants are the hero. Make the waist, fit through hips and thighs, leg shape, pockets, seams, length and hem easy to judge."
        pose = "free hand naturally touching the waistband, pocket, or thigh, slight weight shift"
        fallback = "Keep the top simple and neutral so it does not compete with the pants."
    elif focus == "hoodie":
        framing = "from phone/hair level through knees, with the hoodie or jacket taking most of the frame"
        focus_rule = "The hoodie/jacket is the hero. Make the hood, zipper or placket, chest details, sleeves, cuffs, pockets, hem, fabric and relaxed fit easy to judge."
        pose = "free hand naturally touching the zipper, chest, cuff, pocket, or hem"
        fallback = "Pair it with simple neutral bottoms if matching bottoms are not part of the references."
    elif focus == "shirt":
        framing = "from phone/hair level through knees, with the shirt/top taking most of the frame"
        focus_rule = "The shirt/top is the hero. Make the neckline, chest graphic, sleeves, fit, hem, fabric, texture and silhouette easy to judge."
        pose = "free hand naturally touching the chest, collar, sleeve, or hem"
        fallback = "If no matching bottoms are sold in the references, pair the top with simple neutral bottoms and clean sneakers."
    elif focus == "shoes":
        framing = "full-length down to toes, with one foot slightly forward so the footwear is clear and correctly shaped"
        focus_rule = "The footwear is the hero. Keep the full body visible, but make the shoes unobstructed, correctly sized and easy to see from front and slight side angle."
        pose = "one foot slightly forward, grounded natural stance"
        fallback = "Use simple neutral clothing that does not compete with the footwear."
    elif focus == "handbag":
        framing = "full-length or three-quarter mirror selfie with the bag visible at hip/torso level and true to size"
        focus_rule = "The handbag/bag is the hero. Preserve exact size, strap, handle, logo placement if present, hardware, shape, texture, stitching, and color."
        pose = "free hand holding or lightly lifting the bag at hip level"
        fallback = "Use simple neutral clothing that does not compete with the bag."
    else:
        framing = "full-length head-to-toe mirror selfie, showing the complete outfit clearly"
        focus_rule = "The full outfit is the hero. Show top and bottom together clearly with believable fit, drape and proportions."
        pose = "free hand resting at side, slight weight shift, relaxed confident stance"
        fallback = "Do not substitute, redesign, or add a different matching piece."

    gender_line = "Fit male model" if profile.startswith("m") else "Elegant female model"
    back = "If the references clearly show an important back design, preserve it accurately even though the first frame is front/three-quarter." if getattr(job, "back_design", False) else ""
    revision = getattr(job, "regen_instruction", None)
    revision_rule = f"USER REVISION REQUEST: {revision}. Apply this while preserving the exact product and face-block rule." if revision else ""

    return clean_prompt(f"""
    Ultrarealistic 9:16 mirror-selfie try-on image, authentic iPhone 16 Pro UGC look. {gender_line}. {framing}.
    Setting: {scene_text}. The setting must look like a real usable space, not a studio set, catalog backdrop, or CGI room.
    The model holds a modern smartphone directly in front of the face. {face_block_rule()}
    The model wears the exact same {product} as the product references {ref_mentions} -- preserve exact colors, graphics, pattern, fabric texture, fit, cut, seams, straps, labels, proportions and product-specific details. {focus_rule} {fallback} {back} {revision_rule}
    Pose: {pose}. Natural mirror selfie stance, realistic body balance, no sexual pose.
    Realism: authentic iPhone 16 Pro UGC photo, natural skin texture where visible, realistic hands and fingers, believable fabric folds and seams, subtle sensor grain, mild phone-camera compression, no studio lighting, no glossy catalog look, no extra people, no duplicated limbs, no morphing, no text overlays, no prices, no added logos, no watermarks.
    CRITICAL: preserve every detail of the product from the reference photo. NO FACE SHOWN. PHONE BLOCKS THE FACE COMPLETELY.
    """)


def _male_academy_beats(style: str) -> list[str]:
    if style == "Academy — High-Energy":
        return [
            "Beat 1 — Hook: fast confident free-hand greeting, then extend the free arm to show the fit; phone hand stays steady.",
            "Beat 2 — Vibe & fit: bouncy confident power walk toward the mirror with controlled up-and-down energy; free hand travels from neck/chest toward waist to show the design; use a slight speed-ramp feel without losing product clarity.",
            "Beat 3 — Detail reveal: point to the chest/graphic, then briefly pinch and release the fabric to show texture.",
            "Beat 4 — Range of motion: raise the free arm overhead and reach across toward the opposite shoulder, hold briefly, then lower.",
            "Beat 5 — View & athletic cut: slow side turn and a short hold so the silhouette is readable.",
            "Beat 6 — Finish: quick salute, then free hand settles into a pocket or relaxed finish; confident shoulder movement and small nod.",
        ]
    if style == "Academy — Rapid-Fire / Flashy":
        return [
            "Beat 1 — Hook: quick dance-style free-hand/shoulder opener, then extend the free arm to show the fit.",
            "Beat 2 — Vibe & fit: bouncy power walk toward the mirror with dramatic speed-ramp energy; free hand moves neck-to-waist to reveal the design. Keep the phone over the face even if the body movement is fast.",
            "Beat 3 — Rapid detail reveal: adjust an accessory if present, pull the hem to show the cut, then do a fast fabric pinch-release and a brief thumbs-up.",
            "Beat 4 — Flex: quick bicep flex, then point to the graphic or hero detail.",
            "Beat 5 — View: faster side turn with a brief hold at the end.",
            "Beat 6 — Finish: confident double nod and quick salute while the product remains centered and readable.",
        ]
    return [
        "Beat 1 — Hook: subtle bicep flex, then extend the free arm to show the fit; calm and controlled.",
        "Beat 2 — Vibe & fit: slow powerful walk toward the mirror; halfway in, run the free hand from neck/chest toward the waist to show the design; slight speed-ramp feel only.",
        "Beat 3 — Detail reveal: briefly rub or pinch the fabric between thumb and forefinger to show texture, then release.",
        "Beat 4 — Range of motion: raise the free arm overhead and reach across toward the opposite shoulder; hold briefly, then lower the hand toward the hip.",
        "Beat 5 — View & cut: slow side turn and hold so the fit is easy to judge.",
        "Beat 6 — Finish: free hand on hip and a small confident double nod; keep the product as the focus.",
    ]


def _female_academy_beats(style: str) -> list[str]:
    if style == "Academy — High-Energy":
        return [
            "Beat 1 — Hook: fast confident free-hand greeting, then hand to waist with a small bounce; phone hand steady.",
            "Beat 2 — Vibe: bouncy confident walk toward the mirror; free hand travels from collarbone down the center line toward the hip; use a dynamic speed-ramp feel while keeping the garment clear.",
            "Beat 3 — Quick detail reveal: touch an accessory if present, trace the neckline, give the hem a light pull to show fit, then a gentle hair flick if hair is visible.",
            "Beat 4 — Fit & mobility: controlled high-step in place, brief hold, then point to or touch the main graphic/detail.",
            "Beat 5 — View & cohesion: slow side turn with a subtle speed-ramp feel and a short hold.",
            "Beat 6 — Finish: hand on hip, shift weight to one side, then a confident nod while the phone still blocks the face.",
        ]
    if style == "Academy — Flash-Lit":
        return [
            "Beat 1 — Hook: soft confident free-hand greeting, then hand to waist. Keep a constant steady phone/LED glow with no flicker, pulse, or strobe.",
            "Beat 2 — Vibe: slow confident walk toward the mirror; free hand travels from collarbone toward hip along the outfit center line; slight speed-ramp feel, steady glow.",
            "Beat 3 — Detail reveal: gentle hair flick if visible, then lightly touch the fabric so the product catches the steady light without blowing out graphics or text.",
            "Beat 4 — Fit & mobility: slow high-step in place, brief hold, then free hand runs down the outside of the thigh/fabric.",
            "Beat 5 — View & cohesion: slow side turn and hold; keep illumination consistent and product details sharp.",
            "Beat 6 — Finish: hand on hip, weight shift, confident nod; phone remains over the face and light remains steady.",
        ]
    return [
        "Beat 1 — Hook: soft graceful free-hand greeting, then rest the free hand at the waist; calm and controlled.",
        "Beat 2 — Vibe: slow confident walk toward the mirror; halfway in, run the free hand from collarbone toward hip along the center line of the outfit; slight speed-ramp feel only.",
        "Beat 3 — Detail reveal: gentle hair flick if hair is visible, then let the product catch soft realistic light.",
        "Beat 4 — Fit & mobility: slow high-step in place, brief hold, then run the free hand down the outside of the thigh over the fabric.",
        "Beat 5 — View & cohesion: slow side turn and hold so the fit remains readable.",
        "Beat 6 — Finish: hand on hip, shift weight to one side, then a confident nod while the phone still blocks the face.",
    ]


def _handbag_academy_beats() -> list[str]:
    # The Academy PDF labels this prompt female; the object-focused sequence is kept generic so it
    # can be used with either saved creator profile without changing the selling beats.
    return [
        "Beat 1 — Hook: face the mirror with the bag low at the side and give it a small confident lift.",
        "Beat 2 — Full outfit & bag: take a slow step toward the mirror with the bag beside the thigh so the full look and bag read together.",
        "Beat 3 — Hardware detail: raise the bag to waist height and angle its front toward the mirror so clasp, flap, handle, stitching and hardware are sharp.",
        "Beat 4 — Size proof: hold the bag against the hip briefly to show true scale against the body; no shrinking, swelling, bending, or shape change.",
        "Beat 5 — Styling: shift weight to one leg and let the bag hang naturally from its handle/strap while turning the shoulder slightly.",
        "Beat 6 — Finish: confident nod and one final lift toward the mirror, holding the bag centered and sharp.",
    ]


def _shoes_academy_beats() -> list[str]:
    return [
        "Beat 1 — Hook: stand grounded with both feet level and the shoes clearly visible.",
        "Beat 2 — Profile: lift one heel while the toe stays down and twist the foot slightly to show the side profile, smooth and controlled.",
        "Beat 3 — Detail: settle the foot, then press the toe down and lift the heel briefly so the upper, sole and shape are readable.",
        "Beat 4 — Step: take one small step forward onto the toe, then settle both feet level again with a subtle weight shift.",
        "Beat 5 — View: angle the feet slightly toward the mirror for a clean final product view.",
        "Beat 6 — Finish: grounded stance with both shoes framed clearly, then a small confident nod while the phone still blocks the face.",
    ]


def _fit_reveal_beat() -> str:
    return (
        "Beat 2 — Academy back/side fit reveal: begin slightly angled so the side/back line is visible in the mirror; "
        "make a slow partial turn, not a full spin, while the free hand lightly adjusts the fabric at the waist and moves toward the hip; "
        "finish with a natural weight shift and the side/back fit still visible. Keep it tasteful, natural, and product-focused."
    )


def _needs_back_side_fit(job) -> bool:
    focus = str(getattr(job, "focus", None) or "").lower()
    name = re.sub(r"[^a-z0-9]+", " ", str(getattr(job, "product_name", None) or "").lower())
    return focus == "pants" or bool(getattr(job, "back_design", False)) or any(token in name.split() for token in ("dress", "skirt", "pants", "jeans", "jean", "trousers", "trouser"))


def _custom_modifier(style: str) -> str:
    if style == "Custom — Fit Check":
        return "Prioritize clear front and three-quarter fit views with one slow turn and one simple product-touch gesture so fit and silhouette are easy to judge."
    if style == "Custom — Detail Focus":
        return "Prioritize one or two close product-detail gestures with the free hand, then a restrained side angle; avoid fast movement."
    return "Keep the movement loose and ordinary, like a quick phone fit-check posted by a real creator: small weight shifts, one casual gesture, no choreographed performance."


def _custom_product_motion(focus: str, profile: str) -> str:
    if focus == "shoes":
        return "Start full-body with one foot slightly forward. Shift weight between feet, make one small natural step, angle the feet to show front and side, then finish grounded with the shoes unobstructed."
    if focus == "handbag":
        return "Start with the bag at hip level. Lightly lift it, angle it toward the mirror to show shape, strap and hardware, take one small step, then let it rest naturally at true size."
    if focus == "pants":
        return "Start full-body front view. Gesture to waistband, pocket and upper thigh, take a small half-step and quarter-turn to show side fit and leg shape, then finish angled with one leg forward."
    if focus == "hoodie":
        return "Start front-facing. Touch the zipper/chest once, brush a cuff or pocket, take a small step, then make a natural quarter-turn so hood, sleeve shape and side fit stay visible."
    if focus == "shirt":
        return "Start front view. Brush the chest, collar, sleeve or hem, shift weight, make a natural quarter-turn, then finish relaxed front/three-quarter with the top clearly visible."
    if str(profile or "Male").lower().startswith("f"):
        return "Start in a soft confident full-body pose, make one small greeting, place the free hand at the waist, take one slow step, adjust fabric lightly, make a side turn, then finish with a small nod."
    return "Start centered full-body, make one subtle confident free-hand opener, take one slow step, lightly pinch the fabric, make a side turn, then finish with a small nod."


def _academy_motion(job, style: str, creator_profile: str) -> tuple[str, str]:
    focus = str(getattr(job, "focus", None) or "outfit").lower()
    profile = str(creator_profile or "Male").lower()

    if focus == "handbag":
        title = "Academy product-specific — Handbag"
        beats = _handbag_academy_beats()
        product_rule = "Bag is the hero. Preserve exact texture, stitching, flap/closure, chain or strap, hardware, color, size and proportions in every frame."
    elif focus == "shoes":
        title = "Academy product-specific — Shoes"
        beats = _shoes_academy_beats()
        product_rule = "Shoes are the hero. Preserve exact color, shape, material, sole, laces/closures and details; full-length framing must keep both shoes readable."
    else:
        title = style
        beats = _female_academy_beats(style) if profile.startswith("f") else _male_academy_beats(style)
        product_rule = "Preserve the exact approved product/outfit from the start image; graphics, text, fabric, color, fit and construction must remain stable."
        if _needs_back_side_fit(job):
            beats[1] = _fit_reveal_beat()

    return title, f"{product_rule} " + " ".join(beats)


def video_prompt(job, *, creator_profile: str = "Male", video_style: str = "Academy — Boss / Calm") -> str:
    focus = str(getattr(job, "focus", None) or "outfit").lower()
    style = normalize_motion_style(video_style, creator_profile)
    is_academy = style.startswith("Academy —")

    if is_academy:
        motion_title, motion = _academy_motion(job, style, creator_profile)
        motion_header = (
            f"MOTION PRESET: {motion_title}. This follows the AI Shop Academy movement structure. "
            "The numbered beats are sequential moments inside ONE continuous clip; do not create cuts or separate scenes."
        )
    else:
        motion_header = f"MOTION PRESET: {style}. Custom fallback motion, not an Academy PDF preset."
        motion = f"{_custom_modifier(style)} {_custom_product_motion(focus, creator_profile)}"

    return clean_prompt(f"""
    8-second vertical 1080x1920 target, 9:16 TikTok UGC mirror try-on. One continuous take, multi-shot OFF, audio OFF, no cuts.
    Use the supplied approved start image as the exact first frame and preserve the same person/body, clothing, shoes, room, mirror, phone, lighting and product details for the entire clip.
    {face_block_rule()}
    {motion_header}
    MOVEMENT SEQUENCE: {motion}
    Keep all movement physically possible within a single 8-second take. If every beat cannot fit cleanly, prioritize the hook, product detail, fit/view, and finish rather than rushing or morphing.
    The phone hand remains steady at face level throughout and must never reveal the face. Maintain realistic anatomy, garment physics and mirror geometry. The product must never change color, print, material, proportions, pieces, size, or branding. No zoom jump, no camera cut, no transformation, no extra people, no duplicate body parts, no text, subtitles, captions, music, speech or sound effects. Natural subtle iPhone handheld motion only.
    No bending, no squatting, no sexual poses. FINAL RULE: phone fully covers the face from start to finish. Preserve the exact outfit/product and background.
    """)
