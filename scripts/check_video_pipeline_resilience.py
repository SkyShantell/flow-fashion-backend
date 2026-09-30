#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import Base  # noqa: E402
from backend.models import Batch, ProductJob, QueueTask  # noqa: E402
from backend.services import drive, sheets  # noqa: E402
from backend import manual_ffmpeg, tasks  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def check_drive_retry_and_url_recovery() -> None:
    responses = [
        FakeResponse(404, text="temporary Apps Script miss"),
        FakeResponse(200, {"ok": True, "fileId": "drive-file-123"}),
    ]
    calls: list[dict] = []
    sleeps: list[int] = []
    original_settings = drive.settings
    original_post = drive.requests.post
    original_sleep = drive.time.sleep
    try:
        drive.settings = lambda: SimpleNamespace(
            google_drive_archive_webhook_url="https://drive-webhook.test/exec",
            google_drive_archive_secret="secret",
        )

        def fake_post(url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            return responses.pop(0)

        drive.requests.post = fake_post
        drive.time.sleep = sleeps.append
        payload, error = drive.archive_bytes(
            b"small mp4",
            "video/mp4",
            "final.mp4",
            "video",
            batch_name="Batch test",
            product_name="Product test",
            batch_date="2026-09-30",
            attempts=2,
        )
    finally:
        drive.settings = original_settings
        drive.requests.post = original_post
        drive.time.sleep = original_sleep

    check(not error, f"Drive retry returned an error: {error}")
    check(len(calls) == 2, "Drive did not retry a transient HTTP 404 exactly once")
    check(sleeps == [2], f"Drive retry delay changed: {sleeps}")
    check(payload is not None and payload["file_id"] == "drive-file-123", "Drive fileId was not normalized")
    check("drive-file-123" in payload["view_url"], "Drive view URL was not recovered from fileId")
    check("drive-file-123" in payload["download_url"], "Drive download URL was not recovered from fileId")


class FakeWorksheet:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def get_all_values(self):
        raise AssertionError("known tracker rows must not read the whole spreadsheet")

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class FakeBook:
    def __init__(self, worksheet: FakeWorksheet) -> None:
        self._worksheet = worksheet

    def worksheet(self, _title: str) -> FakeWorksheet:
        return self._worksheet


class FakeDb:
    def add(self, _value) -> None:
        return None

    def flush(self) -> None:
        return None


def check_sheet_known_row_skips_full_read() -> None:
    worksheet = FakeWorksheet()
    original_settings = sheets.settings
    original_credentials = sheets.google_service_account_info
    original_open_book = sheets.open_book
    try:
        sheets.settings = lambda: SimpleNamespace(google_sheet_auto_sync=True, google_sheet_url="sheet-key")
        sheets.google_service_account_info = lambda: {"client_email": "test@example.com"}
        sheets.open_book = lambda: (FakeBook(worksheet), SimpleNamespace(WorksheetNotFound=RuntimeError))
        job = ProductJob(id="job-known-row", batch_id="batch-known-row", sheet_row=12)
        ok, message = sheets.sync_job(job, FakeDb())
    finally:
        sheets.settings = original_settings
        sheets.google_service_account_info = original_credentials
        sheets.open_book = original_open_book

    check(ok, f"known-row sheet sync failed: {message}")
    check(len(worksheet.updates) == 1, "known-row sheet sync did not issue exactly one update")
    check(worksheet.updates[0]["range_name"] == "A12:AA12", "known-row sheet sync targeted the wrong range")


def check_sheet_queue_coalescing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with session_factory() as db:
        batch = Batch(id="batch-coalesce")
        job = ProductJob(
            id="job-coalesce",
            batch_id=batch.id,
            product_url="https://example.test/product",
        )
        db.add_all([batch, job])
        db.flush()
        first = tasks.enqueue_task(
            db,
            "sync_sheet",
            job_id=job.id,
            batch_id=batch.id,
            payload={"first": True},
            priority=300,
            allow_duplicate=True,
        )
        second = tasks.enqueue_task(
            db,
            "sync_sheet",
            job_id=job.id,
            batch_id=batch.id,
            payload={"second": True},
            priority=250,
            allow_duplicate=True,
        )
        queued = db.query(QueueTask).filter(QueueTask.task_type == "sync_sheet").all()

    check(first.id == second.id, "duplicate queued sheet sync was not coalesced")
    check(len(queued) == 1, "more than one queued sheet sync was created")
    check(second.priority == 250, "coalesced sheet sync did not retain the higher priority")
    check(second.payload == {"first": True, "second": True}, "coalesced sheet payload was not merged")


class FakeOverlayDb:
    def __init__(self, job: ProductJob, batch: Batch) -> None:
        self.job = job
        self.batch = batch

    def get(self, model, object_id: str):
        if model is ProductJob and object_id == self.job.id:
            return self.job
        if model is Batch and object_id == self.batch.id:
            return self.batch
        return None

    def add(self, _value) -> None:
        return None

    def flush(self) -> None:
        return None


def check_ffmpeg_uses_drive_first() -> None:
    batch = Batch(id="batch-overlay")
    job = ProductJob(
        id="job-overlay",
        batch_id=batch.id,
        product_url="https://example.test/product",
        product_name="Test product",
        video_status="completed",
        video_job_id="video-job-overlay",
        video_media_id="raw-video-media",
        video_url="https://example.test/raw.mp4",
    )
    task = QueueTask(
        id="task-overlay",
        task_type="apply_text_overlay",
        job_id=job.id,
        batch_id=batch.id,
        attempts=1,
        max_attempts=1,
        payload={"headline": "Test headline", "video_job_id": job.video_job_id},
    )
    enqueued: list[str] = []
    originals = {
        "settings": manual_ffmpeg.settings,
        "download": manual_ffmpeg._download_ffmpeg_source,
        "render": manual_ffmpeg.render_styled_overlay,
        "archive": manual_ffmpeg._archive_ffmpeg_output,
        "upload": manual_ffmpeg.useapi.upload_video_asset,
        "enqueue": manual_ffmpeg.tasks.enqueue_task,
    }
    try:
        manual_ffmpeg.settings = lambda: SimpleNamespace(
            google_drive_archive_webhook_url="https://drive-webhook.test/exec",
            google_drive_archive_secret="secret",
        )
        manual_ffmpeg._download_ffmpeg_source = lambda _job, _payload: b"raw mp4"
        manual_ffmpeg.render_styled_overlay = lambda *_args, **_kwargs: b"final mp4"
        manual_ffmpeg._archive_ffmpeg_output = lambda *_args, **_kwargs: {
            "ok": True,
            "file_id": "final-drive-file",
            "view_url": "https://drive.google.com/file/d/final-drive-file/view",
            "download_url": "https://drive.google.com/uc?export=download&id=final-drive-file",
        }

        def reject_flow_upload(*_args, **_kwargs):
            raise AssertionError("a healthy Drive path must not upload the rendered MP4 back to Flow")

        def fake_enqueue(_db, task_type: str, **_kwargs):
            enqueued.append(task_type)
            return SimpleNamespace(task_type=task_type)

        manual_ffmpeg.useapi.upload_video_asset = reject_flow_upload
        manual_ffmpeg.tasks.enqueue_task = fake_enqueue
        manual_ffmpeg.run_apply_text_overlay(FakeOverlayDb(job, batch), task)
    finally:
        manual_ffmpeg.settings = originals["settings"]
        manual_ffmpeg._download_ffmpeg_source = originals["download"]
        manual_ffmpeg.render_styled_overlay = originals["render"]
        manual_ffmpeg._archive_ffmpeg_output = originals["archive"]
        manual_ffmpeg.useapi.upload_video_asset = originals["upload"]
        manual_ffmpeg.tasks.enqueue_task = originals["enqueue"]

    check(job.video_media_id is None, "Drive final still points at the raw Flow media ID")
    check(job.drive_video_id == "final-drive-file", "Drive final file ID was not stored on the job")
    check(task.payload["fashion_text_overlay_storage"] == "drive", "FFmpeg did not record Drive as primary storage")
    check(enqueued == ["archive_media", "sync_sheet"], f"FFmpeg follow-up tasks changed: {enqueued}")


def check_captcha_error_classes() -> None:
    config_error = "CapSolver create failed: api key invalid or balance insufficient"
    transient_error = "CAPTCHA service failed: AntiCaptcha unavailable"
    check(
        any(marker in config_error.lower() for marker in tasks.FLOW_CAPTCHA_CONFIG_MARKERS),
        "CAPTCHA balance/configuration errors are not classified for fail-fast handling",
    )
    check(
        any(marker in transient_error.lower() for marker in tasks.FLOW_CAPTCHA_OUTAGE_MARKERS),
        "temporary CAPTCHA outages are not classified for cooldown handling",
    )


def main() -> None:
    check_drive_retry_and_url_recovery()
    check_sheet_known_row_skips_full_read()
    check_sheet_queue_coalescing()
    check_ffmpeg_uses_drive_first()
    check_captcha_error_classes()
    print("PASS: Drive retries transient failures and reconstructs missing URLs")
    print("PASS: known Google Sheet rows update without a spreadsheet-wide read")
    print("PASS: queued sheet sync tasks coalesce per job")
    print("PASS: FFmpeg writes to Drive first without a doomed Flow video upload")
    print("PASS: CAPTCHA balance failures and temporary outages are handled separately")


if __name__ == "__main__":
    main()
