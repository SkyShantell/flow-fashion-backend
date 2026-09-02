from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw == "":
        return default
    return raw not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    # Providers
    useapi_token: str = os.getenv("USEAPI_TOKEN", "").strip()
    google_flow_email: str = os.getenv("GOOGLE_FLOW_EMAIL", "").strip()
    sociavault_api_key: str = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    sociavault_region: str = os.getenv("SOCIAVAULT_REGION", "US").strip() or "US"

    # useapi / Flow
    flow_base: str = os.getenv("FLOW_BASE", "https://api.useapi.net/v1/google-flow").strip().rstrip("/")
    image_model: str = os.getenv("IMAGE_MODEL", "nano-banana-pro").strip()
    video_model: str = os.getenv("VIDEO_MODEL", "omni-flash").strip()
    video_duration: int = _int_env("VIDEO_DURATION", 8)
    video_native_resolution: str = os.getenv("VIDEO_NATIVE_RESOLUTION", "720p").strip() or "720p"
    video_final_resolution: str = os.getenv("VIDEO_FINAL_RESOLUTION", "1080p").strip() or "1080p"

    # Queue / worker speed controls
    worker_concurrency: int = _int_env("WORKER_CONCURRENCY", 4)
    poll_seconds: int = _int_env("POLL_SECONDS", 15)
    task_backoff_seconds: int = _int_env("TASK_BACKOFF_SECONDS", 45)
    max_product_refs: int = _int_env("MAX_PRODUCT_REFS", 5)
    max_batch_links: int = _int_env("MAX_BATCH_LINKS", 100)

    # Google outputs
    google_sheet_url: str = os.getenv("GOOGLE_SHEET_URL", "").strip()
    google_drive_archive_webhook_url: str = (
        os.getenv("GOOGLE_DRIVE_ARCHIVE_WEBHOOK_URL", "").strip()
        or os.getenv("GOOGLE_DRIVE_ARCHIVE_URL", "").strip()
    )
    google_drive_archive_secret: str = os.getenv("GOOGLE_DRIVE_ARCHIVE_SECRET", "").strip()
    google_drive_auto_archive: bool = _bool_env("GOOGLE_DRIVE_AUTO_ARCHIVE", True)
    google_sheet_auto_sync: bool = _bool_env("GOOGLE_SHEET_AUTO_SYNC", True)

    # API
    api_key: str = os.getenv("PHASE1_API_KEY", "").strip()


def settings() -> Settings:
    return Settings()


def google_service_account_info() -> dict | None:
    """Load Google service account JSON from env.

    For Railway this should be a single env var:
    GOOGLE_SERVICE_ACCOUNT_JSON={...entire json...}
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
