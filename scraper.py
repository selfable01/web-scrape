"""
scraper.py
----------
Daily price scraper for HUEI YEH (輝葉) products in the
"按摩用品" (Massage Supplies) category on momoshop.com.tw.

Strategy
========
momoshop's search results are rendered by a Next.js front-end. The fully
hydrated product data (goodsCode / goodsName / goodsPrice / marketPrice /
maxPage …) is embedded inside `self.__next_f.push([...])` chunks in the
delivered HTML. Parsing this JSON payload is **far more robust than CSS
selector scraping**:

  * It survives layout / Tailwind / class-name changes.
  * It contains the canonical product ID momo uses internally.
  * It exposes `maxPage`, removing any guessing about pagination.

We still drive the request through Playwright (headless Chromium) because
momo gates raw `requests` traffic with anti-bot checks; a real browser
fingerprint with cookies + JS handshake is the safest path.

Product Identity
================
Products are identified by (product_name + market_price) rather than
momo's goodsCode. Items that share the same name and original price are
treated as the same product and their price history is merged.

Usage
-----
    python scraper.py                 # run today's scrape (skip if done)
    python scraper.py --force         # re-scrape even if today exists
    python scraper.py --headed        # show the browser (debugging)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from typing import Iterator
from urllib.parse import urlencode

from playwright.async_api import Browser, async_playwright

import db_manager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEARCH_KEYWORD = "輝葉"           # HUEI YEH brand
CATE_CODE      = "3100000000"     # 按摩用品 (Massage Supplies)
BASE_URL       = "https://www.momoshop.com.tw/search/searchShop.jsp"

# Rotate among realistic desktop user-agents
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Hard ceiling so a momo bug can never push us into infinite pagination
MAX_PAGES_HARD_LIMIT = 50

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
        records  – list of {canonical_id, product_id, product_name, price,
                            market_price, url}
        max_page – total number of pages momo says exist for this query
    """
    payload = _extract_next_payload(html)

    # maxPage lives on the search result root object
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

        # Sale price: prefer the structured model, fall back to flat field
        price = None
        gpm = goods.get("goodsPriceModel") or {}
        bp  = gpm.get("basePrice") or {}
        price = _to_int_price(bp.get("price")) or _to_int_price(goods.get("goodsPrice"))

        # Market (list) price: optional context for "is this a discount?"
        mpm = goods.get("marketPriceModel") or {}
        mbp = mpm.get("basePrice") or {}
        market = _to_int_price(mbp.get("price")) or _to_int_price(goods.get("goodsPriceOri"))

        name = (goods.get("goodsName") or "").strip()
        if not name or price is None:
            continue  # skip malformed entries

        canonical_id = db_manager.make_canonical_id(name, market)

        records.append({
            "canonical_id": canonical_id,
            "product_id":   pid,
            "product_name": name,
            "price":        price,
            "market_price": market,
            "url":          f"https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={pid}",
        })

    return records, max_page


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------
async def fetch_html(browser: Browser, url: str, ua: str) -> str:
    """Open a fresh context (clean cookies), load the URL, return HTML.

    Scrolls to the bottom of the page and waits 2 seconds so that all
    lazy-loaded prices and images are fully rendered before extraction.
    """
    ctx = await browser.new_context(
        user_agent=ua,
        locale="zh-TW",
        timezone_id="Asia/Taipei",
        viewport={"width": 1366, "height": 900},
    )
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        # Give Next.js time to stream the rest of the chunks
        await page.wait_for_timeout(4_000)

        # Scroll to bottom to trigger lazy-loaded content (prices/images)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3_000)

        return await page.content()
    finally:
        await ctx.close()


async def _wait_for_network() -> None:
    """Wait up to 60 seconds for network connectivity after wake-from-sleep."""
    import socket
    for attempt in range(12):
        try:
            socket.create_connection(("www.momoshop.com.tw", 443), timeout=5)
            print("[scrape] Network is ready.")
            return
        except OSError:
            wait = 5
            print(f"[scrape] Network not ready, retrying in {wait}s "
                  f"(attempt {attempt + 1}/12)...")
            await asyncio.sleep(wait)
    print("[scrape] WARNING: Network may not be ready, proceeding anyway.")


async def scrape(headed: bool = False) -> list[dict]:
    """Walk every page of the search result and return all product records."""
    # Wait for network (important after wake-from-sleep)
    await _wait_for_network()

    all_records: dict[str, dict] = {}  # dedupe by canonical_id across pages
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

                # Retry logic for pages that return 0 products
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
                    all_records.setdefault(r["canonical_id"], r)
                page_num += 1
                # Polite delay between page fetches
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

    db_manager.init_db()

    today = date.today().isoformat()
    if db_manager.already_ran_today(today) and not args.force:
        print(f"[scraper] today ({today}) already scraped — skipping. "
              "Use --force to override.")
        return 0

    unique_records, total_scraped = asyncio.run(scrape(headed=args.headed))
    if not unique_records:
        print("[scraper] no products parsed — aborting without DB write.")
        return 1

    # Verification line
    print(f"Scraped: {total_scraped} items | "
          f"Unique Products (Name+Price matching): {len(unique_records)} | "
          f"Expected: 79")

    inserted = db_manager.insert_prices(unique_records, run_date=today)
    print(f"[scraper] {len(unique_records)} unique products, "
          f"{inserted} new rows written for {today}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
