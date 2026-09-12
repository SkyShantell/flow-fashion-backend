from __future__ import annotations

import hashlib
import threading
import time

from backend.services import useapi
import backend.tasks as tasks

_INSTALLED = False
_CACHE_LOCK = threading.Lock()
_CACHE_EMAILS: list[str] = []
_CACHE_UNTIL = 0.0
_CACHE_SECONDS = 60


def _available_flow_emails() -> list[str]:
    global _CACHE_EMAILS, _CACHE_UNTIL
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE_EMAILS and now < _CACHE_UNTIL:
            return list(_CACHE_EMAILS)

        rows = useapi.list_flow_accounts()
        all_emails: list[str] = []
        usable_emails: list[str] = []
        bad_health = {"error", "expired", "disabled", "invalid"}

        for row in rows:
            if not isinstance(row, dict):
                continue
            email = useapi.normalize_account_email(row.get("email"))
            if not email:
                continue
            all_emails.append(email)
            health = str(row.get("health") or "").strip().lower()
            if health not in bad_health:
                usable_emails.append(email)

        emails = usable_emails or all_emails
        emails = sorted(dict.fromkeys(emails), key=str.lower)
        if not emails:
            raise RuntimeError("No Google Flow account is available for image generation.")

        _CACHE_EMAILS = emails
        _CACHE_UNTIL = now + _CACHE_SECONDS
        return list(emails)


def resolve_batch_flow_account(batch) -> str:
    """Return one concrete Flow account for every asset in a batch.

    A manually selected account is always honored. Automatic mode keeps batch-level
    affinity: batches are deterministically spread across connected accounts, while every
    avatar/product/editorial reference in the same batch is migrated to that one account
    before generation. This prevents Flow's mismatched-reference-email failures without
    changing the saved Automatic setting in the dashboard.
    """
    if batch is None:
        return ""

    explicit = useapi.normalize_account_email(getattr(batch, "flow_account_email", None))
    if explicit:
        return explicit

    emails = _available_flow_emails()
    key = str(getattr(batch, "id", "") or getattr(batch, "name", "") or "flow-fashion")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return emails[int(digest[:12], 16) % len(emails)]


def install_flow_account_affinity() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    tasks._batch_flow_account = resolve_batch_flow_account
    _INSTALLED = True
