"""
Shopify storefront scraper.

Most Shopify stores expose their public catalog at /products.json.
We page through it (250 products per page) until a page comes back empty,
then normalize every product and variant into plain dicts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

PAGE_SIZE = 250                      # Shopify's maximum for products.json
RETRY_STATUS = {429, 430, 500, 502, 503, 504}   # 430 = Shopify "security rejection" throttle


class ScraperError(Exception):
    """Generic scraping failure."""


class NotShopifyStore(ScraperError):
    """The URL does not expose a Shopify products.json feed."""


@dataclass
class StoreData:
    domain: str
    base_url: str
    products: list[dict]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pages: int = 0

    @property
    def n_variants(self) -> int:
        return sum(len(p["variants"]) for p in self.products)


# ---------------------------------------------------------------- URL helpers
def normalize_store_url(url: str) -> str:
    """'example.com/collections/x' -> 'https://example.com'."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty store URL")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path).lower().split("@")[-1].strip("/")
    if not host or "." not in host:
        raise ValueError(f"Invalid store URL: {url!r}")
    return f"https://{host}"


def domain_of(url: str) -> str:
    host = urlparse(normalize_store_url(url)).netloc
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- normalization
def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def normalize_product(raw: dict, base_url: str = "") -> dict:
    images = raw.get("images") or []
    variants = []
    for v in raw.get("variants") or []:
        compare_at = _to_float(v.get("compare_at_price"))
        price = _to_float(v.get("price"))
        if compare_at is not None and price is not None and compare_at <= price:
            compare_at = None            # Shopify often stores compare_at == price; not a sale
        variants.append({
            "id": int(v["id"]),
            "title": v.get("title") or "Default Title",
            "sku": v.get("sku") or None,
            "price": price,
            "compare_at_price": compare_at,
            "available": bool(v.get("available", True)),
            "option1": v.get("option1"),
            "option2": v.get("option2"),
            "option3": v.get("option3"),
        })
    handle = raw.get("handle") or ""
    return {
        "id": int(raw["id"]),
        "title": (raw.get("title") or "").strip(),
        "handle": handle,
        "vendor": raw.get("vendor") or None,
        "product_type": raw.get("product_type") or None,
        "tags": _tags(raw.get("tags")),
        "created_at": raw.get("created_at"),
        "published_at": raw.get("published_at"),
        "updated_at": raw.get("updated_at"),
        "image_url": images[0].get("src") if images else None,
        "url": f"{base_url}/products/{handle}" if base_url and handle else None,
        "variants": variants,
    }


# ---------------------------------------------------------------- scraper
class ShopifyScraper:
    def __init__(
        self,
        session: requests.Session | None = None,
        delay: float = config.REQUEST_DELAY,
        timeout: float = config.REQUEST_TIMEOUT,
        max_retries: int = config.MAX_RETRIES,
        sleep=time.sleep,
    ):
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update({
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json",
            })
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    def _get_json(self, url: str, params: dict) -> dict:
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = e
                log.warning("Request error on %s (attempt %d): %s", url, attempt, e)
                self._sleep(2 ** attempt)
                continue

            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError as e:
                    raise NotShopifyStore(f"{url} did not return JSON") from e
                if not isinstance(data, dict) or "products" not in data:
                    raise NotShopifyStore(f"{url} has no 'products' key")
                return data

            if r.status_code in (401, 403, 404):
                raise NotShopifyStore(
                    f"{url} returned HTTP {r.status_code} (not Shopify, password-protected, "
                    f"or the JSON feed is disabled)")

            if r.status_code in RETRY_STATUS:
                wait = _retry_after(r) or 2 ** attempt
                log.warning("HTTP %s on %s, retrying in %ss", r.status_code, url, wait)
                last_error = ScraperError(f"HTTP {r.status_code}")
                self._sleep(wait)
                continue

            raise ScraperError(f"Unexpected HTTP {r.status_code} on {url}")

        raise ScraperError(f"Gave up on {url} after {self.max_retries} attempts: {last_error}")

    def iter_raw_products(self, store_url: str, max_pages: int | None = None):
        """Yield (page_number, raw_products) until an empty page or max_pages."""
        base = normalize_store_url(store_url)
        page = 1
        while max_pages is None or page <= max_pages:
            data = self._get_json(f"{base}/products.json", {"limit": PAGE_SIZE, "page": page})
            products = data.get("products") or []
            if not products:
                break
            yield page, products
            if len(products) < PAGE_SIZE:
                break                       # last page, save one request
            page += 1
            self._sleep(self.delay)

    def fetch_store(self, store_url: str, max_pages: int | None = None) -> StoreData:
        base = normalize_store_url(store_url)
        products, seen, pages = [], set(), 0
        for page, raw_products in self.iter_raw_products(base, max_pages):
            pages = page
            for raw in raw_products:
                try:
                    p = normalize_product(raw, base)
                except (KeyError, TypeError, ValueError) as e:
                    log.warning("Skipping malformed product on page %d: %s", page, e)
                    continue
                if p["id"] not in seen:     # pagination can repeat items
                    seen.add(p["id"])
                    products.append(p)
            log.info("%s page %d: %d products (total %d)", base, page, len(raw_products), len(products))
        return StoreData(domain=domain_of(base), base_url=base, products=products, pages=pages)


def _retry_after(response) -> float | None:
    value = getattr(response, "headers", {}).get("Retry-After")
    try:
        return min(float(value), 120) if value else None
    except ValueError:
        return None
