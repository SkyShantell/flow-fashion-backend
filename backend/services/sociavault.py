from __future__ import annotations

import hashlib
import html
import re

import requests

from backend.config import settings
from backend.services.useapi import parse_error, normalize_image_bytes

SOCIA_BASE = "https://api.sociavault.com/v1"
SOCIA_PRODUCT_DETAILS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-details"
SOCIA_PRODUCT_REVIEWS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-reviews"


def normalize_remote_url(value) -> str:
    if value is None:
        return ""
    url = html.unescape(str(value)).strip().strip('"\'')
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return ""


def sv_values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        return [value]
    return []


def sv_first_url(value) -> str:
    if isinstance(value, str):
        return normalize_remote_url(value)
    if not isinstance(value, dict):
        return ""
    for key in ("url_list", "urlList", "urls", "review_images", "reviewImages", "images"):
        for candidate in sv_values(value.get(key)):
            url = sv_first_url(candidate)
            if url:
                return url
    for key in ("url", "image_url", "imageUrl", "display_image_url", "displayImageUrl", "original_url", "originalUrl", "preview_url", "previewUrl", "src"):
        url = sv_first_url(value.get(key))
        if url:
            return url
    for key in ("thumb_url_list", "thumbUrlList", "thumbnail_url", "thumbnailUrl"):
        url = sv_first_url(value.get(key))
        if url:
            return url
    return ""


def sv_collect_urls(value, max_depth=8):
    urls = []

    def add(url):
        url = normalize_remote_url(url)
        if url and url not in urls:
            urls.append(url)

    def walk(node, depth=0, path=()):
        if depth > max_depth:
            return
        p = " ".join(path).lower()
        if isinstance(node, str):
            if not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(node)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child, depth + 1, path)
        elif isinstance(node, dict):
            best = sv_first_url(node)
            if best and not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(best)
            for key, child in node.items():
                walk(child, depth + 1, path + (str(key).lower(),))

    walk(value)
    return urls


def dedupe(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def sociavault_get(endpoint: str, params: dict) -> dict:
    cfg = settings()
    if not cfg.sociavault_api_key:
        raise RuntimeError("Missing SOCIAVAULT_API_KEY")
    resp = requests.get(endpoint, headers={"X-API-Key": cfg.sociavault_api_key, "Accept": "application/json"}, params=params, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError(f"SociaVault HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or payload.get("error") or "SociaVault request failed."))
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("SociaVault returned no product data.")
    return data


def classify_focus(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    tokens = set(text.split())
    shoes = {"shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "heel", "heels", "sandal", "sandals", "loafer", "loafers", "clog", "clogs", "slipper", "slippers", "slides"}
    tops = {"shirt", "tee", "hoodie", "sweater", "jacket", "coat", "blouse", "top", "tank", "cardigan", "jersey", "polo"}
    bottoms = {"pants", "pant", "jeans", "jean", "shorts", "leggings", "legging", "jogger", "joggers", "trouser", "trousers", "skirt", "cargo"}
    outfit_phrases = ("two piece", "2 piece", "matching set", "tracksuit", "jumpsuit", "romper")
    outfit_tokens = {"set", "outfit", "suit", "dress"}
    if tokens & shoes:
        return "shoes"
    if any(p in text for p in outfit_phrases) or tokens & outfit_tokens:
        return "outfit"
    if tokens & tops or "t shirt" in text:
        return "shirt"
    if tokens & bottoms:
        return "pants"
    return "outfit"


def import_product(url: str) -> dict:
    cfg = settings()
    data = sociavault_get(SOCIA_PRODUCT_DETAILS, {"url": url, "get_related_videos": "false", "region": cfg.sociavault_region})
    product = data.get("product_base") or data.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    name = str(product.get("title") or product.get("name") or "Unknown Product").strip()
    product_id = str(data.get("product_id") or product.get("id") or hashlib.sha1(url.encode()).hexdigest()[:12])

    listing = []
    raw_images = product.get("images")
    for obj in sv_values(raw_images):
        u = normalize_remote_url(obj) if isinstance(obj, str) else sv_first_url(obj)
        if u:
            listing.append(u)
    if not listing:
        listing = sv_collect_urls(raw_images or product)[:18]
    listing = dedupe([normalize_remote_url(u) for u in listing if normalize_remote_url(u)])[:18]

    reviews = []
    review_block = data.get("product_detail_review") or {}
    review_items = sv_values(review_block.get("review_items") if isinstance(review_block, dict) else None)
    for item in review_items:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        reviews.extend(sv_collect_urls({
            "images": review.get("images"),
            "media": review.get("media"),
            "review_images": review.get("review_images"),
            "display_image_url": review.get("display_image_url"),
        }))
    reviews = [u for u in dedupe(reviews) if u not in set(listing)]
    if not reviews and product_id:
        try:
            review_data = sociavault_get(SOCIA_PRODUCT_REVIEWS, {"product_id": product_id, "page": 1})
            review_root = review_data.get("product_reviews") or review_data.get("reviews") or review_data
            for review in sv_values(review_root):
                if isinstance(review, dict):
                    reviews.extend(sv_collect_urls(review))
        except Exception:
            pass
    reviews = [u for u in dedupe(reviews) if u not in set(listing)][:24]
    if not listing and not reviews:
        raise RuntimeError("No usable product images were returned.")
    default_refs = dedupe(listing[:2] + reviews[:1])[:cfg.max_product_refs]
    if not default_refs:
        default_refs = dedupe(listing + reviews)[:3]
    return {
        "product_id": product_id,
        "product_name": name,
        "listing_images": listing,
        "review_images": reviews,
        "selected_refs": default_refs,
        "focus": classify_focus(name),
    }


def fetch_remote_image(url: str) -> tuple[bytes, str]:
    url = normalize_remote_url(url)
    if not url:
        raise RuntimeError("Invalid image URL")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "image/webp,image/jpeg,image/png,image/*;q=0.8,*/*;q=0.4",
        "Referer": "https://www.tiktok.com/",
        "Cache-Control": "no-cache",
    }
    resp = requests.get(url, timeout=45, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    if not resp.content or len(resp.content) < 64:
        raise RuntimeError("Image response was empty")
    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].lower()
    if mime.startswith("text/") or "json" in mime:
        raise RuntimeError(f"CDN returned {mime}, not an image")
    return normalize_image_bytes(resp.content, mime, max_side=1400, quality=88)
