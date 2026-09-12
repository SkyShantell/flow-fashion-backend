from __future__ import annotations

from pydantic import BaseModel, Field


class SaveAvatarRequest(BaseModel):
    name: str = "Saved avatar"
    image_b64: str
    image_mime: str = "image/jpeg"


class AvatarOut(BaseModel):
    id: str
    name: str
    image_b64: str
    image_mime: str


class CreateBatchRequest(BaseModel):
    name: str = "Flow batch"
    mode: str = "fashion_tryon"
    scene: str = "Modern apartment mirror"
    scene_pool: list[str] = Field(default_factory=list)
    creator_profile: str = "Male"
    video_style: str = "Academy — Boss / Calm"
    motion_pool: list[str] = Field(default_factory=list)
    auto_approve: bool = False
    avatar_b64: str | None = None
    avatar_mime: str = "image/jpeg"
    avatar_name: str | None = None
    flow_account_email: str | None = None
    video_provider: str = "omni"
    kling_account_email: str | None = None
    kling_model: str = "kling-v3-0"
    kling_mode: str = "pro"


class ImportProductsRequest(BaseModel):
    links: list[str] = Field(default_factory=list)
    start_generation: bool = True


class ImportScannerRequest(BaseModel):
    row_nums: list[int] | None = None
    max_items: int = 10
    start_generation: bool = True


class SelectProductRefsRequest(BaseModel):
    refs: list[str] = Field(default_factory=list)
    start_generation: bool = True
    focus: str | None = None
    scene: str | None = None
    motion_style: str | None = None


class UpdateFlowAccountRequest(BaseModel):
    flow_account_email: str | None = None


class UpdateVideoProviderRequest(BaseModel):
    video_provider: str = "omni"
    kling_account_email: str | None = None
    kling_model: str = "kling-v3-0"
    kling_mode: str = "pro"


class UpdateJobSettingsRequest(BaseModel):
    focus: str | None = None
    scene: str | None = None
    motion_style: str | None = None


class ApproveJobRequest(BaseModel):
    approved: bool = True
    start_video: bool = True


class RegenerateJobRequest(BaseModel):
    instruction: str = ""


class RetryJobRequest(BaseModel):
    step: str = "auto"  # auto/import/image/video/upscale/archive/sheet


class RegenerateVideoRequest(BaseModel):
    prompt: str = ""


class EditorialRegenerateRequest(BaseModel):
    instruction: str = ""
    prompt: str = ""


class ApplyTextOverlayRequest(BaseModel):
    headline: str = ""
    subheadline: str = ""
    preset: str = "luxury_serif"
    emoji_prefix: str = ""
    emoji_suffix: str = ""
    # Transparent PNG data URLs rendered by the user's browser. On macOS/iOS the
    # browser uses Apple Color Emoji, so Railway never needs Apple's proprietary font.
    emoji_prefix_pngs: list[str] = Field(default_factory=list)
    emoji_suffix_pngs: list[str] = Field(default_factory=list)
    headline_color: str = "white"
    subheadline_color: str = "white"
    placement: str = "middle"


class JobOut(BaseModel):
    id: str
    batch_id: str
    product_name: str | None
    product_url: str | None
    product_id: str | None
    focus: str | None
    scene: str | None = None
    motion_style: str | None = None
    listing_images: list[str] = Field(default_factory=list)
    review_images: list[str] = Field(default_factory=list)
    selected_refs: list[str] = Field(default_factory=list)
    editorial_shots: list[dict] = Field(default_factory=list)
    stage: str
    approved: bool
    image_status: str
    image_url: str | None
    video_status: str
    upscale_status: str
    video_url: str | None
    video_resolution: str | None
    video_provider_used: str | None = None
    video_provider_account: str | None = None
    drive_video_url: str | None
    drive_video_download_url: str | None = None
    error: str | None = None


class BatchOut(BaseModel):
    id: str
    name: str | None
    avatar_name: str | None = None
    flow_account_email: str | None = None
    video_provider: str = "omni"
    kling_account_email: str | None = None
    kling_model: str = "kling-v3-0"
    kling_mode: str = "pro"
    mode: str = "fashion_tryon"
    scene: str | None
    scene_pool: list[str] = Field(default_factory=list)
    creator_profile: str | None
    video_style: str | None
    motion_pool: list[str] = Field(default_factory=list)
    auto_approve: bool
    status: str
    counts: dict
    jobs: list[JobOut]
