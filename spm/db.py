"""
Database models (SQLAlchemy 2.0). Works with SQLite locally and PostgreSQL in production.

stores ──< products ──< variants ──< snapshots >── scrape_runs
   └──────────────< events >─────────────────────────┘
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, create_engine, event)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(300))
    name: Mapped[str | None] = mapped_column(String(255))
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    products: Mapped[list["Product"]] = relationship(back_populates="store",
                                                     cascade="all, delete-orphan")
    runs: Mapped[list["ScrapeRun"]] = relationship(back_populates="store",
                                                   cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("store_id", "shopify_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    shopify_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    handle: Mapped[str] = mapped_column(String(500))
    vendor: Mapped[str | None] = mapped_column(String(255))
    product_type: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(800))
    image_url: Mapped[str | None] = mapped_column(String(800))
    published_at: Mapped[str | None] = mapped_column(String(40))   # ISO string from Shopify
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    store: Mapped[Store] = relationship(back_populates="products")
    variants: Mapped[list["Variant"]] = relationship(back_populates="product",
                                                     cascade="all, delete-orphan")


class Variant(Base):
    __tablename__ = "variants"
    __table_args__ = (UniqueConstraint("product_id", "shopify_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True)
    shopify_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    sku: Mapped[str | None] = mapped_column(String(255))

    product: Mapped[Product] = relationship(back_populates="variants")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")   # running|success|failed
    n_products: Mapped[int] = mapped_column(Integer, default=0)
    n_variants: Mapped[int] = mapped_column(Integer, default=0)
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    store: Mapped[Store] = relationship(back_populates="runs")


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_variant_run", "variant_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("scrape_runs.id", ondelete="CASCADE"),
                                        index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"))
    price: Mapped[float | None] = mapped_column(Float)
    compare_at_price: Mapped[float | None] = mapped_column(Float)
    available: Mapped[bool] = mapped_column(Boolean)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_store_created", "store_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    run_id: Mapped[int] = mapped_column(ForeignKey("scrape_runs.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"))
    old_value: Mapped[float | None] = mapped_column(Float)
    new_value: Mapped[float | None] = mapped_column(Float)
    pct_change: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- engine helpers
def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or config.DATABASE_URL
    if url.startswith("postgres://"):                 # Heroku/Render style URLs
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=echo, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):                    # enforce foreign keys in SQLite
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def get_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
