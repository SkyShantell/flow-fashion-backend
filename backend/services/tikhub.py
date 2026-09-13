from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import requests

from backend.config import settings
from backend.services.sociavault import classify_focus, normalize_remote_url

TIKHUB_BASE = "https://api.tikhub.io/api/v1/tiktok/shop/web"
DETAIL_V3 = f"{TIKHUB_BASE}/fetch_product_detail_v3"
DETAIL_V1 = f"{TIKHUB_BASE}/fetch_product_detail"
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


def _collect_image_urls(node, *, review_mode: bool = False, max_depth: int = 12) -> list[str]:
    """Collect real product/review images, ranking product galleries above UI artwork."""
    scored: dict[str, tuple[int, int]] = {}
    order = 0

    def add(value, path: tuple[str, ...]) -> None:
        nonlocal order
        url = normalize_remote_url(value)
        if not url:
            return
        low_url = url.lower()
        path_text = " ".join(path).lower()
        combined = f"{path_text} {low_url}"

        # TikHub productInfo also contains UI artwork. Never allow that to become a product ref.
        if any(x in combined for x in (
            "avatar", "profile", "seller_logo", "shop_logo", "shopinfo", "shop_info",
            "favicon", "qrcode", "qr_code", "sprite", "placeholder", "tiktok-logo",
            "tiktok_logo", "app_icon", "share_icon",
        )):
            return
        if any(x in path_text for x in ("seller", "shop logo", "shop_logo", "logo", "icon")):
            return
        if not any(x in low_url for x in (".jpg", ".jpeg", ".png", ".webp", "image", "img", "byteimg", "ibytedtos", "ibyteimg")):
            return

        strong = (
            "main_image", "mainimage", "main_images", "mainimages",
            "product_image", "productimage", "product_images", "productimages",
            "sku_image", "skuimage", "sku_images", "skuimages",
            "image_list", "imagelist", "images", "image_urls", "imageurls",
        )
        score = 0
        if any(x in path_text for x in strong):
            score += 100
        elif any(x in path_text for x in ("image", "img", "photo", "picture", "media", "cover", "thumb")):
            score += 45
        else:
            return

        if review_mode:
            if "review" in path_text or "comment" in path_text:
                score += 80
        else:
            if "product" in path_text or "sku" in path_text:
                score += 50
            if "cover" in path_text:
                score += 15
        if any(x in low_url for x in ("byteimg", "ibytedtos", "ibyteimg")):
            score += 10

        order += 1
        previous = scored.get(url)
        if previous is None or score > previous[0]:
            scored[url] = (score, order)

    def walk(value, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > max_depth:
            return
        if isinstance(value, str):
            add(value, path)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, path, depth + 1)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + (str(key).lower(),), depth + 1)

    walk(node)
    ranked = sorted(scored.items(), key=lambda item: (-item[1][0], item[1][1]))
    return [url for url, _meta in ranked]


def _merge_unique(*groups: list[str], limit: int = 30) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value and value not in seen:
                seen.add(value)
                out.append(value)
                if len(out) >= limit:
                    return out
    return out


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

    # TikHub recommends V3 for all regions, but the desktop detail endpoint sometimes
    # exposes a cleaner product gallery. Only call it when V3 did not yield 2+ photos.
    if len(listing_images) < 2:
        try:
            detail_v1 = _get(
                DETAIL_V1,
                {"product_id": product_id, "seller_id": "", "region": region_code},
            )
            global_data = detail_v1.get("global_data") or detail_v1.get("globalData") or {}
            v1_product = (
                global_data.get("product_info")
                or global_data.get("productInfo")
                or detail_v1.get("product_info")
                or detail_v1.get("productInfo")
                or {}
            )
            if isinstance(v1_product, dict):
                if title == "Unknown Product":
                    title = _first_text(
                        v1_product,
                        ("title", "product_title", "productTitle", "name", "product_name", "productName"),
                        blocked=("seller", "shop", "brand", "category"),
                    ) or title
                listing_images = _merge_unique(
                    listing_images,
                    _collect_image_urls(v1_product, review_mode=False),
                    limit=18,
                )
        except Exception:
            pass

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
        review_images = []

    listing_images = _merge_unique(listing_images, limit=18)
    listing_set = set(listing_images)
    review_images = [u for u in _merge_unique(review_images, limit=24) if u not in listing_set]

    if not listing_images and not review_images:
        raise RuntimeError("TikHub returned the UK product but no usable product images.")

    selected_refs = _merge_unique(listing_images[:2], review_images[:1], limit=settings().max_product_refs)
    if not selected_refs:
        selected_refs = _merge_unique(listing_images, review_images, limit=3)

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
