"""
Diff engine: compare two catalog snapshots and produce change events.

Pure functions only (no database, no network) so they are easy to test
and reuse from the pipeline, the CLI and the demo app.

Event types
-----------
new_product, product_removed            - catalog changes
price_drop, price_increase              - per variant
sale_started, sale_ended                - per variant (compare_at_price appears / disappears)
out_of_stock, back_in_stock             - per product (any variant available?)
sitewide_sale                           - store level (many products dropped at once)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

NEW_PRODUCT = "new_product"
PRODUCT_REMOVED = "product_removed"
PRICE_DROP = "price_drop"
PRICE_INCREASE = "price_increase"
SALE_STARTED = "sale_started"
SALE_ENDED = "sale_ended"
OUT_OF_STOCK = "out_of_stock"
BACK_IN_STOCK = "back_in_stock"
SITEWIDE_SALE = "sitewide_sale"

EVENT_TYPES = [NEW_PRODUCT, PRODUCT_REMOVED, PRICE_DROP, PRICE_INCREASE, SALE_STARTED,
               SALE_ENDED, OUT_OF_STOCK, BACK_IN_STOCK, SITEWIDE_SALE]


@dataclass(frozen=True)
class VariantState:
    product_id: int
    variant_id: int
    product_title: str
    variant_title: str
    price: float | None
    compare_at_price: float | None
    available: bool

    @property
    def on_sale(self) -> bool:
        return (self.compare_at_price is not None and self.price is not None
                and self.compare_at_price > self.price)

    @property
    def label(self) -> str:
        if self.variant_title and self.variant_title != "Default Title":
            return f"{self.product_title} ({self.variant_title})"
        return self.product_title


State = dict[tuple[int, int], VariantState]


def states_from_products(products: Iterable[dict]) -> State:
    """Normalized products (from scraper.normalize_product) -> {(product_id, variant_id): state}."""
    out: State = {}
    for p in products:
        for v in p["variants"]:
            out[(p["id"], v["id"])] = VariantState(
                product_id=p["id"], variant_id=v["id"],
                product_title=p["title"], variant_title=v.get("title") or "Default Title",
                price=v.get("price"), compare_at_price=v.get("compare_at_price"),
                available=bool(v.get("available")),
            )
    return out


def pct_change(old: float | None, new: float | None) -> float | None:
    if old in (None, 0) or new is None:
        return None
    return round((new - old) / old * 100, 2)


def _event(event_type, state: VariantState, *, variant_level=True, old=None, new=None,
           pct=None, message=""):
    return {
        "type": event_type,
        "product_id": state.product_id,
        "variant_id": state.variant_id if variant_level else None,
        "product_title": state.product_title,
        "variant_title": state.variant_title if variant_level else None,
        "old_value": old,
        "new_value": new,
        "pct_change": pct,
        "message": message,
    }


def _by_product(state: State) -> dict[int, list[VariantState]]:
    grouped: dict[int, list[VariantState]] = {}
    for s in state.values():
        grouped.setdefault(s.product_id, []).append(s)
    return grouped


def diff_states(prev: State, curr: State, min_price_change_pct: float = 0.0) -> list[dict]:
    """Return change events between two snapshots. The first snapshot (prev empty) is a baseline."""
    if not prev:
        return []

    events: list[dict] = []
    prev_products, curr_products = _by_product(prev), _by_product(curr)

    # --- catalog changes
    for pid in curr_products.keys() - prev_products.keys():
        first = curr_products[pid][0]
        prices = [s.price for s in curr_products[pid] if s.price is not None]
        events.append(_event(NEW_PRODUCT, first, variant_level=False,
                             new=min(prices) if prices else None,
                             message=f"New product: {first.product_title}"))
    for pid in prev_products.keys() - curr_products.keys():
        first = prev_products[pid][0]
        events.append(_event(PRODUCT_REMOVED, first, variant_level=False,
                             message=f"Removed: {first.product_title}"))

    # --- variant price & sale changes
    for key in curr.keys() & prev.keys():
        old, new = prev[key], curr[key]
        if old.price is not None and new.price is not None and old.price != new.price:
            pct = pct_change(old.price, new.price)
            if pct is None or abs(pct) >= min_price_change_pct:
                kind = PRICE_DROP if new.price < old.price else PRICE_INCREASE
                verb = "dropped" if kind == PRICE_DROP else "increased"
                pct_txt = f" ({pct:+.1f}%)" if pct is not None else ""
                events.append(_event(kind, new, old=old.price, new=new.price, pct=pct,
                                     message=f"{new.label}: price {verb} "
                                             f"{old.price:.2f} -> {new.price:.2f}{pct_txt}"))
        if new.on_sale and not old.on_sale:
            disc = -(pct_change(new.compare_at_price, new.price) or 0.0)
            events.append(_event(SALE_STARTED, new, old=new.compare_at_price, new=new.price,
                                 pct=-disc, message=f"{new.label}: on sale, {disc:.0f}% off "
                                                    f"(was {new.compare_at_price:.2f})"))
        elif old.on_sale and not new.on_sale:
            events.append(_event(SALE_ENDED, new, old=old.price, new=new.price,
                                 message=f"{new.label}: sale ended"))

    # --- product-level stock changes
    for pid in curr_products.keys() & prev_products.keys():
        was = any(s.available for s in prev_products[pid])
        now = any(s.available for s in curr_products[pid])
        first = curr_products[pid][0]
        if was and not now:
            events.append(_event(OUT_OF_STOCK, first, variant_level=False,
                                 message=f"Sold out: {first.product_title}"))
        elif now and not was:
            events.append(_event(BACK_IN_STOCK, first, variant_level=False,
                                 message=f"Back in stock: {first.product_title}"))

    return events


def detect_sitewide_sale(events: list[dict], n_products: int,
                         threshold: float = 0.25, min_products: int = 5) -> dict | None:
    """If a large share of the catalog dropped price or went on sale in one run, flag a sitewide sale."""
    if n_products <= 0:
        return None
    hit = {e["product_id"] for e in events if e["type"] in (PRICE_DROP, SALE_STARTED)}
    share = len(hit) / n_products
    if len(hit) >= min_products and share >= threshold:
        drops = [abs(e["pct_change"]) for e in events
                 if e["type"] in (PRICE_DROP, SALE_STARTED) and e["pct_change"] is not None]
        avg = round(sum(drops) / len(drops), 1) if drops else None
        avg_txt = f", average discount {avg:.0f}%" if avg is not None else ""
        return {
            "type": SITEWIDE_SALE, "product_id": None, "variant_id": None,
            "product_title": None, "variant_title": None,
            "old_value": None, "new_value": round(share * 100, 1), "pct_change": avg,
            "message": f"Sitewide sale detected: {len(hit)} products ({share:.0%} of catalog) "
                       f"discounted{avg_txt}",
        }
    return None
