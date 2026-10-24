"""
Public demo.

    streamlit run app/streamlit_app.py

Tab 1 - Analyze any store: paste a Shopify URL, get an instant catalog analysis (no signup).
Tab 2 - Tracked stores: price history, change feed and sale calendar from the database.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # make `spm` importable

from spm import analysis, config                                 # noqa: E402
from spm.diff import EVENT_TYPES                                  # noqa: E402
from spm.scraper import NotShopifyStore, ScraperError, ShopifyScraper, normalize_store_url  # noqa: E402

st.set_page_config(page_title="Shopify Price Monitor", page_icon="📈", layout="wide")

EVENT_LABELS = {
    "new_product": "🆕 New product", "product_removed": "🗑️ Removed",
    "price_drop": "🔻 Price drop", "price_increase": "🔺 Price increase",
    "sale_started": "🏷️ Sale started", "sale_ended": "⏹️ Sale ended",
    "out_of_stock": "⛔ Sold out", "back_in_stock": "✅ Back in stock",
    "sitewide_sale": "🔥 Sitewide sale",
}


# ---------------------------------------------------------------- data access
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_catalog(url: str, max_pages: int) -> tuple[str, list[dict], int]:
    data = ShopifyScraper(delay=0.5).fetch_store(url, max_pages=max_pages)
    return data.domain, data.products, data.pages


@st.cache_resource
def get_db_sessionmaker():
    from spm.db import get_engine, get_sessionmaker, init_db
    engine = get_engine()
    init_db(engine)
    return get_sessionmaker(engine)


def fmt_money(x) -> str:
    return "–" if x is None or pd.isna(x) else f"{x:,.2f}"


# ---------------------------------------------------------------- tab 1: instant analysis
def render_instant_analysis():
    st.subheader("Analyze any Shopify store")
    st.caption("Paste a competitor's store URL. We read its public product feed and analyze "
               f"up to {config.DEMO_MAX_PAGES * 250:,} products.")

    with st.form("analyze"):
        c1, c2 = st.columns([4, 1])
        url = c1.text_input("Store URL", placeholder="e.g. allbirds.com",
                            label_visibility="collapsed")
        submitted = c2.form_submit_button("Analyze", use_container_width=True, type="primary")

    if submitted and url:
        used = st.session_state.get("analyses", 0)
        if used >= config.DEMO_MAX_ANALYSES_PER_SESSION:
            st.warning("Demo limit reached for this session. Sign up for unlimited tracking.")
            return
        try:
            normalized = normalize_store_url(url)
        except ValueError as e:
            st.error(str(e))
            return
        with st.spinner(f"Reading {normalized} …"):
            try:
                domain, products, pages = fetch_catalog(normalized, config.DEMO_MAX_PAGES)
            except NotShopifyStore:
                st.error("This doesn't look like a Shopify store, or its product feed is "
                         "disabled.")
                return
            except ScraperError as e:
                st.error(f"Could not read the store right now ({e}). Try again in a minute.")
                return
        st.session_state["analyses"] = used + 1
        st.session_state["current"] = (normalized, domain, products, pages)

    if "current" not in st.session_state:
        st.info("Try a well-known Shopify store such as **allbirds.com**, **gymshark.com** "
                "or **colourpop.com**.")
        return

    base_url, domain, products, pages = st.session_state["current"]
    if not products:
        st.warning("The store has no public products.")
        return
    render_catalog_report(base_url, domain, products, pages)


def render_catalog_report(base_url, domain, products, pages):
    df = analysis.products_to_frame(products)
    s = analysis.catalog_summary(df)
    truncated = pages >= config.DEMO_MAX_PAGES

    st.markdown(f"### {domain}")
    if truncated:
        st.caption(f"Demo shows the first {s['products']:,} products.")

    k = st.columns(6)
    k[0].metric("Products", f"{s['products']:,}", f"{s['variants']:,} variants",
                delta_color="off")
    k[1].metric("Median price", fmt_money(s["median_price"]))
    k[2].metric("On sale", f"{s['on_sale_pct']:.0f}%")
    k[3].metric("Avg discount", f"{s['avg_discount_pct']:.0f}%")
    k[4].metric("Sold out", f"{s['out_of_stock_pct']:.0f}%")
    k[5].metric("New (30 days)", s["new_last_30d"])

    products_df = analysis.product_level(df)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Price distribution**")
        fig = px.histogram(products_df, x="min_price", nbins=40,
                           labels={"min_price": "Price"})
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="Products")
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.markdown("**Discount depth (products on sale)**")
        sale = products_df[products_df["on_sale"]]
        if sale.empty:
            st.caption("No products on sale right now.")
        else:
            fig = px.histogram(sale, x="max_discount_pct", nbins=20,
                               labels={"max_discount_pct": "Discount %"})
            fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="Products")
            st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Categories**")
        types = analysis.type_breakdown(df)
        fig = px.bar(types, x="products", y="product_type", orientation="h",
                     hover_data=["median_price", "on_sale_pct", "out_of_stock_pct"],
                     labels={"product_type": "", "products": "Products"})
        fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.markdown("**Launch velocity (products published per month)**")
        tl = analysis.launch_timeline(df)
        tl = tl[tl["period"] >= tl["period"].max() - pd.DateOffset(months=24)] if not tl.empty else tl
        fig = px.bar(tl, x="period", y="launches", labels={"period": "", "launches": "Launches"})
        fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("**Price endings**")
        st.dataframe(analysis.price_endings(df), hide_index=True, use_container_width=True)
        st.markdown("**Top vendors**")
        st.dataframe(analysis.vendor_breakdown(df, top=8), hide_index=True,
                     use_container_width=True)
    with c2:
        tab_sale, tab_new = st.tabs(["Biggest discounts", "Newest products"])
        link = st.column_config.LinkColumn("Link", display_text="open")
        with tab_sale:
            st.dataframe(analysis.discounted_products(df), hide_index=True,
                         use_container_width=True, column_config={"url": link})
        with tab_new:
            st.dataframe(analysis.newest_products(df), hide_index=True,
                         use_container_width=True, column_config={"url": link})

    c1, c2 = st.columns(2)
    c1.download_button("Download full catalog (CSV)", df.to_csv(index=False).encode(),
                       file_name=f"{domain}_catalog.csv", mime="text/csv",
                       use_container_width=True)
    if config.DEMO_ALLOW_TRACKING:
        if c2.button("📈 Track this store daily", use_container_width=True):
            from spm.pipeline import get_or_create_store
            with get_db_sessionmaker()() as db:
                get_or_create_store(db, base_url, tracked=True)
                db.commit()
            st.success("Added. It will be scraped on the next scheduled run.")
    else:
        c2.button("📈 Track this store daily (coming soon)", disabled=True,
                  use_container_width=True)


# ---------------------------------------------------------------- tab 2: tracked stores
def render_tracked_stores():
    from spm import queries
    try:
        Session = get_db_sessionmaker()
    except Exception as e:                       # database not configured
        st.error(f"Database not available: {e}")
        return

    with Session() as db:
        stores = queries.list_stores(db)
    if stores.empty:
        st.info("No tracked stores yet. Run `python -m spm.cli add <url>` then "
                "`python -m spm.cli scrape --all` (history appears from the second scrape).")
        return

    c1, c2, c3 = st.columns([2, 2, 1])
    domain = c1.selectbox("Store", stores["domain"].tolist())
    store = stores[stores["domain"] == domain].iloc[0]
    types = c2.multiselect("Event types", EVENT_TYPES, format_func=EVENT_LABELS.get,
                           default=[t for t in EVENT_TYPES if t != "product_removed"])
    days = c3.selectbox("Period", [7, 30, 90, 365], index=1, format_func=lambda d: f"{d} days")

    store_id = int(store["id"])
    with Session() as db:
        events = queries.events_df(db, store_id=store_id, types=types or None, since_days=days)
        calendar = queries.daily_event_counts(db, store_id, days=days)
        products = queries.store_products(db, store_id)

    k = st.columns(4)
    k[0].metric("Active products", int(store["active_products"]))
    k[1].metric("Scrapes", int(store["runs"]))
    last = store["last_scraped_at"]
    k[2].metric("Last scrape", "–" if pd.isna(last) else pd.Timestamp(last).strftime("%b %d %H:%M"))
    k[3].metric(f"Events ({days}d)", len(events))

    st.markdown("**Activity calendar**")
    if calendar.empty:
        st.caption("No changes recorded in this period yet.")
    else:
        calendar["type"] = calendar["type"].map(EVENT_LABELS).fillna(calendar["type"])
        fig = px.bar(calendar, x="date", y="count", color="type",
                     labels={"date": "", "count": "Events", "type": ""})
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0), legend_orientation="h")
        st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**Change feed**")
        if events.empty:
            st.caption("No events match these filters.")
        else:
            feed = events[["created_at", "type", "message", "url"]].copy()
            feed["type"] = feed["type"].map(EVENT_LABELS).fillna(feed["type"])
            st.dataframe(feed, hide_index=True, use_container_width=True, height=420,
                         column_config={
                             "created_at": st.column_config.DatetimeColumn(
                                 "When", format="MMM D, HH:mm"),
                             "url": st.column_config.LinkColumn("Link", display_text="open")})
    with c2:
        st.markdown("**Price history**")
        if products.empty:
            st.caption("No products.")
        else:
            options = dict(zip(products["title"], products["id"]))
            title = st.selectbox("Product", list(options), label_visibility="collapsed")
            with Session() as db:
                hist = queries.price_history(db, int(options[title]))
            if hist.empty:
                st.caption("No history yet.")
            else:
                fig = px.line(hist, x="scraped_at", y="price", color="variant", markers=True,
                              line_shape="hv", labels={"scraped_at": "", "price": "Price"})
                fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                                  showlegend=hist["variant"].nunique() <= 8)
                st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------- layout
st.title("📈 Shopify Price Monitor")
st.caption("Track competitors' prices, discounts, launches and stock - automatically.")

tab1, tab2 = st.tabs(["🔍 Analyze any store", "📊 Tracked stores"])
with tab1:
    render_instant_analysis()
with tab2:
    render_tracked_stores()

st.divider()
st.caption("Uses only public storefront data (products.json). Be polite: data is cached for "
           "1 hour and requests are rate-limited.")
