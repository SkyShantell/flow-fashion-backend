from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import requests

from backend.config import settings
from backend.services.sociavault import classify_focus, normalize_remote_url

TIKHUB_BASE = "https://api.tikhub.io/api/v1/tiktok/shop/web"
DETAIL_V3 = f"{TIKHUB_BASE}/fetch_product_detail_v3"
REVIEWS_V2 = f"{TIKHUB_BASE}/fetch_product_reviews_v2"


def extract_product_id(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise RuntimeError("TikHub requires a TikTok Shop product URL.")

    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        for key in ("product_id", "placeholder_product_id", "productId"):
            value = str((query.get(key) or [""])[0]).strip()
            if value.isdigit():
                return value
        path = parsed.path or ""
    except Exception:
        path = text

    patterns = (
        r"/view/product/(\d{15,24})",
        r"/shop/pdp/(?:[^/?#]*-)?(\d{15,24})(?:[/?#]|$)",
        r"/product/(\d{15,24})(?:[/?#]|$)",
        r"(?:product_id|placeholder_product_id)=([0-9]{15,24})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)

    candidates = re.findall(r"(?<!\d)(\d{17,24})(?!\d)", path)
    if candidates:
        return candidates[-1]
    raise RuntimeError("Could not extract a TikTok Shop product ID from this URL for TikHub.")


def _get(endpoint: str, params: dict) -> dict:
    cfg = settings()
    if not cfg.tikhub_api_key:
        raise RuntimeError("Missing TIKHUB_API_KEY")

    headers = {
        "Authorization": f"Bearer {cfg.tikhub_api_key}",
        "Accept": "application/json",
        "User-Agent": "FlowFashion/1.0",
    }
    last_error = ""
    for attempt in range(3):
        try:
            resp = requests.get(endpoint, headers=headers, params=params, timeout=35)
            if resp.status_code == 400 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                body = resp.text[:800]
                raise RuntimeError(f"TikHub HTTP {resp.status_code}: {body}")
            payload = resp.json()
            if not isinstance(payload, dict):
                raise RuntimeError("TikHub returned an invalid response.")
            code = payload.get("code")
            if code not in (None, 0, 200, "0", "200"):
                raise RuntimeError(str(payload.get("message") or payload.get("message_en") or f"TikHub error {code}"))
            data = payload.get("data")
            if data is None:
                raise RuntimeError(str(payload.get("message") or "TikHub returned no product data."))
            return data if isinstance(data, dict) else {"items": data}
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2 and ("HTTP 400" in last_error or "timed out" in last_error.lower() or "timeout" in last_error.lower()):
                time.sleep(1.5 * (attempt + 1))
                continue
            break
    raise RuntimeError(last_error or "TikHub request failed.")


def _first_text(node, keys: tuple[str, ...], blocked: tuple[str, ...] = ()) -> str:
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key, value in node.items():
            low = str(key).lower()
            if any(part in low for part in blocked):
                continue
            found = _first_text(value, keys, blocked)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _first_text(value, keys, blocked)
            if found:
                return found
    return ""


def _collect_image_urls(node, *, review_mode: bool = False, max_depth: int = 10) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        url = normalize_remote_url(value)
        if not url or url in seen:
            return
        low = url.lower()
        if any(x in low for x in ("avatar", "profile", "shop_logo", "seller_logo")):
            return
        seen.add(url)
        urls.append(url)

    def walk(value, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > max_depth:
            return
        joined = " ".join(path).lower()
        if any(x in joined for x in ("avatar", "profile", "seller", "shopinfo", "shop_info", "logo", "icon")):
            return
        if isinstance(value, str):
            if review_mode:
                allowed = any(x in joined for x in ("image", "img", "photo", "picture", "media", "cover"))
            else:
                allowed = any(x in joined for x in ("image", "img", "picture", "cover", "productinfo", "product_info"))
            if allowed and not any(x in joined for x in ("video", "play", "aweme")):
                add(value)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, path, depth + 1)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + (str(key).lower(),), depth + 1)

    walk(node)
    return urls


def import_product(url: str, region: str = "GB") -> dict:
    region_code = str(region or "GB").strip().upper()
    if region_code == "UK":
        region_code = "GB"
    if region_code != "GB":
        raise RuntimeError("TikHub importer is currently reserved for UK / GB products.")

    product_id = extract_product_id(url)
    detail = _get(DETAIL_V3, {"product_id": product_id, "region": region_code})
    product_info = detail.get("productInfo") or detail.get("product_info") or detail
    if not isinstance(product_info, dict):
        product_info = detail

    title = _first_text(
        product_info,
        ("title", "product_title", "productTitle", "name", "product_name", "productName"),
        blocked=("seller", "shop", "brand", "category"),
    ) or "Unknown Product"

    listing_images = _collect_image_urls(product_info, review_mode=False)[:18]

    review_images: list[str] = []
    try:
        review_data = _get(
            REVIEWS_V2,
            {
                "product_id": product_id,
                "page_start": 1,
                "sort_rule": 2,
                "filter_type": 2,
                "filter_value": 1,
                "region": region_code,
            },
        )
        review_root = review_data.get("reviews") or review_data.get("review_list") or review_data
        review_images = _collect_image_urls(review_root, review_mode=True)[:24]
    except Exception:
        # Listing images are enough to continue if TikHub review media is temporarily unavailable.
        review_images = []

    listing_set = set(listing_images)
    review_images = [u for u in review_images if u not in listing_set]
    if not listing_images and not review_images:
        raise RuntimeError("TikHub returned the UK product but no usable product images.")

    selected_refs = (listing_images[:2] + review_images[:1])[: settings().max_product_refs]
    if not selected_refs:
        selected_refs = (listing_images + review_images)[:3]

    return {
        "product_id": product_id,
        "product_name": title,
        "sociavault_region": region_code,
        "listing_images": listing_images,
        "review_images": review_images,
        "selected_refs": selected_refs,
        "focus": classify_focus(title),
        "provider": "tikhub",
    }
