"""
Command line interface.

    python -m spm.cli init-db
    python -m spm.cli add gymshark.com allbirds.com
    python -m spm.cli scrape --all              # what the daily cron runs
    python -m spm.cli scrape allbirds.com --max-pages 2
    python -m spm.cli events --store allbirds.com --days 7
    python -m spm.cli export allbirds.com -o allbirds.csv    # one-off, no database needed
    python -m spm.cli stores
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import analysis
from .scraper import NotShopifyStore, ScraperError, ShopifyScraper, domain_of


def _db():
    from .db import get_engine, get_sessionmaker, init_db   # lazy: export works without SQLAlchemy
    engine = get_engine()
    init_db(engine)
    return get_sessionmaker(engine)()


def cmd_init_db(args):
    _db().close()
    print("Database ready.")


def cmd_add(args):
    from .pipeline import get_or_create_store
    with _db() as db:
        for url in args.urls:
            store = get_or_create_store(db, url, tracked=True)
            print(f"Tracking {store.domain}")
        db.commit()


def cmd_stores(args):
    from .queries import list_stores
    with _db() as db:
        df = list_stores(db, tracked_only=False)
    print(df.to_string(index=False) if not df.empty else "No stores yet. Use: add <url>")


def cmd_scrape(args):
    from sqlalchemy import select

    from .db import Store
    from .pipeline import scrape_and_save

    scraper = ShopifyScraper(delay=args.delay)
    with _db() as db:
        if args.all:
            urls = [s.base_url for s in db.scalars(select(Store).where(Store.is_tracked.is_(True)))]
        else:
            urls = args.urls
        if not urls:
            sys.exit("Nothing to scrape. Pass URLs or add stores first.")
        failed = 0
        for url in urls:
            try:
                run = scrape_and_save(db, url, scraper, max_pages=args.max_pages)
                print(f"OK   {domain_of(url):35} {run.n_products:6} products "
                      f"{run.n_events:5} events")
            except (NotShopifyStore, ScraperError) as e:
                failed += 1
                print(f"FAIL {domain_of(url):35} {e}")
    sys.exit(1 if failed and failed == len(urls) else 0)


def cmd_events(args):
    from sqlalchemy import select

    from .db import Store
    from .queries import events_df
    with _db() as db:
        store_id = None
        if args.store:
            store = db.scalar(select(Store).where(Store.domain == domain_of(args.store)))
            if store is None:
                sys.exit(f"Unknown store {args.store}")
            store_id = store.id
        df = events_df(db, store_id=store_id, types=args.type, since_days=args.days,
                       limit=args.limit)
    if df.empty:
        print("No events yet (events appear from the second scrape of a store).")
        return
    for _, e in df.iterrows():
        print(f"{e.created_at:%Y-%m-%d %H:%M}  {e.store:25} {e.type:15} {e.message}")


def cmd_export(args):
    data = ShopifyScraper(delay=args.delay).fetch_store(args.url, max_pages=args.max_pages)
    df = analysis.products_to_frame(data.products)
    out = args.output or f"{data.domain.replace('.', '_')}.csv"
    df.to_csv(out, index=False)
    summary = analysis.catalog_summary(df)
    print(f"Saved {len(df)} variants of {summary.get('products', 0)} products to {out}")
    for k, v in summary.items():
        print(f"  {k:18} {v}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spm", description="Shopify competitor price monitor")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables").set_defaults(func=cmd_init_db)

    s = sub.add_parser("add", help="track one or more stores")
    s.add_argument("urls", nargs="+")
    s.set_defaults(func=cmd_add)

    sub.add_parser("stores", help="list stores").set_defaults(func=cmd_stores)

    s = sub.add_parser("scrape", help="scrape stores and record changes")
    s.add_argument("urls", nargs="*")
    s.add_argument("--all", action="store_true", help="scrape every tracked store")
    s.add_argument("--max-pages", type=int, default=None, help="limit pages (250 products each)")
    s.add_argument("--delay", type=float, default=1.0, help="seconds between pages")
    s.set_defaults(func=cmd_scrape)

    s = sub.add_parser("events", help="show recent changes")
    s.add_argument("--store")
    s.add_argument("--type", action="append", help="filter by type (repeatable)")
    s.add_argument("--days", type=int, default=None)
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("export", help="one-off catalog export to CSV (no database)")
    s.add_argument("url")
    s.add_argument("-o", "--output")
    s.add_argument("--max-pages", type=int, default=None)
    s.add_argument("--delay", type=float, default=1.0)
    s.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
