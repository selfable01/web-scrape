"""
view_data.py
------------
Quick utility to inspect raw data in momo_prices.db.

Usage
-----
    python view_data.py                  # summary + last 7 days of data
    python view_data.py --all            # dump every row
    python view_data.py --product 12345  # history for one product
    python view_data.py --dates          # list all scrape dates with row counts
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "momo_prices.db"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run 'python scraper.py' first to create it.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def show_summary() -> None:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    dates = conn.execute("SELECT COUNT(DISTINCT date) FROM prices").fetchone()[0]
    products = conn.execute("SELECT COUNT(DISTINCT product_id) FROM prices").fetchone()[0]
    date_range = conn.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()

    print("=" * 60)
    print("  momo_prices.db — Summary")
    print("=" * 60)
    print(f"  Total rows       : {total}")
    print(f"  Distinct dates   : {dates}")
    print(f"  Distinct products: {products}")
    if date_range[0]:
        print(f"  Date range       : {date_range[0]} → {date_range[1]}")
    else:
        print("  Date range       : (empty database)")
    print()
    conn.close()


def show_dates() -> None:
    conn = _connect()
    rows = conn.execute(
        "SELECT date, COUNT(*) AS products FROM prices GROUP BY date ORDER BY date"
    ).fetchall()
    print(f"{'Date':>12}  {'Products':>8}")
    print("-" * 22)
    for r in rows:
        print(f"{r['date']:>12}  {r['products']:>8}")
    conn.close()


def show_product(product_id: str) -> None:
    conn = _connect()
    rows = conn.execute(
        "SELECT date, product_name, price, market_price FROM prices "
        "WHERE product_id = ? ORDER BY date",
        (product_id,),
    ).fetchall()
    if not rows:
        print(f"No data found for product_id={product_id}")
        conn.close()
        return
    print(f"History for {product_id}: {rows[0]['product_name']}")
    print(f"{'Date':>12}  {'Price':>8}  {'Market':>8}")
    print("-" * 32)
    for r in rows:
        mp = f"{r['market_price']:>8,}" if r["market_price"] else "     N/A"
        print(f"{r['date']:>12}  {r['price']:>8,}  {mp}")
    conn.close()


def show_all() -> None:
    conn = _connect()
    rows = conn.execute(
        "SELECT date, product_id, product_name, price, market_price "
        "FROM prices ORDER BY date, product_id"
    ).fetchall()
    print(f"{'Date':>12}  {'ID':>10}  {'Price':>8}  {'Market':>8}  Name")
    print("-" * 80)
    for r in rows:
        mp = f"{r['market_price']:>8,}" if r["market_price"] else "     N/A"
        name = r["product_name"][:40]
        print(f"{r['date']:>12}  {r['product_id']:>10}  "
              f"{r['price']:>8,}  {mp}  {name}")
    print(f"\n({len(rows)} rows)")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="dump every row")
    ap.add_argument("--product", type=str, help="show history for one product ID")
    ap.add_argument("--dates", action="store_true", help="list scrape dates")
    args = ap.parse_args()

    if args.product:
        show_product(args.product)
    elif args.dates:
        show_dates()
    elif args.all:
        show_all()
    else:
        show_summary()
        print("Tip: use --all, --dates, or --product <id> for more detail.")
        print()
        print("Useful SQL you can run directly:")
        print('  sqlite3 momo_prices.db "SELECT * FROM prices ORDER BY date, product_id;"')
        print('  sqlite3 momo_prices.db "SELECT date, COUNT(*) FROM prices GROUP BY date;"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
