"""Read-side queries that return pandas DataFrames for the demo app and the CLI."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Event, Product, ScrapeRun, Snapshot, Store, Variant, utcnow


def _frame(rows, columns) -> pd.DataFrame:
    return pd.DataFrame([tuple(r) for r in rows], columns=columns)


def list_stores(db: Session, tracked_only: bool = True) -> pd.DataFrame:
    active = (select(Product.store_id, func.count(Product.id).label("n"))
              .where(Product.is_active.is_(True)).group_by(Product.store_id).subquery())
    runs = (select(ScrapeRun.store_id, func.count(ScrapeRun.id).label("runs"))
            .where(ScrapeRun.status == "success").group_by(ScrapeRun.store_id).subquery())
    q = (select(Store.id, Store.domain, Store.base_url, Store.last_scraped_at,
                func.coalesce(active.c.n, 0), func.coalesce(runs.c.runs, 0))
         .outerjoin(active, active.c.store_id == Store.id)
         .outerjoin(runs, runs.c.store_id == Store.id)
         .order_by(Store.domain))
    if tracked_only:
        q = q.where(Store.is_tracked.is_(True))
    return _frame(db.execute(q), ["id", "domain", "base_url", "last_scraped_at",
                                  "active_products", "runs"])


def events_df(db: Session, store_id: int | None = None, types: list[str] | None = None,
              since_days: int | None = None, limit: int = 1000) -> pd.DataFrame:
    q = (select(Event.created_at, Store.domain, Event.event_type, Event.message,
                Event.old_value, Event.new_value, Event.pct_change, Product.id,
                Product.title, Product.url)
         .join(Store, Event.store_id == Store.id)
         .outerjoin(Product, Event.product_id == Product.id)
         .order_by(Event.created_at.desc(), Event.id.desc()).limit(limit))
    if store_id is not None:
        q = q.where(Event.store_id == store_id)
    if types:
        q = q.where(Event.event_type.in_(types))
    if since_days:
        q = q.where(Event.created_at >= utcnow() - timedelta(days=since_days))
    return _frame(db.execute(q), ["created_at", "store", "type", "message", "old_value",
                                  "new_value", "pct_change", "product_id", "product_title",
                                  "url"])


def store_products(db: Session, store_id: int, active_only: bool = True) -> pd.DataFrame:
    q = (select(Product.id, Product.title, Product.product_type, Product.vendor, Product.url,
                Product.first_seen, Product.is_active)
         .where(Product.store_id == store_id).order_by(Product.title))
    if active_only:
        q = q.where(Product.is_active.is_(True))
    return _frame(db.execute(q), ["id", "title", "product_type", "vendor", "url",
                                  "first_seen", "is_active"])


def price_history(db: Session, product_id: int) -> pd.DataFrame:
    """Price per variant over every successful run (product_id = our DB id)."""
    q = (select(ScrapeRun.started_at, Variant.title, Snapshot.price,
                Snapshot.compare_at_price, Snapshot.available)
         .join(Variant, Snapshot.variant_id == Variant.id)
         .join(ScrapeRun, Snapshot.run_id == ScrapeRun.id)
         .where(Variant.product_id == product_id, ScrapeRun.status == "success")
         .order_by(ScrapeRun.started_at))
    return _frame(db.execute(q), ["scraped_at", "variant", "price", "compare_at_price",
                                  "available"])


def daily_event_counts(db: Session, store_id: int, days: int = 90) -> pd.DataFrame:
    """Events per day and type - drives the 'sale calendar' heatmap."""
    df = events_df(db, store_id=store_id, since_days=days, limit=100_000)
    if df.empty:
        return pd.DataFrame(columns=["date", "type", "count"])
    df["date"] = pd.to_datetime(df["created_at"], utc=True).dt.date
    return df.groupby(["date", "type"]).size().reset_index(name="count")


def latest_run(db: Session, store_id: int) -> ScrapeRun | None:
    return db.scalar(select(ScrapeRun).where(ScrapeRun.store_id == store_id)
                     .order_by(ScrapeRun.id.desc()).limit(1))
