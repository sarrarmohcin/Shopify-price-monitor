"""Settings read from environment variables (or a .env file)."""

import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/spm.db")

# Scraper politeness
REQUEST_DELAY = float(os.getenv("SPM_REQUEST_DELAY", "1.0"))   # seconds between pages
REQUEST_TIMEOUT = float(os.getenv("SPM_REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("SPM_MAX_RETRIES", "4"))
USER_AGENT = os.getenv(
    "SPM_USER_AGENT",
    "ShopifyPriceMonitor/0.1 (+https://github.com/your-username/shopify-price-monitor)",
)

# Diff engine
MIN_PRICE_CHANGE_PCT = float(os.getenv("SPM_MIN_PRICE_CHANGE_PCT", "0"))  # ignore tiny changes
SITEWIDE_SALE_THRESHOLD = float(os.getenv("SPM_SITEWIDE_SALE_THRESHOLD", "0.25"))

# Public demo limits
DEMO_MAX_PAGES = int(os.getenv("SPM_DEMO_MAX_PAGES", "4"))          # 4 x 250 = 1,000 products
DEMO_MAX_ANALYSES_PER_SESSION = int(os.getenv("SPM_DEMO_MAX_ANALYSES", "10"))
DEMO_ALLOW_TRACKING = os.getenv("SPM_DEMO_ALLOW_TRACKING", "false").lower() == "true"
