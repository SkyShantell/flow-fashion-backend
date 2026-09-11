from __future__ import annotations

from datetime import datetime, timezone

from backend.config import google_service_account_info, settings

SCANNER_QUEUE_TAB = "Scanner Queue"
TRACKER_TAB = "Flow Try-On"

TRACKER_HEADERS = [
    "Product #", "Product name", "Product link", "Product ID", "Stage", "Image status", "Approved",
    "Image URL", "Image media ID", "Video status", "Upscale status", "Video URL", "Video media ID",
    "Video resolution", "Video job ID", "Upscale job ID", "Drive image", "Drive video", "Archive folder",
    "Error", "Image calls", "Video calls", "Upscale calls", "Failures", "Sheet row", "Job ID", "Batch ID",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def open_book():
    info = google_service_account_info()
    cfg = settings()
    if not info or not cfg.google_sheet_url:
        raise RuntimeError("Google Sheets is not configured.")
    import gspread
    gc = gspread.service_account_from_dict(info)
    ref = cfg.google_sheet_url.strip()
    book = gc.open_by_url(ref) if ref.startswith(("http://", "https://")) else gc.open_by_key(ref)
    return book, gspread


def hyperlink(url: str, label: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    return f'=HYPERLINK("{url.replace(chr(34), chr(34)*2)}","{str(label or "Open").replace(chr(34), chr(34)*2)}")'


def scanner_pending() -> tuple[list[dict], str]:
    try:
        book, gspread = open_book()
        try:
            ws = book.worksheet(SCANNER_QUEUE_TAB)
        except gspread.WorksheetNotFound:
            return [], ""
        values = ws.get_all_values()
        if len(values) < 2:
            return [], ""
        headers = values[0]
        rows = []
        for row_num, raw in enumerate(values[1:], start=2):
            padded = raw + [""] * max(0, len(headers) - len(raw))
            rec = dict(zip(headers, padded))
            rec["_row_num"] = row_num
            status = str(rec.get("Status") or "Pending").strip().lower()
            link = str(rec.get("Product Link") or "").strip()
            if status in {"", "pending", "queued"} and link:
                rows.append(rec)

        def n(value):
            try:
                return int(float(str(value or "0").replace(",", "")))
            except Exception:
                return 0

        rows.sort(key=lambda r: (n(r.get("Creator Count")), n(r.get("Video Count")), n(r.get("Combined Views"))), reverse=True)
        return rows, ""
    except Exception as exc:
        return [], str(exc)


def mark_scanner_rows(row_nums: list[int], status: str, batch_id: str = "") -> str:
    if not row_nums:
        return ""
    try:
        book, gspread = open_book()
        ws = book.worksheet(SCANNER_QUEUE_TAB)
        stamped = utc_now_iso()
        for row_num in row_nums:
            if int(row_num) >= 2:
                ws.update(range_name=f"J{int(row_num)}:L{int(row_num)}", values=[[status, stamped, batch_id]], value_input_option="USER_ENTERED")
        return ""
    except Exception as exc:
        return str(exc)


def ensure_tracker_sheet():
    book, gspread = open_book()
    try:
        ws = book.worksheet(TRACKER_TAB)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=TRACKER_TAB, rows=1000, cols=len(TRACKER_HEADERS) + 3)
    existing = ws.get_all_values()
    ws.update(range_name="A1", values=[TRACKER_HEADERS], value_input_option="USER_ENTERED")
    if not existing:
        existing = [TRACKER_HEADERS]
    return book, ws, existing


def job_to_tracker_row(job, product_number: int, sheet_row: int) -> list[str]:
    err = job.image_error or job.video_error or job.upscale_error or job.drive_error or ""
    return [
        str(product_number),
        str(job.product_name or ""),
        hyperlink(job.product_url, "Open product"),
        str(job.product_id or ""),
        str(job.stage or ""),
        str(job.image_status or ""),
        "Yes" if job.approved else "No",
        hyperlink(job.image_url, "View image"),
        str(job.image_media_id or ""),
        str(job.video_status or ""),
        str(job.upscale_status or ""),
        hyperlink(job.video_url or job.drive_video_download_url, "View video"),
        str(job.video_media_id or ""),
        str(job.video_resolution or ""),
        str(job.video_job_id or ""),
        str(job.upscale_job_id or ""),
        hyperlink(job.drive_image_url, "Drive image"),
        hyperlink(job.drive_video_url, "Drive video"),
        hyperlink(job.drive_product_folder_url or job.drive_batch_folder_url, "Archive"),
        str(err),
        str(job.image_attempts or 0),
        str(job.video_attempts or 0),
        str(job.upscale_attempts or 0),
        str(job.failure_count or 0),
        str(sheet_row),
        str(job.id),
        str(job.batch_id),
    ]


def sync_job(job, db) -> tuple[bool, str]:
    cfg = settings()
    if not cfg.google_sheet_auto_sync or not cfg.google_sheet_url or not google_service_account_info():
        return False, "Google Sheets auto-sync is not configured."
    try:
        _book, ws, existing = ensure_tracker_sheet()
        row_by_job = {}
        max_product = 0
        for row_idx, raw in enumerate(existing[1:], start=2):
            if not raw:
                continue
            try:
                max_product = max(max_product, int(str(raw[0] or "0").strip() or 0))
            except Exception:
                pass
            if len(raw) >= 26 and raw[25]:
                row_by_job[str(raw[25])] = row_idx

        target_row = int(job.sheet_row or 0) or row_by_job.get(str(job.id)) or (len(existing) + 1)
        if target_row < 2:
            target_row = len(existing) + 1
        if job.sheet_row:
            product_number = None
            if target_row - 1 < len(existing):
                old = existing[target_row - 1]
                try:
                    product_number = int(str(old[0] or "0").strip() or 0)
                except Exception:
                    product_number = None
            product_number = product_number or max(1, target_row - 1)
        else:
            product_number = max_product + 1

        job.sheet_row = target_row
        db.add(job)
        db.flush()
        last_col = chr(ord("A") + len(TRACKER_HEADERS) - 1) if len(TRACKER_HEADERS) <= 26 else "AA"
        row = job_to_tracker_row(job, product_number, target_row)
        ws.update(range_name=f"A{target_row}:{last_col}{target_row}", values=[row], value_input_option="USER_ENTERED")
        return True, f"Synced row {target_row}."
    except Exception as exc:
        return False, f"Google Sheets sync failed: {exc}"
