"""
scraper.py
----------
Daily price scraper for HUEI YEH (輝葉) products in the
"按摩用品" (Massage Supplies) category on momoshop.com.tw.

Designed to run inside GitHub Actions. Stores results in a Vercel Postgres
database via the POSTGRES_URL environment variable.

Usage (local):
    export POSTGRES_URL="postgres://..."
    python scraper.py
    python scraper.py --force         # re-scrape even if today exists
    python scraper.py --headed        # show the browser (debugging)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone, timedelta
from typing import Iterator
from urllib.parse import urlencode

import psycopg2
import psycopg2.extras
from playwright.async_api import Browser, async_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEARCH_KEYWORD = "輝葉"           # HUEI YEH brand
CATE_CODE      = "3100000000"     # 按摩用品 (Massage Supplies)
BASE_URL       = "https://www.momoshop.com.tw/search/searchShop.jsp"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

MAX_PAGES_HARD_LIMIT = 50

# Timezone for Taiwan
TW_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db_url() -> str:
    url = os.environ.get("POSTGRES_URL", "")
    if not url:
        print("[db] ERROR: POSTGRES_URL environment variable is not set.")
        sys.exit(1)
    # Vercel Postgres sometimes uses postgres:// which psycopg2 needs as postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_conn():
    return psycopg2.connect(get_db_url())


def init_db():
    """Create the momo_prices table if it doesn't exist."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS momo_prices (
                    id              SERIAL PRIMARY KEY,
                    product_name    TEXT        NOT NULL,
                    original_price  INTEGER,
                    discount_price  INTEGER     NOT NULL,
                    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    unique_key      TEXT        NOT NULL
                );
            """)
            # Unique index: one record per product per timestamp
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uix_momo_prices_key_day
                    ON momo_prices (unique_key, timestamp);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_momo_prices_key
                    ON momo_prices (unique_key);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_momo_prices_ts
                    ON momo_prices (timestamp);
            """)
        conn.commit()
    finally:
        conn.close()


def make_unique_key(product_name: str, original_price: int | None) -> str:
    """Derive a stable unique key from (name + original_price)."""
    key = f"{product_name.strip()}|{original_price or 0}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def already_ran_today() -> bool:
    """Return True if at least one row exists for today (Asia/Taipei)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM momo_prices
                 WHERE (timestamp AT TIME ZONE 'Asia/Taipei')::date = (NOW() AT TIME ZONE 'Asia/Taipei')::date
                 LIMIT 1
            """)
            return cur.fetchone() is not None
    finally:
        conn.close()


def insert_prices(records: list[dict]) -> int:
    """Insert today's scraped records. Returns number of rows inserted."""
    now = datetime.now(TW_TZ)
    conn = get_conn()
    inserted = 0
    try:
        with conn.cursor() as cur:
            for r in records:
                try:
                    cur.execute("""
                        INSERT INTO momo_prices
                            (product_name, original_price, discount_price, timestamp, unique_key)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (unique_key, timestamp) DO NOTHING
                    """, (
                        r["product_name"],
                        r["original_price"],
                        r["discount_price"],
                        now,
                        r["unique_key"],
                    ))
                    if cur.rowcount > 0:
                        inserted += 1
                except psycopg2.Error as e:
                    print(f"[db] Insert error for {r['product_name'][:30]}: {e}")
                    conn.rollback()
                    continue
        conn.commit()
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------
def build_search_url(page_num: int = 1) -> str:
    params = {
        "searchKeyword": SEARCH_KEYWORD,
        "cateCode":      CATE_CODE,
        "cateLevel":     "2",
        "searchType":    "1",
        "curPage":       str(page_num),
    }
    return f"{BASE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Next.js payload extraction
# ---------------------------------------------------------------------------
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL)


def _extract_next_payload(html: str) -> str:
    """Concatenate and unescape every __next_f.push() chunk in the HTML."""
    chunks = _PUSH_RE.findall(html)
    out = []
    for chunk in chunks:
        unescaped = chunk.replace('\\"', '"').replace('\\\\', '\\')
        out.append(unescaped)
    return "".join(out)


def _iter_goods_objects(payload: str) -> Iterator[dict]:
    """Yield each product dict found in the payload."""
    for match in re.finditer(r'\{"goodsCode":"\d+"', payload):
        start = match.start()
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(payload)):
            ch = payload[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    raw = payload[start : i + 1]
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        pass
                    break


_PRICE_NUM_RE = re.compile(r"[\d,]+")


def _to_int_price(price_str: str | None) -> int | None:
    """'$$5,680' -> 5680. Returns None if nothing parseable."""
    if not price_str:
        return None
    m = _PRICE_NUM_RE.search(price_str)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_page(html: str) -> tuple[list[dict], int]:
    """
    Parse one search results page.

    Returns (records, max_page) where:
        records  – list of {unique_key, product_name, original_price, discount_price}
        max_page – total number of pages momo says exist for this query
    """
    payload = _extract_next_payload(html)

    max_page = 1
    m = re.search(r'"maxPage":(\d+)', payload)
    if m:
        max_page = int(m.group(1))

    records, seen_codes = [], set()
    for goods in _iter_goods_objects(payload):
        pid = goods.get("goodsCode")
        if not pid or pid in seen_codes:
            continue
        seen_codes.add(pid)

        # Sale price (discount_price)
        price = None
        gpm = goods.get("goodsPriceModel") or {}
        bp  = gpm.get("basePrice") or {}
        price = _to_int_price(bp.get("price")) or _to_int_price(goods.get("goodsPrice"))

        # Market / list price (original_price — the strikethrough)
        mpm = goods.get("marketPriceModel") or {}
        mbp = mpm.get("basePrice") or {}
        market = _to_int_price(mbp.get("price")) or _to_int_price(goods.get("goodsPriceOri"))

        name = (goods.get("goodsName") or "").strip()
        if not name or price is None:
            continue

        unique_key = make_unique_key(name, market)

        records.append({
            "unique_key":      unique_key,
            "product_name":    name,
            "original_price":  market,
            "discount_price":  price,
        })

    return records, max_page


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------
async def fetch_html(browser: Browser, url: str, ua: str) -> str:
    """Open a fresh context, load the URL, scroll to bottom, return HTML."""
    ctx = await browser.new_context(
        user_agent=ua,
        locale="zh-TW",
        timezone_id="Asia/Taipei",
        viewport={"width": 1366, "height": 900},
    )
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(4_000)

        # Scroll to bottom to trigger lazy-loaded content
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3_000)

        return await page.content()
    finally:
        await ctx.close()


async def scrape(headed: bool = False) -> tuple[list[dict], int]:
    """Walk every page of the search result and return all product records."""
    all_records: dict[str, dict] = {}  # dedupe by unique_key across pages
    total_scraped = 0
    max_retries = 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page_num = 1
            max_page = 1
            while page_num <= max_page and page_num <= MAX_PAGES_HARD_LIMIT:
                ua = USER_AGENTS[(page_num - 1) % len(USER_AGENTS)]
                url = build_search_url(page_num)
                print(f"[scrape] page {page_num} → {url}")

                records = []
                for attempt in range(max_retries + 1):
                    html = await fetch_html(browser, url, ua)
                    records, max_page = parse_page(html)
                    if records:
                        break
                    if attempt < max_retries:
                        print(f"[scrape]   got 0 products, retrying in 10s "
                              f"(attempt {attempt + 1}/{max_retries})...")
                        await asyncio.sleep(10)

                total_scraped += len(records)
                print(f"[scrape]   parsed {len(records)} products "
                      f"(maxPage={max_page})")
                for r in records:
                    all_records.setdefault(r["unique_key"], r)
                page_num += 1
                await asyncio.sleep(2)
        finally:
            await browser.close()
    return list(all_records.values()), total_scraped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force",  action="store_true",
                    help="run even if today's scrape is already in the DB")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window (debugging)")
    args = ap.parse_args()

    init_db()

    if already_ran_today() and not args.force:
        print("[scraper] today already scraped — skipping. Use --force to override.")
        return 0

    unique_records, total_scraped = asyncio.run(scrape(headed=args.headed))
    if not unique_records:
        print("[scraper] no products parsed — aborting without DB write.")
        return 1

    # Verification line
    print(f"Total Scraped: {total_scraped} | "
          f"Unique Matches: {len(unique_records)} | "
          f"Target: 79")

    inserted = insert_prices(unique_records)
    print(f"[scraper] {len(unique_records)} unique products, "
          f"{inserted} new rows written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
