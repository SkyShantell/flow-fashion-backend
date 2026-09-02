from __future__ import annotations

import re

SCENES = {
    "Modern apartment mirror": "a realistic modern upscale apartment with a large full-length mirror, warm ceiling lighting, neutral furniture, and believable lived-in details",
    "Walk-in closet": "a realistic upscale walk-in closet with dark wood shelving, folded clothes, soft warm recessed lighting, and a large full-length mirror",
    "Luxury bathroom mirror": "a realistic upscale bathroom with dark stone surfaces, a large clean mirror, warm ceiling light, and subtle hotel-like details",
    "Penthouse at night": "a realistic modern penthouse at night with floor-to-ceiling windows, city lights, warm recessed lighting, polished floor, and a large full-length mirror",
}


def clean_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def face_block_rule() -> str:
    return (
        "CRITICAL FACE RULE: this is a mirror selfie and the smartphone must stay directly in front of the model's face, blocking the face completely. "
        "No eyes, eyebrows, nose, lips, cheeks, jawline, facial skin, or recognizable facial features may be visible around the phone. "
        "Hair may be visible, but the face itself must be 100% obscured by the phone. Do not lower, offset, tilt away, or move the phone aside."
    )


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
    back = "The product references indicate a back design; use a slight angled pose that hints at it without hiding the front." if getattr(job, "back_design", False) else ""
    revision = (getattr(job, "regen_instruction", None) or "").strip()
    revision_rule = f"OPERATOR REVISION REQUEST: {revision}. Apply this while preserving the exact product, avatar body/identity cues, and face-blocking rule." if revision else ""

    return clean_prompt(f"""
    Ultrarealistic photorealistic POV mirror selfie, 1080x1920, 9:16, {framing}. {gender_line} using @reference_1 as the avatar/person reference.
    {face_block_rule()}
    REFERENCE RULES: @reference_1 is the PERSON/AVATAR reference. Use the same person/body/hair/skin/visible styling cues, but do not reveal the face. {ref_mentions} are CLOTHING/PRODUCT references for {product}. Ignore any people/models appearing in the clothing references and use only the product design, colors, materials, print, stitching, construction, fit cues and details.
    The model wears the exact same {product} as the product references -- preserve exact colors, graphics, pattern, fabric texture, fit, cut, seams, straps, labels, proportions and product-specific details. {focus_rule} {fallback} {back} {revision_rule}
    Visibly clear super ultra luxury {scene_text}, warm indoor lighting, luxury but lived-in props, natural shadows.
    Pose: phone hand subtle natural sway, {pose}. No sexual poses.
    Realism: authentic iPhone 16 Pro UGC photo, natural skin texture where visible, realistic hands and fingers, believable fabric folds and seams, subtle sensor grain, mild phone-camera compression, no studio lighting, no glossy catalog look, no extra people, no duplicated limbs, no morphing, no text overlays, no prices, no added logos, no watermarks.
    CRITICAL: preserve every detail of the product from the reference photo. NO FACE SHOWN. PHONE BLOCKS THE FACE COMPLETELY.
    """)


def _base_motion(focus: str, style: str, profile: str, back: bool) -> str:
    style = (style or "Calm").lower()
    profile = (profile or "Male").lower()

    if focus == "shoes":
        return "Start full-body with one foot slightly forward. Shift weight between feet, make one small natural step, angle the legs to show the footwear from front and side, then finish with one foot forward and the shoes unobstructed."
    if focus == "handbag":
        return "Start with the bag at hip level. Lightly lift the bag, angle it toward the mirror to show shape, strap and hardware, take one small step, then let it rest naturally at true size."
    if focus == "pants":
        return "Start full-body front view. Shift weight naturally, use the free hand to gesture to the waistband, pocket and upper thigh, take a small half-step and quarter-turn to show side fit and leg shape, then finish angled with one leg forward."
    if focus == "shirt":
        motion = "Start full-body front view. Use the free hand to brush the chest, collar, sleeve or hem while gently shifting weight. Make a natural quarter-turn to show the side silhouette."
        if back:
            motion += " Briefly rotate farther to reveal the back graphic/design, then return toward the mirror."
        return motion + " Finish relaxed front/three-quarter, shirt still clearly visible."

    if style == "high-energy":
        motion = "Start centered with a quick friendly greeting gesture, take a confident small step, point briefly toward the product detail, pinch or smooth the fabric once, then turn slightly to show the silhouette."
    elif style == "flashy":
        motion = "Start with confident trend-style energy, quick shoulder/free-hand opener, small power step, hem or fabric touch, brief detail point, smooth side turn, then finish with a confident nod."
    else:
        if profile.startswith("f"):
            motion = "Start in a soft confident full-body front pose, subtle greeting, hand at waist, slow step, light fabric or hair adjustment, side turn, then finish with a small nod."
        else:
            motion = "Start in a centered full-body front pose, subtle confident flex or free-hand opener, slow step, light fabric pinch, side turn, then finish with a small nod."
    if back:
        motion += " Include one tasteful partial side/back reveal without hiding the outfit."
    return motion


def video_prompt(job, *, creator_profile: str = "Male", video_style: str = "Calm") -> str:
    focus = getattr(job, "focus", None) or "outfit"
    motion = _base_motion(focus, video_style, creator_profile, bool(getattr(job, "back_design", False)))
    return clean_prompt(f"""
    8-second vertical 1080x1920 target, 9:16 TikTok UGC mirror try-on. One continuous take, multi-shot OFF, audio OFF, no cuts.
    Use the supplied start image as the exact first frame and preserve the same person/body, clothing, shoes, room, mirror, phone, lighting and product details for the entire clip.
    {face_block_rule()}
    Movement should look like a real creator casually checking the fit in a mirror, not runway choreography. {motion}
    The phone hand remains steady at face level throughout and must never reveal the face. Maintain realistic anatomy, garment physics and mirror geometry. The product must never change color, print, material, proportions, pieces, size, or branding. No zoom jump, no camera cut, no transformation, no extra people, no duplicate body parts, no text, subtitles, captions, music, speech or sound effects. Natural subtle iPhone handheld motion only.
    FINAL RULE: phone fully covers the face from start to finish. Preserve the exact outfit/product and background.
    """)
