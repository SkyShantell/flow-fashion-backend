from __future__ import annotations

from pydantic import BaseModel, Field


class CreateBatchRequest(BaseModel):
    name: str = "Flow batch"
    scene: str = "Modern apartment mirror"
    creator_profile: str = "Male"
    video_style: str = "Calm"
    auto_approve: bool = False
    avatar_b64: str | None = None
    avatar_mime: str = "image/jpeg"


class ImportProductsRequest(BaseModel):
    links: list[str] = Field(default_factory=list)
    start_generation: bool = True


class ImportScannerRequest(BaseModel):
    row_nums: list[int] | None = None
    max_items: int = 10
    start_generation: bool = True


class ApproveJobRequest(BaseModel):
    approved: bool = True
    start_video: bool = True


class RegenerateJobRequest(BaseModel):
    instruction: str = ""


class RetryJobRequest(BaseModel):
    step: str = "auto"  # auto/import/image/video/upscale/archive/sheet


class JobOut(BaseModel):
    id: str
    batch_id: str
    product_name: str | None
    product_url: str | None
    product_id: str | None
    focus: str | None
    stage: str
    approved: bool
    image_status: str
    image_url: str | None
    video_status: str
    upscale_status: str
    video_url: str | None
    video_resolution: str | None
    drive_video_url: str | None
    error: str | None = None


class BatchOut(BaseModel):
    id: str
    name: str | None
    scene: str | None
    creator_profile: str | None
    video_style: str | None
    auto_approve: bool
    status: str
    counts: dict
    jobs: list[JobOut]
