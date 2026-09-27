from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from textwrap import dedent
from typing import Literal


ShoePovFormat = Literal["worn", "held"]


@dataclass(frozen=True)
class Beat:
    name: str
    text: str


@dataclass(frozen=True)
class BeatSelection:
    hook: Beat
    mid: Beat
    closer: Beat


@dataclass(frozen=True)
class Setting:
    name: str
    worn: str
    held: str


IMAGE_TEMPLATE_WORN = dedent("""
FORMAT: WORN. Vertical 9:16 photo taken on a phone. FIRST-PERSON POV: the creator sits and films their own feet, the phone held at chest height and tilted down, slight wide-angle phone look, realistic smartphone depth and grain. No face, no mirror, no reflection, no tripod, no second person anywhere in frame. NO HAND IS IN FRAME: at most the bottom edge of the phone itself shows at the very top of the frame. Only the creator's two lower legs and two feet are visible.

FRAME (this exact layout, it is the first frame of the video):
- Centre and bottom of frame: the creator's two feet, {{skinTone}} skin at the ankle, BOTH feet wearing the {{productTitle}}, one shoe on each foot, laces/straps/buckles done up as sold, resting on the ground under them. Start pose: {{startPose}}. The legs enter the frame at the top in {{bottoms}}.
- The two shoes are the largest objects in the frame and sharply in focus; the ground is a touch softer.
- Ankles: {{skinTone}} skin, {{anklewear}}.
- Ground, light and prop: {{setting}}
- Nothing else in frame. No hand, no held shoe, no third shoe, no other footwear, no shoe box on the feet, no text, no watermark, no sticker.

{{samePerson}} {{countLock}} {{frameGuard}}

PRODUCT (exact, both shoes identical): {{productDetails}}. Reproduce the logo, printed text, pattern, colour, upper material, sole, laces/straps/buckles exactly as in the product reference, same wording, same spelling, same placement, on BOTH shoes. The product is evenly lit and readable: a black or dark product keeps visible seams, texture and edge highlights and never merges with a dark floor, mat or seat; a white product keeps its texture and is not blown out.

LOOK: natural light only, as described in the setting, no flash, no studio backdrop, no colour cast on the product, slightly imperfect handheld framing like a real TikTok Shop creator photo. Real skin texture at the ankle; the shoes sit with visible weight on the ground.{{lighting}}

FEET: {{feet}}
""").strip()


IMAGE_TEMPLATE_HELD = dedent("""
FORMAT: HELD. Vertical 9:16 photo taken on a phone. FIRST-PERSON POV: the creator sits and holds the pair of shoes up in front of them, the phone propped at chest height and looking down at the hands (both of the creator's hands are on the shoes), slight wide-angle phone look, realistic smartphone depth and grain. No face, no mirror, no reflection, no tripod, no second person anywhere in frame. THE FEET ARE OUT OF FRAME: the shot is framed at chest height over the seat, the lap or the open box, and no foot and no worn shoe appear anywhere. Only the creator's two hands, wrists and the pair of shoes are visible.

FRAME (this exact layout, it is the first frame of the video):
- Centre of frame: the creator's two hands, {{skinTone}} skin, holding the pair of {{productTitle}}, one shoe in each hand, gripped around the midsole/heel. Start pose: {{startPose}}. The pair is the largest object in the frame and sharply in focus; the surface below is a touch softer.
- Hands: {{skinTone}} skin, five fingers on each hand in a natural grip that does not cover the logo or printed text, {{nails}}, {{accessories}}. Sleeves: {{sleeve}} at both wrists, running off the frame edges.
- Below the hands, light and prop: {{setting}}
- Nothing else in frame. No foot, no leg, no shoe on a foot, no third shoe, no other footwear, no text, no watermark, no sticker.

{{samePerson}} {{countLock}} {{frameGuard}}

PRODUCT (exact, both shoes identical): {{productDetails}}. Reproduce the logo, printed text, pattern, colour, upper material, sole, laces/straps/buckles exactly as in the product reference, same wording, same spelling, same placement, on BOTH shoes. The product is evenly lit and readable: a black or dark product keeps visible seams, texture and edge highlights and never merges with a dark seat, box or footwell; a white product keeps its texture and is not blown out.

LOOK: natural light only, as described in the setting, no flash, no studio backdrop, no colour cast on the product, slightly imperfect handheld framing like a real TikTok Shop creator photo. Real skin texture; the shoes have visible weight in the hands.{{lighting}}

HANDS: {{hands}}
""").strip()


VIDEO_TEMPLATE_WORN = dedent("""
FORMAT: WORN. 8 seconds, 9:16 vertical, TikTok Shop UGC. Use the provided image as the EXACT first frame: same framing, same two feet ({{skinTone}} skin at the ankle), same {{productTitle}} on BOTH feet, same trousers, same ground and prop, same lighting. ONE continuous handheld take, no cuts, no cutaways, no transitions, no speed changes.

CAMERA: true first-person POV. The creator sits where the first frame shows and films their own feet with the phone held at chest height, tilted down; the phone and the hand holding it are NEVER visible (at most the phone's own bottom edge at the very top of frame); there is no third-person camera. The lens keeps looking down for the full 8 seconds with only natural handheld micro-drift (1 to 2 cm sway), one or two autofocus breaths between the shoes and the ground, and slight motion blur on the faster move. The camera itself never pushes in, zooms, pans or tilts up. The ground, prop and trouser hems stay where they are in the first frame; only the feet move, a few centimetres at a time, and they never leave the frame. NO HAND ever enters the frame and no shoe ever leaves a foot. The creator never stands up, never walks away, never jumps.

{{HOOK}}

{{MID}}

{{CLOSER}}

{{samePerson}}

{{productLock}} {{countLock}}

RIGID SHOES, SMALL FOOT MOVES: each shoe of the {{productTitle}} is a rigid object laced on a foot; it never bends beyond the natural flex at the toe, never stretches, warps, swims, re-forms or changes shape or proportion; every change of angle is the SAME shoe moved by the foot inside it, on the foot for the whole take. At most ONE turn of a foot in the whole take (a quarter turn at most, on the heel), made slowly over about a second with the shoe's edges staying sharp; the rest of the take is lifts and taps of a few centimetres. No morphing between views, no jump between angles. Laces or straps stay done up and sway only with the move; the shoes have weight and never float. Moves are unhurried, each with a short natural pause at the end, like a person admiring their new shoes. No robotic smoothness, no rendered-turntable feel.

AUDIO: {{VOICE}}

NEVER: no standing up, no walking away, no jumping, no spinning, no 360, no continuous rotation, no zoom, no dolly, no push-in of the camera itself, no camera pan, no camera tilt up, no cut, no transition, no slow motion, no speed ramp, no face, no model, no mirror, no reflection, no visible phone beyond its bottom edge, no second person, no hand in frame, no fingers, no held shoe, no shoe taken off, no shoe box on the feet, no duplicate or third shoe, no background change, no outfit change, no lighting change, no on-screen text, no captions, no subtitles, no stickers, no watermark, no logo cards, no logo other than the product's own. Product lock repeated: {{productTitle}} keeps its exact logo, printed text, pattern, colour, sole and closures on both shoes in every frame.{{lighting}}

FEET: {{feet}}
""").strip()


VIDEO_TEMPLATE_HELD = dedent("""
FORMAT: HELD. 8 seconds, 9:16 vertical, TikTok Shop UGC. Use the provided image as the EXACT first frame: same framing, same two hands ({{skinTone}} skin), nails, jewellery and sleeves, same pair of {{productTitle}} in the hands, same seat, box or surface below, same lighting. ONE continuous handheld take, no cuts, no cutaways, no transitions, no speed changes.

CAMERA: true first-person POV. The creator sits where the first frame shows; the phone is propped or held at chest height looking down at their hands and is NEVER visible; there is no third-person camera. The lens keeps looking down at the hands for the full 8 seconds with only natural handheld micro-drift (1 to 2 cm sway), one or two autofocus breaths between the shoes and the surface below, and slight motion blur on the faster lift. The camera itself never pushes in, zooms, pans or tilts up or down. The seat, box, sleeve cuffs and surface stay where they are in the first frame; only the hands and the pair of {{productTitle}} move. THE FEET STAY OUT OF FRAME for the whole take: no foot, no leg, no shoe on a foot ever appears, and the shoes are never put on.

{{HOOK}}

{{MID}}

{{CLOSER}}

{{samePerson}}

{{productLock}} {{countLock}}

RIGID SHOES, ONE PHYSICAL TURN: each shoe of the {{productTitle}} is a rigid object; it never bends, stretches, warps, swims, re-forms or changes shape or proportion; every change of angle is the SAME solid shoe rotated in the same gripping hand, the fingers visibly staying wrapped on it through the turn, like a person turning a shoe in their hand. At most ONE turn in the whole take (a quarter turn at most), made slowly over about a second with the shoe's edges staying sharp; the rest of the take is small tilts of a few degrees and lifts of a few centimetres. No morphing between views, no jump between angles, no view that appears without the hand turning it there. MOVEMENT REALISM: every motion is a small wrist or forearm move, and neither shoe leaves its hand except where a beat says the pair is set down and the hands let go. Fingers stay wrapped around the midsole/heel and never cover the logo or printed text; laces or straps hang and sway with gravity; the shoes have weight, nothing floats or snaps into place. Moves are unhurried, each with a short natural pause at the end, like a person showing a friend. No robotic smoothness, no perfectly centred product, no rendered-turntable feel.

AUDIO: {{VOICE}}

NEVER: no spinning, no 360, no continuous rotation (a quarter turn each way is the maximum), no zoom, no dolly, no push-in of the camera itself, no camera pan, no camera tilt, no cut, no transition, no slow motion, no speed ramp, no face, no model, no mirror, no reflection, no visible phone, no second person, no extra hands or fingers, no hand deformation, no foot in frame, no shoe on a foot, no putting a shoe on, no duplicate or third shoe, no background change, no outfit change, no lighting change, no on-screen text, no captions, no subtitles, no stickers, no watermark, no logo cards, no logo other than the product's own. Product lock repeated: {{productTitle}} keeps its exact logo, printed text, pattern, colour, sole and closures on both shoes in every frame.{{lighting}}

HANDS: {{hands}}
""").strip()


SAME_PERSON_TEMPLATE = (
    "SAME PERSON: {{parts}} belong to the same person: {{skinTone}} skin, {{genderNoun}}. "
    "{{partsList}} all show that same {{skinTone}} skin in every frame, never a lighter or darker patch, "
    "never a second skin tone anywhere in frame.{{nailsLine}}"
)

COUNT_LOCK_TEMPLATE = (
    "EXACTLY TWO SHOES: exactly two shoes of this product exist in the frame: {{where}}, never three, "
    "never a mixed pose (never one shoe on a foot and one in a hand). One pair only: no second pair, "
    "no duplicate, no shoe grows out of another shoe, no second sole, heel or toe splits off, no shoe "
    "changes into a different shoe as it moves. Count the shoes in every frame: two, {{where}}."
)

FRAME_GUARD = (
    "The product photos are references for the SHOE ONLY: never copy their scene, camera angle, "
    "background, model, legs or pose; the frame is the FRAME above and nothing else."
)

PRODUCT_LOCK_TEMPLATE = (
    "CRITICAL PRODUCT LOCK — {{productTitle}}. The two shoes {{where}} are the exact product in the first "
    "frame and stay identical to it and to each other for all 8 seconds. Colour: {{productColour}} — no "
    "colour shift, no tint change, no fading. Logos and printed text: {{productBranding}} (write out the "
    "exact wording and where it sits, e.g. \"a pink N on the outer side, VANS in white on the heel tab\"; "
    "if the product has none, write \"no logo — plain upper, keep it plain\") — same wording, same spelling, "
    "same font, same size, same placement on both shoes; the letters never rearrange, blur into other "
    "letters, duplicate, mirror, move or vanish, and the logo never drifts across the shoe as it moves. "
    "Pattern and material: {{productPatternMaterial}} (e.g. \"pink and black checkerboard canvas\", \"tan suede "
    "with white stitching\", \"warm brown/tan leopard print\") — no pattern drift, no texture change. Sole: "
    "{{productSole}} (colour, thickness, tread, e.g. \"chunky white foam sole with grey tread\", \"thick black "
    "platform\") — same height, same colour, same tread, no deformation. Closures: {{productClosures}} (e.g. "
    "\"white flat laces\", \"two black straps with two black buckles\", \"none — slip-on\") — same count, same "
    "colour, same position, nothing added, nothing removed. Shape stays the same: no morphing, no stretching, "
    "no added straps, no extra eyelets, no changed toe shape, no altered platform height, no second logo, no "
    "swapped shoe, no duplicate shoe. {{coverRule}} EXPOSURE: the product is evenly lit and fully readable "
    "in every frame; a black or dark product keeps visible seams, texture and edge highlights and is never "
    "crushed into shadow, silhouetted or blended into a dark seat, mat, box or footwell; a white product "
    "keeps its texture and is never blown out; when a shoe comes close to the lens it stays sharp and "
    "correctly exposed, not a dark or glowing blob. Re-check at 2 s, 5 s and 8 s: the {{productBranding}}, "
    "pattern, colour, sole and closures read exactly as in the first frame on both shoes."
)

SILENT_VOICE = (
    "No speech, no voiceover, no singing, no humming, no lip-sync, no music, no sound effects. Silent, or at "
    "most a faint natural ambient tone from the setting (car cabin hush, distant street, quiet garage, a little "
    "fabric rustle). Nobody talks."
)

TALKING_VOICE_TEMPLATE = (
    "VOICEOVER: a young {{gender}} creator speaks off-camera in a casual, low-key TikTok voice, like telling a "
    "friend while filming their own shoes — {{voiceProfile}}. {{pronoun}} says the line once, unhurried, starting "
    "about 0.5 s in and finishing before 7 s: \"{{LINE}}\". Keep the line under 14 words. No face is in frame so "
    "no lips are seen and no lip-sync is needed. No music, no other voices, no sound effects under the line; "
    "faint ambient tone only. The spoken words must not appear as text on screen."
)


WORN_HOOKS = (
    Beat("Heel tap (founder base)", "FIRST FRAME: both feet flat on the ground side by side a hand's width apart, toes pointing away from the lens, the uppers and lacing of both shoes facing the camera. 0–2 s — HOOK: the RIGHT heel lifts a few centimetres and taps down twice, unhurried, the toe staying on the ground, so the {{keyDetail}} and the upper catch the light on each tap; the left foot stays planted. Autofocus pulls from the ground onto the shoes and locks."),
    Beat("Toe to the lens", "FIRST FRAME: both feet flat on the ground, the RIGHT foot half a shoe length ahead of the left, both uppers facing the camera. 0–2 s — HOOK: the right foot pivots on its heel so the toe angles up toward the lens by about 30 degrees, showing the toe box and the {{keyDetail}}, holds for half a second, then settles flat with a small natural bounce. The left foot stays planted."),
    Beat("Small lift", "FIRST FRAME: both feet flat on the ground side by side, uppers facing the camera. 0–2 s — HOOK: the RIGHT foot lifts straight up off the ground a few centimetres, the whole shoe staying level so the side profile and sole edge show for a beat, then sets down in the same spot with a small settle. The left foot stays planted. Autofocus locks on the shoes."),
)

WORN_MIDS = (
    Beat("Step onto the mat", "2–5 s — MID: the RIGHT foot lifts and steps a hand's width forward onto the ground under it, setting down heel first then toe, then the LEFT foot follows and sets down beside it; both shoes end flat and side by side with the uppers facing the lens. Two small unhurried steps of the feet only, a few centimetres each; the body, the trousers and the camera do not move and the feet never leave the frame."),
    Beat("Ankles cross and uncross", "2–5 s — MID: the RIGHT foot lifts a little and crosses over the left ankle so the outer side profile, side branding and sole edge of the right shoe face the lens, holds there a full second, then uncrosses and sets back down beside the left foot, upper facing the camera. One cross, one uncross, nothing continuous."),
    Beat("Side profile turn", "2–5 s — MID: the RIGHT foot pivots on its heel to turn the toe outward about 45 degrees so the outer side profile, side branding and sole thickness face the lens, holds that profile for one full second, then turns back to point forward with the upper facing the camera. One turn out, one turn back, never more than a quarter turn."),
)

WORN_CLOSERS = (
    Beat("Settle and hold (founder base)", "5–8 s — CLOSER: both feet settle flat side by side with the uppers squarely facing the lens and hold for the final three seconds. Only the phone's breathing drift moves the frame; autofocus locks and stays locked on the {{keyDetail}}; the pair reads identical. Ends still."),
    Beat("Heel tap and rest", "5–8 s — CLOSER: the LEFT heel lifts a few centimetres and taps down once, then both feet rest flat side by side and hold for the last two seconds, uppers facing the camera. The last second is still, both shoes on the feet."),
    Beat("Return to first frame", "5–8 s — CLOSER: both feet move back to exactly the first-frame position and angle and stop, so the last frame matches the first for a seamless loop. The last second is still."),
)

HELD_HOOKS = (
    Beat("Close and turn (founder base)", "FIRST FRAME: the pair is held at chest height over the surface below, one shoe in each hand, both uppers facing the lens, toes pointing up and slightly outward. 0–2 s — HOOK: the RIGHT hand brings its shoe about 15 cm closer to the lens and turns it a quarter turn so the outer side profile and {{keyDetail}} face the camera, ending with a tiny natural overshoot and settle; the left hand keeps its shoe steady below. Autofocus pulls onto the near shoe and locks."),
    Beat("Pair forward", "FIRST FRAME: the pair is held together side by side in both hands at chest height, uppers facing the lens, heels resting against the palms, toes up. 0–2 s — HOOK: both hands lift the pair a few centimetres toward the lens together in one smooth motion, with a few degrees of forward tilt so the {{keyDetail}} and toe boxes catch the light, ending with a tiny overshoot and settle. Autofocus locks on the pair."),
    Beat("Freeze then lift", "FIRST FRAME: the pair is held in both hands at chest height over the surface below, one shoe in each hand, uppers facing the lens. 0–2 s — HOOK: for the first 0.6 seconds nothing moves except the phone's own handheld micro-drift, exactly like the still coming alive; then both hands lift the pair briskly about 10 cm toward the lens and stop with a tiny natural overshoot and settle, uppers squarely facing the camera."),
)

HELD_MIDS = (
    Beat("Sole tilt", "2–5 s — MID: the RIGHT hand tips its {{productTitle}} so the sole and outsole tread face the lens for about one second, tread readable, then tilts it back so the upper faces the camera and holds. The shoe does not rise or fall during the tilt; the left hand keeps its shoe still, upper forward."),
    Beat("Heel rotate", "2–5 s — MID: the RIGHT hand holds its {{productTitle}} by the heel and rotates it slowly a quarter turn so the outer side profile, side branding and sole thickness face the lens, holds that profile for one full second, then turns it back so the upper faces the camera. One turn each way, never more than a quarter turn, nothing continuous; the left hand keeps its shoe still."),
    Beat("Pair together", "2–5 s — MID: the hands bring the two shoes together side by side, uppers forward, heels level and toes pointing the same way, so the pair reads as one matching pair with the {{keyDetail}} on both; hold there for one second, then ease them a few centimetres apart again. No turning."),
)

HELD_CLOSERS = (
    Beat("Into the box (founder base)", "5–8 s — CLOSER: the hands lower the pair into the open shoe box below (or set them side by side on the seat or surface below when there is no box), uppers up, toes pointing the same way, then open and slide out toward the frame edges. Final frame: the pair sitting side by side, both shoes identical, the hands resting at the edges; no motion except camera breath for the last second."),
    Beat("Locked hold", "5–8 s — CLOSER: both hands bring the pair to dead centre at chest height with the uppers squarely facing the lens and hold for the final three seconds. Only the phone's breathing drift moves the frame; autofocus locks and stays locked on the {{keyDetail}}; the top third of the frame stays clear above the shoes. Ends still, both shoes in the hands."),
    Beat("Return to first frame", "5–8 s — CLOSER: the hands lower and turn the pair back to exactly the first-frame position and angle and stop, so the last frame matches the first for a seamless loop. The last half second is still, both shoes in the hands."),
)

ALL_BEATS = WORN_HOOKS + WORN_MIDS + WORN_CLOSERS + HELD_HOOKS + HELD_MIDS + HELD_CLOSERS


SETTINGS = (
    Setting(
        "Driver's seat, shoe box on the passenger seat",
        "Sitting in the driver's seat of a parked car, both feet on the black rubber driver's floor mat, the pedal edges at the top of frame, the centre console and the passenger seat cushion at the right edge. Soft daylight through the windscreen and side windows, slightly cool, no direct sun, the footwell a touch darker. Prop: the shoe's open box with its lid and tissue paper on the passenger seat cushion at the right edge of frame. Keep the mat and product exposed separately: a dark product is lifted out of the footwell shadow and never sinks into the mat.",
        "Sitting in the driver's seat of a parked car, the pair held over the lap, the lower rim of the steering wheel at the top of frame, the trousers across the lap below the shoes, the centre console and the passenger seat cushion at the right edge, the feet out of frame below. Soft daylight through the windscreen and side windows, slightly cool, no direct sun. Prop: the shoe's open box with its lid and tissue paper on the passenger seat cushion at the right edge of frame. Keep the seat and product exposed separately: a dark product keeps its edge highlights against the dark cabin.",
    ),
    Setting(
        "Passenger seat footwell",
        "Sitting in the front passenger seat of a car, both feet on the ribbed black rubber floor mat in the footwell, the grey/black seat edge, a slice of door sill and the centre console at the frame sides. Soft daylight through the windscreen and open door falls on the mat, slightly cool, no direct sun, slight shadow deep in the footwell. Prop: a designer handbag on the floor mat in the upper corner. Keep the mat and product exposed separately: a dark product is lifted out of the footwell shadow and never sinks into the mat.",
        "Sitting in the front passenger seat of a car, the pair held over the lap, the glovebox edge at the top of frame, the trousers across the lap below the shoes, the centre console and a slice of the door at the frame sides, the feet out of frame below. Soft daylight through the windscreen and open door, slightly cool, no direct sun. Prop: a designer handbag on the centre console at the frame edge. Keep the seat and product exposed separately: a dark product keeps its edge highlights against the dark cabin.",
    ),
    Setting(
        "Open car trunk",
        "Sitting on the rear bumper of a car with the trunk open, both feet on the pale concrete driveway below, the trunk's dark carpeted floor and the bumper lip at the top edge of frame, a tail light at one side. Even open-shade daylight from behind, neutral tone, soft shadow under the bumper. Prop: a designer handbag on the trunk carpet at the top corner. Dark product keeps visible edge highlights against the concrete.",
        "Sitting on the edge of the open trunk of a car, the pair held over the trunk's dark carpeted floor, the bumper lip at the bottom edge of frame and a tail light at one side, the feet out of frame below. Even open-shade daylight, neutral tone, soft shadow inside the trunk. Prop: a designer handbag on the trunk carpet at the top corner. Dark product keeps visible edge highlights against the trunk carpet.",
    ),
    Setting(
        "Garage floor beside the car",
        "Sitting on a low stool on a smooth grey concrete garage floor beside a parked car, both feet on the concrete, the car's lower door panel, sill and a tyre at one edge of frame, a faint oil mark and a painted floor line on the concrete. Cool overhead garage LED light mixed with daylight from the open garage door, no warm cast. Prop: a designer handbag on the floor in the upper corner. Dark product keeps visible edge highlights against the grey floor.",
        "Sitting on a low stool beside a parked car in a garage, the pair held over the lap, the trousers across the lap below the shoes, the grey concrete floor and the car's lower door panel and tyre behind them at the frame edges, the feet out of frame below. Cool overhead garage LED light mixed with daylight from the open garage door, no warm cast. Prop: a designer handbag on the floor in the upper corner. Dark product keeps visible edge highlights against the grey floor.",
    ),
    Setting(
        "Driveway by the car door",
        "Sitting on the door sill of the car with its front door open, both feet on the pale concrete driveway, the sill edge, the bottom of the open door and the front tyre at the frame edge. Bright open-shade daylight, neutral tone, a soft shadow under the car. Prop: a designer handbag on the concrete in the upper corner.",
        "Sitting on the door sill of the car with its front door open, the pair held over the lap, the trousers across the lap below the shoes, the pale concrete driveway and the bottom of the open door at the frame edges, the feet out of frame below. Bright open-shade daylight, neutral tone, a soft shadow under the car. Prop: a designer handbag on the concrete in the upper corner.",
    ),
    Setting(
        "Shoe box open on the car seat",
        "Sitting sideways on the front passenger seat with the car door open, both feet up on the seat fabric, the shoe's open box with its lid and tissue paper on the seat beside the feet, the seat stitching and seatbelt buckle visible. Bright, even daylight from the open door, warm afternoon tone. Prop: a small crossbody bag resting on the seat behind the feet.",
        "Sitting sideways on the front passenger seat with the car door open, the pair held over the shoe's open box with its lid and tissue paper on the seat below, the seat stitching and seatbelt buckle visible, the feet out of frame. Bright, even daylight from the open door, warm afternoon tone. Prop: a small crossbody bag resting on the seat at the frame edge.",
    ),
    Setting(
        "Night car, pink LED",
        "Sitting in the front passenger seat of a car at night, both feet on the black rubber floor mat in the footwell, the centre console and a slice of the door at the frame sides. Dark cabin with a pink-purple ambient LED glow from the footwell and dashboard strip, a cool streetlight through the windscreen, moody and low-key. The product is lit by the LED and the phone's screen glow so it stays sharp and fully readable, never a dark blob, never sunk into the mat. Prop: a small black designer bag on the mat in the upper corner.",
        "Sitting in the front passenger seat of a car at night, the pair held over the lap, the trousers across the lap below the shoes, the centre console and a slice of the door at the frame sides, the feet out of frame below. Dark cabin with a pink-purple ambient LED glow from the dashboard strip, a cool streetlight through the windscreen, moody and low-key. The product is lit by the LED and the phone's screen glow so it stays sharp and fully readable, never a dark blob, never sunk into the seat. Prop: a small black designer bag on the centre console at the frame edge.",
    ),
)

WOMEN_BOTTOMS = ("black leggings", "ripped black skinny jeans", "black bike shorts")
MEN_BOTTOMS = ("black sweatpants", "grey joggers", "black cargo pants", "washed jeans", "grey shorts")
WOMEN_ANKLEWEAR = (
    "a fine gold anklet on one ankle, no-show socks inside the shoes",
    "a fine gold anklet on one ankle, bare ankles inside the shoes",
)
MEN_ANKLEWEAR = (
    "white crew socks showing at the ankle, no jewellery",
    "black crew socks showing at the ankle, no jewellery",
    "grey ankle socks just showing at the shoe collar, no jewellery",
)
WOMEN_NAILS = ("short pale-pink gel nails", "milky white gel nails", "short nude almond nails")
WOMEN_SLEEVES = (
    "a cream ribbed knit sweater sleeve",
    "a pink ribbed knit sweater sleeve",
    "a distressed cream ribbed knit sweater sleeve",
    "a brown knit sweater sleeve",
    "a fitted black long-sleeve top",
)
MEN_SLEEVES = (
    "a black hoodie cuff",
    "a grey crewneck sweatshirt sleeve",
    "a navy puffer-jacket cuff",
    "a washed-black hoodie cuff",
)
MEN_PROPS = (
    "a backpack",
    "a sling bag",
    "a gym bag",
    "the edge of a skateboard deck",
    "a basketball",
    "car keys",
    "a water bottle",
    "a cap",
    "a pair of sunglasses",
)

REFERENCE_FALLBACK = "exactly as in the product reference and the first frame"
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")
EXACT_SPEC_RE = re.compile(r"(?:^|[;—-]\s*)(colou?rs?|print|material|closure):\s*([^;.]+)", re.IGNORECASE)
ACCESSORY_RE = re.compile(r"\b(ring|bracelet|watch|bangle|cord|bead)s?\b", re.IGNORECASE)


def _stable_index(seed: str, salt: str, size: int) -> int:
    digest = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % size


def _pick(values: tuple[str, ...], seed: str, salt: str) -> str:
    return values[_stable_index(seed, salt, len(values))]


def _fill(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def normalize_format(value: str | None) -> ShoePovFormat:
    if str(value or "").strip().lower() == "held":
        return "held"
    return "worn"


def format_for_item(index: int, pinned_format: str | None = None) -> ShoePovFormat:
    if str(pinned_format or "").strip().lower() in {"worn", "held"}:
        return normalize_format(pinned_format)
    return "worn" if max(0, int(index)) % 2 == 0 else "held"


def choose_beats(
    format_name: str,
    *,
    seed: str,
    index: int,
    rotating_formats: bool = True,
) -> BeatSelection:
    fmt = normalize_format(format_name)
    step = max(0, int(index)) // 2 if rotating_formats else max(0, int(index))
    hooks = WORN_HOOKS if fmt == "worn" else HELD_HOOKS
    mids = WORN_MIDS if fmt == "worn" else HELD_MIDS
    closers = WORN_CLOSERS if fmt == "worn" else HELD_CLOSERS

    hook_offset = _stable_index(seed, f"{fmt}:hook", len(hooks))
    mid_offset = _stable_index(seed, f"{fmt}:mid", len(mids))
    closer_offset = _stable_index(seed, f"{fmt}:closer", len(closers))
    valid: list[BeatSelection] = []
    for raw_step in range(len(hooks) * len(mids) * len(closers)):
        hook = hooks[(hook_offset + raw_step) % len(hooks)]
        mid = mids[(mid_offset + raw_step // len(hooks)) % len(mids)]
        closer = closers[(closer_offset + raw_step // (len(hooks) * len(mids))) % len(closers)]
        if fmt == "worn" and hook.name == "Heel tap (founder base)" and closer.name == "Heel tap and rest":
            continue
        if fmt == "worn" and hook.name == "Toe to the lens" and mid.name == "Side profile turn":
            continue
        if fmt == "held":
            turning = {"Close and turn (founder base)", "Heel rotate", "Sole tilt"}
            if sum(beat.name in turning for beat in (hook, mid, closer)) > 1:
                continue
        valid.append(BeatSelection(hook=hook, mid=mid, closer=closer))
    if not valid:
        raise RuntimeError(f"No valid Shoes POV beat trios for {fmt}.")
    return valid[step % len(valid)]


def normalize_skin_tone(value: str | None) -> str:
    tone = re.sub(r"\s+", " ", str(value or "").strip())
    tone = re.sub(r"\s+(?:skin(?:\s+tone)?|skin-toned)$", "", tone, flags=re.IGNORECASE).strip()
    if not tone:
        return "[skin tone missing]"
    if tone.lower() == "deep":
        return "deep brown"
    return tone


def _normal_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def resolve_setting_name(requested: str | None, *, seed: str, index: int) -> tuple[str, bool]:
    query = " ".join(_normal_words(str(requested or "")))
    labels = [(setting, " ".join(_normal_words(setting.name))) for setting in SETTINGS]
    if query:
        for setting, label in labels:
            if query == label:
                return setting.name, True
        query_words = query.split()
        whole_matches: list[Setting] = []
        for setting, label in labels:
            label_words = label.split()
            if all(any(q.startswith(word) or word.startswith(q) for word in label_words) for q in query_words):
                whole_matches.append(setting)
        if whole_matches:
            chosen = min(whole_matches, key=lambda item: (len(_normal_words(item.name)), len(item.name)))
            return chosen.name, True
        stems = {word[:4] for word in query_words if len(word) >= 4}
        stem_matches = [setting for setting, label in labels if stems.intersection({word[:4] for word in label.split() if len(word) >= 4})]
        if stem_matches:
            chosen = min(stem_matches, key=lambda item: (len(_normal_words(item.name)), len(item.name)))
            return chosen.name, True
    chosen = SETTINGS[(_stable_index(seed, "setting", len(SETTINGS)) + max(0, int(index))) % len(SETTINGS)]
    return chosen.name, False


def _setting_text(name: str, fmt: ShoePovFormat, gender: str, seed: str, index: int) -> str:
    setting = next(item for item in SETTINGS if item.name == name)
    text = setting.worn if fmt == "worn" else setting.held
    if gender == "male":
        item_seed = f"{seed}:{index}"
        prop = _pick(MEN_PROPS, item_seed, "mens-prop")
        text = re.sub(r"Prop:\s*.*?\.", f"Prop: {prop} in the upper corner of frame.", text, count=1)
    return text


def _gender(value: str | None) -> str:
    return "male" if str(value or "").strip().lower().startswith("m") else "female"


def _same_person(fmt: ShoePovFormat, skin_tone: str, gender: str, nails: str) -> str:
    if fmt == "worn":
        values = {
            "parts": "the two feet and ankles",
            "skinTone": skin_tone,
            "genderNoun": "a man" if gender == "male" else "a woman",
            "partsList": "Both ankles, both feet where they show above the shoe and any skin at the trouser hem",
            "nailsLine": "",
        }
    else:
        values = {
            "parts": "the two hands",
            "skinTone": skin_tone,
            "genderNoun": "a man" if gender == "male" else "a woman",
            "partsList": "Both hands, both wrists and every finger",
            "nailsLine": f" Nails: {nails}.",
        }
    return _fill(SAME_PERSON_TEMPLATE, values)


def _count_lock(fmt: ShoePovFormat) -> str:
    where = "both on the feet" if fmt == "worn" else "both in the hands"
    return _fill(COUNT_LOCK_TEMPLATE, {"where": where})


def _lighting(value: str | None) -> str:
    text = re.sub(r"^\s*LIGHTING:\s*", "", str(value or "").strip(), flags=re.IGNORECASE)
    return f"\n\nLIGHTING: {text}, on every frame; no other light source or time of day overrides it." if text else ""


def _clean_line(value: str | None, fallback: str) -> str:
    return str(value or "").strip().rstrip(".") or fallback


def _resolved_styling(
    *,
    fmt: ShoePovFormat,
    gender: str,
    seed: str,
    index: int,
    nails: str | None,
    accessories: str | None,
) -> dict[str, str]:
    item_seed = f"{seed}:{index}"
    if gender == "male":
        return {
            "bottoms": _pick(MEN_BOTTOMS, item_seed, "bottoms"),
            "anklewear": _pick(MEN_ANKLEWEAR, item_seed, "anklewear"),
            "nails": "short clean natural nails, no polish",
            "accessories": str(accessories or "").strip() if ACCESSORY_RE.search(str(accessories or "")) else "no jewellery",
            "sleeve": _pick(MEN_SLEEVES, item_seed, "sleeve"),
        }
    return {
        "bottoms": _pick(WOMEN_BOTTOMS, item_seed, "bottoms"),
        "anklewear": _pick(WOMEN_ANKLEWEAR, item_seed, "anklewear"),
        "nails": str(nails or "").strip() or _pick(WOMEN_NAILS, item_seed, "nails"),
        "accessories": str(accessories or "").strip() if ACCESSORY_RE.search(str(accessories or "")) else "two thin gold rings including one small statement ring and a thin gold bracelet",
        "sleeve": _pick(WOMEN_SLEEVES, item_seed, "sleeve"),
    }


def _feet_or_hands(fmt: ShoePovFormat, gender: str, skin_tone: str, styling: dict[str, str], *, video: bool) -> str:
    if fmt == "worn":
        text = "The feet are a man's feet, a sock at each ankle." if gender == "male" else "The feet are a woman's feet, slim at the ankle."
        if video:
            text += f" The ankles have {skin_tone} skin, {styling['anklewear']}."
    else:
        text = "The hands are a larger man's hands; a few knuckle hairs and visible veins are fine. No dainty finger poses." if gender == "male" else "The hands are a slim woman's hands."
        if video:
            text += f" The hands have {skin_tone} skin, {styling['nails']}."
    if video:
        text += " Movement is the same choreography with slightly larger, blunter moves, no dainty poses." if gender == "male" else " Movement is the same choreography with slightly lighter, more deliberate placement."
    return text


def _extract_start_pose(hook_text: str, product_title: str) -> str:
    filled = _fill(hook_text, {"productTitle": product_title})
    match = re.search(r"(FIRST FRAME:.*?\.)\s+0–2 s", filled, flags=re.DOTALL)
    if not match:
        raise ValueError("Shoe POV hook is missing an extractable FIRST FRAME sentence.")
    return match.group(1).strip().rstrip(".")


def _parse_product_facts(description: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for match in EXACT_SPEC_RE.finditer(description):
        key = match.group(1).lower()
        if key.startswith("colo"):
            key = "color"
        facts[key] = match.group(2).strip()
    tokens = re.findall(r"\S+", description)
    sole = ""
    for pos, token in enumerate(tokens):
        if re.sub(r"[^a-z]", "", token.lower()) == "sole":
            sole = " ".join(tokens[max(0, pos - 5):min(len(tokens), pos + 6)]).strip(" ,.;:-")
            break
    pattern_material = "; ".join(value for value in (facts.get("print"), facts.get("material")) if value)
    return {
        "colour": facts.get("color", ""),
        "pattern_material": pattern_material,
        "closures": facts.get("closure", ""),
        "sole": sole,
    }


def _key_detail(explicit: str | None, branding: str | None) -> str:
    if str(explicit or "").strip():
        return str(explicit).strip()
    first = re.split(r"[;,]", str(branding or "").strip(), maxsplit=1)[0].strip()
    if first and not re.match(r"^(?:none|no logo|plain)\b", first, flags=re.IGNORECASE):
        return first
    return "logo or most recognisable detail of the upper"


def _product_lock(
    *,
    fmt: ShoePovFormat,
    title: str,
    description: str,
    colour: str | None,
    branding: str | None,
    pattern_material: str | None,
    sole: str | None,
    closures: str | None,
) -> tuple[str, str]:
    parsed = _parse_product_facts(description)
    resolved_branding = str(branding or "").strip() or REFERENCE_FALLBACK
    values = {
        "productTitle": title,
        "where": "both on the feet" if fmt == "worn" else "both in the hands",
        "productColour": str(colour or "").strip() or parsed["colour"] or REFERENCE_FALLBACK,
        "productBranding": resolved_branding,
        "productPatternMaterial": str(pattern_material or "").strip() or parsed["pattern_material"] or REFERENCE_FALLBACK,
        "productSole": str(sole or "").strip() or parsed["sole"] or REFERENCE_FALLBACK,
        "productClosures": str(closures or "").strip() or parsed["closures"] or REFERENCE_FALLBACK,
        "coverRule": "Trouser hems, socks and laces never cover the logo or printed text." if fmt == "worn" else "Fingers hold the shoes but never cover the logo or printed text.",
    }
    return _fill(PRODUCT_LOCK_TEMPLATE, values), resolved_branding


def _voice(gender: str, talking: bool, line: str | None) -> str:
    if not talking:
        return SILENT_VOICE
    cleaned = str(line or "").strip().strip("\"'").replace('"', "'")
    if not cleaned:
        raise ValueError("A talking Shoes POV video requires a spoken line.")
    if len(cleaned.split()) > 14:
        raise ValueError("The Shoes POV spoken line must be 14 words or fewer.")
    return _fill(TALKING_VOICE_TEMPLATE, {
        "gender": gender,
        "voiceProfile": "low-key, unbothered" if gender == "male" else "slightly surprised, a smile in the voice, not an ad read, not shouted, not robotic",
        "pronoun": "He" if gender == "male" else "She",
        "LINE": cleaned,
    })


def build_image_prompt(
    *,
    gender: str,
    skin_tone: str | None,
    product_title: str,
    product_description: str = "",
    format_name: str,
    hook: Beat,
    seed: str,
    index: int,
    setting_name: str | None = None,
    nails: str | None = None,
    accessories: str | None = None,
    lighting: str | None = None,
) -> str:
    fmt = normalize_format(format_name)
    resolved_gender = _gender(gender)
    tone = normalize_skin_tone(skin_tone)
    title = _clean_line(product_title, "[product title missing]")
    details = _clean_line(product_description, title)
    styling = _resolved_styling(fmt=fmt, gender=resolved_gender, seed=seed, index=index, nails=nails, accessories=accessories)
    chosen_setting, _matched = resolve_setting_name(setting_name, seed=seed, index=index)
    values = {
        "skinTone": tone,
        "productTitle": title,
        "productDetails": details,
        "startPose": _extract_start_pose(hook.text, title),
        "bottoms": styling["bottoms"],
        "anklewear": styling["anklewear"],
        "nails": styling["nails"],
        "accessories": styling["accessories"],
        "sleeve": styling["sleeve"],
        "setting": _setting_text(chosen_setting, fmt, resolved_gender, seed, index),
        "samePerson": _same_person(fmt, tone, resolved_gender, styling["nails"]),
        "countLock": _count_lock(fmt),
        "frameGuard": FRAME_GUARD,
        "lighting": _lighting(lighting),
        "feet": _feet_or_hands(fmt, resolved_gender, tone, styling, video=False),
        "hands": _feet_or_hands(fmt, resolved_gender, tone, styling, video=False),
    }
    return _fill(IMAGE_TEMPLATE_WORN if fmt == "worn" else IMAGE_TEMPLATE_HELD, values)


def build_video_prompt(
    *,
    gender: str,
    skin_tone: str | None,
    product_title: str,
    product_description: str = "",
    format_name: str,
    beats: BeatSelection,
    seed: str,
    index: int,
    nails: str | None = None,
    accessories: str | None = None,
    lighting: str | None = None,
    key_detail: str | None = None,
    product_colour: str | None = None,
    product_branding: str | None = None,
    product_pattern_material: str | None = None,
    product_sole: str | None = None,
    product_closures: str | None = None,
    talking: bool = False,
    spoken_line: str | None = None,
) -> str:
    fmt = normalize_format(format_name)
    resolved_gender = _gender(gender)
    tone = normalize_skin_tone(skin_tone)
    title = _clean_line(product_title, "[product title missing]")
    details = _clean_line(product_description, title)
    styling = _resolved_styling(fmt=fmt, gender=resolved_gender, seed=seed, index=index, nails=nails, accessories=accessories)
    product_lock, resolved_branding = _product_lock(
        fmt=fmt,
        title=title,
        description=details,
        colour=product_colour,
        branding=product_branding,
        pattern_material=product_pattern_material,
        sole=product_sole,
        closures=product_closures,
    )
    detail = _key_detail(key_detail, resolved_branding)
    values = {
        "skinTone": tone,
        "productTitle": title,
        "HOOK": _fill(beats.hook.text, {"keyDetail": detail, "productTitle": title}),
        "MID": _fill(beats.mid.text, {"keyDetail": detail, "productTitle": title}),
        "CLOSER": _fill(beats.closer.text, {"keyDetail": detail, "productTitle": title}),
        "samePerson": _same_person(fmt, tone, resolved_gender, styling["nails"]),
        "productLock": product_lock,
        "countLock": _count_lock(fmt),
        "VOICE": _voice(resolved_gender, talking, spoken_line),
        "lighting": _lighting(lighting),
        "feet": _feet_or_hands(fmt, resolved_gender, tone, styling, video=True),
        "hands": _feet_or_hands(fmt, resolved_gender, tone, styling, video=True),
    }
    return _fill(VIDEO_TEMPLATE_WORN if fmt == "worn" else VIDEO_TEMPLATE_HELD, values)


def unfilled_placeholders(prompt: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(str(prompt or ""))))


def image_reference_caption(reference_count: int) -> str:
    count = max(1, min(4, int(reference_count)))
    refs = [f"@reference_{index}" for index in range(1, count + 1)]
    subject = refs[0] if count == 1 else " and ".join(refs)
    verb = "is" if count == 1 else "are"
    return f"{subject} {verb} the exact product; copy its colour, print, buttons, fabric."


__all__ = [
    "ALL_BEATS",
    "HELD_CLOSERS",
    "HELD_HOOKS",
    "HELD_MIDS",
    "IMAGE_TEMPLATE_HELD",
    "IMAGE_TEMPLATE_WORN",
    "MEN_ANKLEWEAR",
    "MEN_BOTTOMS",
    "MEN_SLEEVES",
    "SETTINGS",
    "VIDEO_TEMPLATE_HELD",
    "VIDEO_TEMPLATE_WORN",
    "WOMEN_ANKLEWEAR",
    "WOMEN_BOTTOMS",
    "WOMEN_NAILS",
    "WOMEN_SLEEVES",
    "WORN_CLOSERS",
    "WORN_HOOKS",
    "WORN_MIDS",
    "Beat",
    "BeatSelection",
    "Setting",
    "build_image_prompt",
    "build_video_prompt",
    "choose_beats",
    "format_for_item",
    "image_reference_caption",
    "normalize_format",
    "normalize_skin_tone",
    "resolve_setting_name",
    "unfilled_placeholders",
]
