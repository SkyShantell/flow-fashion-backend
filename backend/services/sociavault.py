from __future__ import annotations

import hashlib
import html
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from backend.config import settings
from backend.services.useapi import parse_error, normalize_image_bytes

SOCIA_BASE = "https://api.sociavault.com/v1"
SOCIA_PRODUCT_DETAILS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-details"
SOCIA_PRODUCT_REVIEWS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-reviews"
log = logging.getLogger("flow-sociavault")


def product_title(data: dict) -> str:
    """Find the product title across SociaVault's flat and nested response shapes."""
    blocked = {"seller", "shop", "review", "related", "video", "category", "brand", "sku"}

    def walk(node, depth=0, allow_name=False):
        if depth > 7 or not isinstance(node, dict):
            return ""
        for key, value in node.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"title", "producttitle", "productname"} and isinstance(value, str):
                candidate = html.unescape(value).strip()
                if candidate and candidate.lower() != "unknown product":
                    return candidate
        if allow_name and isinstance(node.get("name"), str):
            candidate = html.unescape(node["name"]).strip()
            if candidate and candidate.lower() != "unknown product":
                return candidate
        for key, value in node.items():
            if any(word in str(key).lower() for word in blocked):
                continue
            if isinstance(value, dict):
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                result = walk(value, depth + 1, normalized in {"productbase", "productinfo", "productdetail", "product"})
                if result:
                    return result
        return ""

    for key in ("product_base", "productBase", "product", "product_info", "productInfo", "product_detail", "productDetail", "data"):
        node = data.get(key)
        if isinstance(node, dict):
            name = walk(node, allow_name=key != "data")
            if name:
                return name
    return walk(data) or "Unknown Product"


def lookup_product_name(url: str, region: str = "US") -> str:
    data = sociavault_get(SOCIA_PRODUCT_DETAILS, {"url": url, "get_related_videos": "false", "region": region}, timeout=20)
    return product_title(data)


def tiktok_page_title(url: str) -> str:
    """Use a public TikTok page's own metadata when regional APIs omit the title."""
    class TitleParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self.in_title = False

        def handle_starttag(self, tag, attrs):
            if tag == "title":
                self.in_title = True
            if tag == "meta":
                values = dict(attrs)
                if values.get("property") == "og:title" or values.get("name") == "twitter:title":
                    self.title = values.get("content") or self.title

        def handle_endtag(self, tag):
            if tag == "title":
                self.in_title = False

        def handle_data(self, data):
            if self.in_title and not self.title:
                self.title = data

    current = str(url or "").strip()
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Accept": "text/html"}
    for _ in range(3):
        host = (urlsplit(current).hostname or "").lower()
        if urlsplit(current).scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return "Unknown Product"
        response = requests.get(current, headers=headers, timeout=(4, 9), allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            current = urljoin(current, response.headers.get("Location") or "")
            continue
        if response.status_code >= 400:
            return "Unknown Product"
        parser = TitleParser()
        parser.feed(response.text[:1_000_000])
        name = html.unescape(parser.title).strip()
        name = re.sub(r"\s*[|\-]\s*TikTok(?: Shop)?\s*$", "", name, flags=re.I).strip()
        return name if len(name) >= 8 and name.lower() not in {"tiktok shop", "tiktok - make your day", "shop on tiktok"} else "Unknown Product"
    return "Unknown Product"


def tiktok_page_product_image(url: str) -> tuple[str, str]:
    """Read a product's public TikTok metadata when regional gallery APIs are empty."""
    class ProductParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self.image = ""

        def handle_starttag(self, tag, attrs):
            if tag != "meta":
                return
            values = dict(attrs)
            key = values.get("property") or values.get("name")
            if key in {"og:title", "twitter:title"}:
                self.title = values.get("content") or self.title
            elif key in {"og:image", "twitter:image"}:
                self.image = values.get("content") or self.image

    current = str(url or "").strip()
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Accept": "text/html"}
    for _ in range(3):
        parsed = urlsplit(current)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return "Unknown Product", ""
        response = requests.get(current, headers=headers, timeout=(4, 9), allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            current = urljoin(current, response.headers.get("Location") or "")
            continue
        if response.status_code >= 400:
            return "Unknown Product", ""
        parser = ProductParser()
        parser.feed(response.text[:1_000_000])
        title = html.unescape(parser.title).strip()
        title = re.sub(r"\s*[|\-]\s*TikTok(?: Shop)?\s*$", "", title, flags=re.I).strip()
        image = normalize_remote_url(parser.image)
        if len(title) < 8 or title.lower() in {"tiktok shop", "tiktok - make your day", "shop on tiktok"}:
            return "Unknown Product", ""
        if not image or any(x in image.lower() for x in ("tiktok-logo", "tiktok_logo")):
            return title, ""
        return title, image
    return "Unknown Product", ""


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


def sociavault_get(endpoint: str, params: dict, *, timeout: int = 90) -> dict:
    cfg = settings()
    if not cfg.sociavault_api_key:
        raise RuntimeError("Missing SOCIAVAULT_API_KEY")
    resp = requests.get(endpoint, headers={"X-API-Key": cfg.sociavault_api_key, "Accept": "application/json"}, params=params, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"SociaVault HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("SociaVault returned no product data.")

    def fail_if_unsuccessful(node):
        if isinstance(node, dict) and node.get("success") is False:
            raise RuntimeError(str(node.get("message") or node.get("error") or "SociaVault request failed."))

    fail_if_unsuccessful(payload)
    data = payload.get("data", payload)
    # SociaVault sometimes wraps the scraper result in an additional data envelope.
    for _ in range(3):
        if not isinstance(data, dict):
            break
        fail_if_unsuccessful(data)
        nested = data.get("data")
        if isinstance(nested, dict) and not any(k in data for k in ("product_base", "product", "product_id", "product_detail_review")):
            data = nested
            continue
        break
    if not isinstance(data, dict):
        raise RuntimeError("SociaVault returned no product data.")
    return data


def classify_focus(name: str) -> str:
    """Classify the product into the production framing/motion bucket.

    V5 intentionally exposes this value in the UI so the user can override it
    before generation.
    """
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    tokens = set(text.split())

    shoes = {"shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "heel", "heels", "sandal", "sandals", "loafer", "loafers", "clog", "clogs", "slipper", "slippers", "slides"}
    bags = {"bag", "handbag", "purse", "tote", "crossbody", "backpack", "clutch", "satchel"}
    hoodies = {"hoodie", "hooded", "sweatshirt", "pullover", "jacket", "coat", "windbreaker", "parka"}
    tops = {"shirt", "tee", "sweater", "blouse", "top", "tank", "cardigan", "jersey", "polo"}
    bottoms = {"pants", "pant", "jeans", "jean", "shorts", "leggings", "legging", "jogger", "joggers", "trouser", "trousers", "skirt", "cargo"}
    outfit_phrases = ("two piece", "2 piece", "two-piece", "matching set", "tracksuit", "track suit", "jumpsuit", "romper")
    outfit_tokens = {"set", "outfit", "suit", "dress"}

    if tokens & shoes:
        return "shoes"
    if tokens & bags:
        return "handbag"
    # A hoodie + pants set should stay an outfit rather than being reduced to a hoodie.
    if any(p in text for p in outfit_phrases) or tokens & outfit_tokens:
        return "outfit"
    if tokens & hoodies or "zip hoodie" in text or "zip up hoodie" in text:
        return "hoodie"
    if tokens & tops or "t shirt" in text:
        return "shirt"
    if tokens & bottoms:
        return "pants"
    return "outfit"


def import_product(url: str, region: str | None = None) -> dict:
    cfg = settings()
    region_code = str(region or cfg.sociavault_region or "US").strip().upper()
    if region_code == "UK":
        region_code = "GB"
    if region_code not in {"US", "GB"}:
        raise RuntimeError("SociaVault market must be US or UK.")
    data = sociavault_get(SOCIA_PRODUCT_DETAILS, {"url": url, "get_related_videos": "false", "region": region_code})
    product = data.get("product_base") or data.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    name = product_title(data)
    if name == "Unknown Product":
        log.warning("SociaVault product details omitted title · keys=%s · product_keys=%s", sorted(data.keys())[:25], sorted(product.keys())[:25])
    product_id = str(data.get("product_id") or product.get("id") or hashlib.sha1(url.encode()).hexdigest()[:12])

    listing = []
    raw_images = product.get("images")
    for obj in sv_values(raw_images):
        u = normalize_remote_url(obj) if isinstance(obj, str) else sv_first_url(obj)
        if u:
            listing.append(u)
    if not listing:
        listing = sv_collect_urls(raw_images or product)[:18]
    # Last-resort parser for response-shape changes: scan the returned product payload,
    # while sv_collect_urls still filters avatars/seller/shop UI imagery.
    if not listing:
        listing = sv_collect_urls(data)[:18]
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
            review_data = sociavault_get(SOCIA_PRODUCT_REVIEWS, {"product_id": product_id, "page": 1, "region": region_code})
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
        "sociavault_region": region_code,
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
