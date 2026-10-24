"""
Scrape -> store -> diff pipeline.

    store_data = scraper.fetch_store(url)
    save_scrape(session, store_data)   # upserts catalog, writes snapshots + events
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import Event, Product, ScrapeRun, Snapshot, Store, Variant, utcnow
from .diff import VariantState, detect_sitewide_sale, diff_states, states_from_products
from .scraper import ShopifyScraper, StoreData, domain_of, normalize_store_url

log = logging.getLogger(__name__)


def get_or_create_store(db: Session, url: str, tracked: bool = True) -> Store:
    domain = domain_of(url)
    store = db.scalar(select(Store).where(Store.domain == domain))
    if store is None:
        store = Store(domain=domain, base_url=normalize_store_url(url), is_tracked=tracked)
        db.add(store)
        db.flush()
    elif tracked and not store.is_tracked:
        store.is_tracked = True
    return store


def load_last_state(db: Session, store: Store) -> dict:
    """State of the last successful run, keyed like diff.states_from_products."""
    last_run_id = db.scalar(
        select(ScrapeRun.id)
        .where(ScrapeRun.store_id == store.id, ScrapeRun.status == "success")
        .order_by(ScrapeRun.id.desc()).limit(1))
    if last_run_id is None:
        return {}
    rows = db.execute(
        select(Product.shopify_id, Product.title, Variant.shopify_id, Variant.title,
               Snapshot.price, Snapshot.compare_at_price, Snapshot.available)
        .join(Variant, Snapshot.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .where(Snapshot.run_id == last_run_id))
    return {
        (pid, vid): VariantState(pid, vid, ptitle, vtitle, price, cap, bool(avail))
        for pid, ptitle, vid, vtitle, price, cap, avail in rows
    }


def save_scrape(db: Session, data: StoreData) -> ScrapeRun:
    """Upsert the catalog, write one snapshot per variant, compute and store events."""
    store = get_or_create_store(db, data.base_url)
    previous = load_last_state(db, store)          # read BEFORE writing the new run

    run = ScrapeRun(store_id=store.id, started_at=data.fetched_at)
    db.add(run)
    db.flush()

    # --- upsert products and variants
    products = {p.shopify_id: p for p in db.scalars(
        select(Product).where(Product.store_id == store.id))}
    variants = {(v.product_id, v.shopify_id): v for v in db.scalars(
        select(Variant).join(Product).where(Product.store_id == store.id))}

    now = utcnow()
    seen_products: set[int] = set()
    variant_rows: list[tuple[Variant, dict]] = []

    for p in data.products:
        row = products.get(p["id"])
        if row is None:
            row = Product(store_id=store.id, shopify_id=p["id"], first_seen=now,
                          title=p["title"], handle=p["handle"])
            db.add(row)
            products[p["id"]] = row
        row.title, row.handle = p["title"], p["handle"]
        row.vendor, row.product_type = p["vendor"], p["product_type"]
        row.tags = ", ".join(p["tags"])
        row.url, row.image_url, row.published_at = p["url"], p["image_url"], p["published_at"]
        row.last_seen, row.is_active = now, True
        seen_products.add(p["id"])
    db.flush()                                     # one flush: new products get their ids

    for p in data.products:
        row = products[p["id"]]
        for v in p["variants"]:
            vrow = variants.get((row.id, v["id"]))
            if vrow is None:
                vrow = Variant(product_id=row.id, shopify_id=v["id"], title=v["title"])
                db.add(vrow)
                variants[(row.id, v["id"])] = vrow
            vrow.title, vrow.sku = v["title"], v["sku"]
            variant_rows.append((vrow, v))

    for shopify_id, row in products.items():
        if shopify_id not in seen_products:
            row.is_active = False
    db.flush()

    # --- snapshots
    db.add_all(Snapshot(run_id=run.id, variant_id=vrow.id, price=v["price"],
                        compare_at_price=v["compare_at_price"], available=v["available"])
               for vrow, v in variant_rows)

    # --- events
    current = states_from_products(data.products)
    events = diff_states(previous, current, config.MIN_PRICE_CHANGE_PCT)
    sitewide = detect_sitewide_sale(events, len(data.products), config.SITEWIDE_SALE_THRESHOLD)
    if sitewide:
        events.insert(0, sitewide)

    variant_ids = {key: vrow.id for key, vrow in variants.items()}   # (product row id, shopify id)

    for e in events:
        prod_row = products.get(e["product_id"]) if e["product_id"] else None
        var_id = None
        if prod_row is not None and e["variant_id"]:
            var_id = variant_ids.get((prod_row.id, e["variant_id"]))
        db.add(Event(store_id=store.id, run_id=run.id, created_at=now,
                     event_type=e["type"], product_id=prod_row.id if prod_row else None,
                     variant_id=var_id, old_value=e["old_value"], new_value=e["new_value"],
                     pct_change=e["pct_change"], message=e["message"]))

    run.status = "success"
    run.finished_at = utcnow()
    run.n_products, run.n_variants, run.n_events = len(data.products), data.n_variants, len(events)
    store.last_scraped_at = run.finished_at
    db.commit()
    log.info("%s: %d products, %d variants, %d events", store.domain, run.n_products,
             run.n_variants, run.n_events)
    return run


def scrape_and_save(db: Session, url: str, scraper: ShopifyScraper | None = None,
                    max_pages: int | None = None) -> ScrapeRun:
    """Fetch a store and save it. A failure is recorded as a failed run and re-raised."""
    scraper = scraper or ShopifyScraper()
    try:
        data = scraper.fetch_store(url, max_pages=max_pages)
        return save_scrape(db, data)
    except Exception as exc:
        db.rollback()
        store = get_or_create_store(db, url)
        db.add(ScrapeRun(store_id=store.id, status="failed", finished_at=utcnow(),
                         error=str(exc)[:2000]))
        db.commit()
        raise
