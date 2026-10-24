"""
Catalog analysis with pandas.

Input: normalized products (scraper.normalize_product) -> DataFrames and summary metrics
used by the Streamlit demo, the CLI export and (later) the email digests.
"""

from __future__ import annotations

import pandas as pd

VARIANT_COLUMNS = [
    "product_id", "product_title", "handle", "vendor", "product_type", "tags", "url",
    "image_url", "created_at", "published_at", "updated_at",
    "variant_id", "variant_title", "sku", "price", "compare_at_price", "available",
]


def products_to_frame(products: list[dict]) -> pd.DataFrame:
    """One row per variant."""
    rows = []
    for p in products:
        for v in p["variants"]:
            rows.append({
                "product_id": p["id"], "product_title": p["title"], "handle": p["handle"],
                "vendor": p["vendor"], "product_type": p["product_type"],
                "tags": ", ".join(p["tags"]), "url": p["url"], "image_url": p["image_url"],
                "created_at": p["created_at"], "published_at": p["published_at"],
                "updated_at": p["updated_at"],
                "variant_id": v["id"], "variant_title": v["title"], "sku": v["sku"],
                "price": v["price"], "compare_at_price": v["compare_at_price"],
                "available": v["available"],
            })
    df = pd.DataFrame(rows, columns=VARIANT_COLUMNS)
    for col in ("created_at", "published_at", "updated_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["compare_at_price"] = pd.to_numeric(df["compare_at_price"], errors="coerce")
    df["available"] = df["available"].astype(bool)
    df["on_sale"] = df["compare_at_price"].notna() & (df["compare_at_price"] > df["price"])
    df["discount_pct"] = ((df["compare_at_price"] - df["price"]) / df["compare_at_price"] * 100) \
        .where(df["on_sale"]).round(1)
    df["product_type"] = df["product_type"].fillna("").replace("", "Uncategorized")
    df["vendor"] = df["vendor"].fillna("").replace("", "Unknown")
    return df


def product_level(df: pd.DataFrame) -> pd.DataFrame:
    """One row per product: price range, sale and stock status."""
    if df.empty:
        return pd.DataFrame(columns=["product_id", "product_title", "vendor", "product_type",
                                     "url", "image_url", "published_at", "min_price", "max_price",
                                     "variants", "on_sale", "max_discount_pct", "available",
                                     "available_share"])
    g = df.groupby("product_id", sort=False)
    out = g.agg(
        product_title=("product_title", "first"), vendor=("vendor", "first"),
        product_type=("product_type", "first"), url=("url", "first"),
        image_url=("image_url", "first"), published_at=("published_at", "first"),
        min_price=("price", "min"), max_price=("price", "max"),
        variants=("variant_id", "count"), on_sale=("on_sale", "any"),
        max_discount_pct=("discount_pct", "max"), available=("available", "any"),
        available_share=("available", "mean"),
    ).reset_index()
    out["available_share"] = (out["available_share"] * 100).round(0)
    return out


def catalog_summary(df: pd.DataFrame) -> dict:
    products = product_level(df)
    n = len(products)
    if n == 0:
        return {"products": 0, "variants": 0}
    on_sale = products["on_sale"]
    return {
        "products": n,
        "variants": int(len(df)),
        "vendors": int(df["vendor"].nunique()),
        "product_types": int(df["product_type"].nunique()),
        "median_price": float(products["min_price"].median()),
        "min_price": float(df["price"].min()),
        "max_price": float(df["price"].max()),
        "on_sale_pct": round(float(on_sale.mean()) * 100, 1),
        "avg_discount_pct": round(float(df.loc[df["on_sale"], "discount_pct"].mean()), 1)
                            if df["on_sale"].any() else 0.0,
        "out_of_stock_pct": round(float((~products["available"]).mean()) * 100, 1),
        "new_last_30d": int((products["published_at"] >=
                             pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)).sum()),
    }


def type_breakdown(df: pd.DataFrame, top: int = 15) -> pd.DataFrame:
    products = product_level(df)
    if products.empty:
        return products
    out = products.groupby("product_type").agg(
        products=("product_id", "count"),
        median_price=("min_price", "median"),
        on_sale_pct=("on_sale", "mean"),
        out_of_stock_pct=("available", lambda s: (~s).mean()),
    ).reset_index()
    out["on_sale_pct"] = (out["on_sale_pct"] * 100).round(1)
    out["out_of_stock_pct"] = (out["out_of_stock_pct"] * 100).round(1)
    out["median_price"] = out["median_price"].round(2)
    return out.sort_values("products", ascending=False).head(top)


def vendor_breakdown(df: pd.DataFrame, top: int = 15) -> pd.DataFrame:
    products = product_level(df)
    if products.empty:
        return products
    return (products.groupby("vendor")
            .agg(products=("product_id", "count"), median_price=("min_price", "median"))
            .round(2).reset_index().sort_values("products", ascending=False).head(top))


def price_endings(df: pd.DataFrame) -> pd.DataFrame:
    """Pricing psychology: how prices end (.99, .95, .00 ...)."""
    prices = df["price"].dropna()
    if prices.empty:
        return pd.DataFrame(columns=["ending", "share_pct"])
    cents = ((prices * 100).round().astype(int) % 100)

    def label(c):
        if c == 0:
            return ".00"
        if c in (99, 95, 90, 50):
            return f".{c:02d}"
        return "other"

    counts = cents.map(label).value_counts(normalize=True) * 100
    return counts.round(1).rename_axis("ending").reset_index(name="share_pct")


def discounted_products(df: pd.DataFrame, top: int = 50) -> pd.DataFrame:
    products = product_level(df)
    sale = products[products["on_sale"]].sort_values("max_discount_pct", ascending=False)
    return sale[["product_title", "product_type", "min_price", "max_discount_pct",
                 "available", "url"]].head(top)


def newest_products(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    products = product_level(df).sort_values("published_at", ascending=False)
    return products[["product_title", "product_type", "min_price", "published_at",
                     "available", "url"]].head(n)


def launch_timeline(df: pd.DataFrame, freq: str = "MS") -> pd.DataFrame:
    """Products published per period (default: per month) - shows launch velocity."""
    products = product_level(df).dropna(subset=["published_at"])
    if products.empty:
        return pd.DataFrame(columns=["period", "launches"])
    s = products.set_index("published_at").resample(freq)["product_id"].count()
    out = s.rename("launches").reset_index().rename(columns={"published_at": "period"})
    out["period"] = out["period"].dt.tz_localize(None)
    return out
