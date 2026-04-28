"""
scraper.py
----------
Per-user price scraper for HUEI YEH products on momoshop.com.tw.

Runs via GitHub Actions on an hourly cron. Each hour it:
  1. Queries the users table for users whose scrape_time has arrived
  2. Scrapes momo once (shared across all due users)
  3. Inserts the results into each due user's account (user_id)

Usage:
    python scraper.py --check-schedule     # production: hourly mode
    python scraper.py --force              # dev: scrape for ALL users
    python scraper.py --headed             # dev: show browser window
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Iterator
from urllib.parse import urlencode

import psycopg2
import psycopg2.extras
from playwright.async_api import Browser, async_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEARCH_KEYWORD = "輝葉"
CATE_CODE      = "3100000000"
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
MAX_RETRIES = 3
RETRY_DELAY_S = 10
PAGE_DELAY_S = 2

TW_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db_url() -> str:
    url = os.environ.get("POSTGRES_URL", "")
    if not url:
        print("[db] ERROR: POSTGRES_URL environment variable is not set.")
        sys.exit(1)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_conn():
    return psycopg2.connect(get_db_url())


def init_db():
    """Create tables if they don't exist (indexes managed via schema.sql)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id              SERIAL PRIMARY KEY,
                    username        TEXT        NOT NULL UNIQUE,
                    email           TEXT        NOT NULL UNIQUE,
                    password_hash   TEXT        NOT NULL,
                    scrape_time     TIME        NOT NULL DEFAULT '11:00',
                    history_days    INTEGER     NOT NULL DEFAULT 7,
                    last_scrape_at  TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS momo_prices (
                    id              SERIAL PRIMARY KEY,
                    user_id         INTEGER     REFERENCES users(id) ON DELETE CASCADE,
                    product_name    TEXT        NOT NULL,
                    original_price  INTEGER,
                    discount_price  INTEGER     NOT NULL,
                    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    unique_key      TEXT        NOT NULL
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_momo_prices_key
                    ON momo_prices (unique_key);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_momo_prices_ts
                    ON momo_prices (timestamp);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_momo_prices_user
                    ON momo_prices (user_id);
            """)
        conn.commit()
    finally:
        conn.close()


def make_unique_key(product_name: str, original_price: int | None) -> str:
    key = f"{product_name.strip()}|{original_price or 0}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# User schedule queries
# ---------------------------------------------------------------------------
def get_users_due_now() -> list[dict]:
    """
    Users whose scrape_time has passed (full HH:MM comparison, not just hour)
    AND haven't been scraped today.

    Example: user scrape_time=10:15, current Taipei time=10:20,
    last_scrape_at is NULL → this user is due.
    Cron runs every 15 min, so max wait is ~15 minutes after scrape_time.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, username, scrape_time, history_days
                  FROM users
                 WHERE (NOW() AT TIME ZONE 'Asia/Taipei')::time >= scrape_time
                   AND (
                       last_scrape_at IS NULL
                       OR (last_scrape_at AT TIME ZONE 'Asia/Taipei')::date
                          < (NOW() AT TIME ZONE 'Asia/Taipei')::date
                   )
            """)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_all_users() -> list[dict]:
    """Return every registered user (for --force mode)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, username, scrape_time, history_days FROM users")
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def update_last_scrape(user_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_scrape_at = NOW() WHERE id = %s",
                (user_id,),
            )
        conn.commit()
    finally:
        conn.close()


def insert_prices(records: list[dict], user_id: int) -> int:
    """Insert scraped records for a specific user. Returns rows inserted."""
    now = datetime.now(TW_TZ)
    conn = get_conn()
    inserted = 0
    try:
        with conn.cursor() as cur:
            for r in records:
                try:
                    cur.execute("""
                        INSERT INTO momo_prices
                            (user_id, product_name, original_price, discount_price,
                             timestamp, unique_key)
                        SELECT %s, %s, %s, %s, %s, %s
                         WHERE NOT EXISTS (
                            SELECT 1 FROM momo_prices
                             WHERE user_id = %s
                               AND unique_key = %s
                               AND CAST(timestamp AT TIME ZONE 'Asia/Taipei' AS date)
                                   = CAST(%s AT TIME ZONE 'Asia/Taipei' AS date)
                         )
                    """, (
                        user_id,
                        r["product_name"],
                        r["original_price"],
                        r["discount_price"],
                        now,
                        r["unique_key"],
                        user_id,
                        r["unique_key"],
                        now,
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
    chunks = _PUSH_RE.findall(html)
    out = []
    for chunk in chunks:
        unescaped = chunk.replace('\\"', '"').replace('\\\\', '\\')
        out.append(unescaped)
    return "".join(out)


def _iter_goods_objects(payload: str) -> Iterator[dict]:
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

        gpm = goods.get("goodsPriceModel") or {}
        bp  = gpm.get("basePrice") or {}
        price = _to_int_price(bp.get("price")) or _to_int_price(goods.get("goodsPrice"))

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
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3_000)
        return await page.content()
    except Exception as e:
        print(f"[fetch] Error loading {url}: {e}")
        return ""
    finally:
        await ctx.close()


async def scrape(headed: bool = False) -> tuple[list[dict], int]:
    """Scrape all pages. Returns (unique_records, total_scraped)."""
    all_records: dict[str, dict] = {}
    total_scraped = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page_num = 1
            max_page = 1
            consecutive_empty = 0
            while page_num <= max_page and page_num <= MAX_PAGES_HARD_LIMIT:
                ua = USER_AGENTS[(page_num - 1) % len(USER_AGENTS)]
                url = build_search_url(page_num)
                print(f"[scrape] page {page_num} -> {url}")

                records = []
                for attempt in range(MAX_RETRIES + 1):
                    html = await fetch_html(browser, url, ua)
                    if not html:
                        if attempt < MAX_RETRIES:
                            print(f"[scrape]   empty response, retry {attempt+1}/{MAX_RETRIES}...")
                            await asyncio.sleep(RETRY_DELAY_S)
                            continue
                        break
                    records, max_page = parse_page(html)
                    if records:
                        break
                    if attempt < MAX_RETRIES:
                        print(f"[scrape]   0 products, retry {attempt+1}/{MAX_RETRIES}...")
                        await asyncio.sleep(RETRY_DELAY_S)

                if not records:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        print("[scrape]   3 consecutive empty pages — stopping.")
                        break
                else:
                    consecutive_empty = 0

                total_scraped += len(records)
                print(f"[scrape]   {len(records)} products (maxPage={max_page})")
                for r in records:
                    all_records.setdefault(r["unique_key"], r)
                page_num += 1
                await asyncio.sleep(PAGE_DELAY_S)
        finally:
            await browser.close()
    return list(all_records.values()), total_scraped


# ---------------------------------------------------------------------------
# Per-user distribution
# ---------------------------------------------------------------------------
def distribute_to_user(user: dict, records: list[dict]):
    """Insert scraped products into one user's account."""
    user_id = user["id"]
    username = user["username"]
    inserted = insert_prices(records, user_id)
    update_last_scrape(user_id)
    print(f"[scheduler] user '{username}' (id={user_id}): "
          f"{inserted} new rows written.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="scrape for ALL registered users regardless of schedule")
    ap.add_argument("--headed", action="store_true",
                    help="show browser window (debugging)")
    ap.add_argument("--check-schedule", action="store_true",
                    help="hourly mode: find due users, scrape, distribute")
    args = ap.parse_args()

    init_db()

    # ── Decide which users to scrape for ─────────────────────────────────
    if args.check_schedule:
        users = get_users_due_now()
        if not users:
            print("[scheduler] no users due right now — exiting.")
            return 0
        print(f"[scheduler] {len(users)} user(s) due: "
              f"{', '.join(u['username'] for u in users)}")
    elif args.force:
        users = get_all_users()
        if not users:
            print("[scheduler] no registered users — exiting.")
            return 0
        print(f"[scheduler] --force: scraping for all {len(users)} user(s)")
    else:
        # Default: same as --check-schedule
        users = get_users_due_now()
        if not users:
            print("[scheduler] no users due right now — exiting.")
            return 0
        print(f"[scheduler] {len(users)} user(s) due: "
              f"{', '.join(u['username'] for u in users)}")

    # ── Scrape once (shared across all due users) ────────────────────────
    print("[scrape] starting browser...")
    unique_records, total_scraped = asyncio.run(scrape(headed=args.headed))

    if not unique_records:
        print("[scrape] no products parsed — aborting.")
        return 1

    print(f"[scrape] done. Total: {total_scraped} | "
          f"Unique: {len(unique_records)} | Target: ~79")

    # ── Distribute results to each user's account ────────────────────────
    for user in users:
        distribute_to_user(user, unique_records)

    print(f"[scheduler] finished. {len(users)} user(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
