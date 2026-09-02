from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from backend.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18]}"


class Batch(Base):
    __tablename__ = "batches"

    id = Column(String(64), primary_key=True, default=lambda: new_id("batch"))
    name = Column(String(200), default="Flow batch")
    source = Column(String(80), default="manual")
    scene = Column(String(120), default="Modern apartment mirror")
    creator_profile = Column(String(40), default="Male")
    video_style = Column(String(40), default="Calm")
    auto_approve = Column(Boolean, default=False)

    avatar_b64 = Column(Text, nullable=True)
    avatar_mime = Column(String(80), default="image/jpeg")
    avatar_media_id = Column(String(500), nullable=True)

    status = Column(String(80), default="open")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    jobs = relationship("ProductJob", back_populates="batch", cascade="all, delete-orphan")


class ProductJob(Base):
    __tablename__ = "product_jobs"

    id = Column(String(64), primary_key=True, default=lambda: new_id("job"))
    batch_id = Column(String(64), ForeignKey("batches.id"), nullable=False, index=True)

    product_url = Column(Text, nullable=False)
    product_id = Column(String(160), nullable=True, index=True)
    product_name = Column(Text, default="Unknown Product")
    focus = Column(String(40), default="outfit")
    back_design = Column(Boolean, default=False)

    listing_images = Column(JSON, default=list)
    review_images = Column(JSON, default=list)
    selected_refs = Column(JSON, default=list)
    flow_product_ref_ids = Column(JSON, default=list)
    ref_signature = Column(String(80), nullable=True)

    stage = Column(String(80), default="pending_import", index=True)
    approved = Column(Boolean, default=False)

    image_status = Column(String(80), default="pending")
    image_job_id = Column(String(500), nullable=True)
    image_media_id = Column(String(500), nullable=True)
    image_url = Column(Text, nullable=True)
    image_seed = Column(String(120), nullable=True)
    image_error = Column(Text, nullable=True)

    video_status = Column(String(80), default="pending")
    video_job_id = Column(String(500), nullable=True)
    video_source_media_id = Column(String(500), nullable=True)
    video_source_url = Column(Text, nullable=True)
    video_source_resolution = Column(String(40), nullable=True)
    thumbnail_url = Column(Text, nullable=True)
    video_error = Column(Text, nullable=True)

    upscale_status = Column(String(80), default="pending")
    upscale_job_id = Column(String(500), nullable=True)
    video_media_id = Column(String(500), nullable=True)  # final/upscaled ID
    video_url = Column(Text, nullable=True)              # final/upscaled URL
    video_resolution = Column(String(40), nullable=True)
    upscale_error = Column(Text, nullable=True)

    drive_image_id = Column(String(160), nullable=True)
    drive_image_url = Column(Text, nullable=True)
    drive_image_download_url = Column(Text, nullable=True)
    drive_video_id = Column(String(160), nullable=True)
    drive_video_url = Column(Text, nullable=True)
    drive_video_download_url = Column(Text, nullable=True)
    drive_product_folder_url = Column(Text, nullable=True)
    drive_batch_folder_url = Column(Text, nullable=True)
    drive_error = Column(Text, nullable=True)

    sheet_row = Column(Integer, nullable=True)

    image_attempts = Column(Integer, default=0)
    video_attempts = Column(Integer, default=0)
    upscale_attempts = Column(Integer, default=0)
    archive_attempts = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)

    regen_instruction = Column(Text, nullable=True)
    last_regen_instruction = Column(Text, nullable=True)

    scanner_row_num = Column(Integer, nullable=True)
    scanner_creator_count = Column(Integer, nullable=True)
    scanner_video_count = Column(Integer, nullable=True)
    scanner_combined_views = Column(Integer, nullable=True)
    scanner_creators = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    batch = relationship("Batch", back_populates="jobs")
    tasks = relationship("QueueTask", back_populates="job", cascade="all, delete-orphan")


class QueueTask(Base):
    __tablename__ = "queue_tasks"

    id = Column(String(64), primary_key=True, default=lambda: new_id("task"))
    task_type = Column(String(80), nullable=False, index=True)
    status = Column(String(40), default="queued", index=True)  # queued/running/done/failed
    priority = Column(Integer, default=100, index=True)

    batch_id = Column(String(64), ForeignKey("batches.id"), nullable=True, index=True)
    job_id = Column(String(64), ForeignKey("product_jobs.id"), nullable=True, index=True)
    payload = Column(JSON, default=dict)

    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    error = Column(Text, nullable=True)

    run_after = Column(DateTime(timezone=True), default=utcnow, index=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    job = relationship("ProductJob", back_populates="tasks")
