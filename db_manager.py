"""
db_manager.py
-------------
SQLite storage layer for the momo HUEI YEH (輝葉) price tracker.

Schema:
    prices(date TEXT, canonical_id TEXT, product_id TEXT, product_name TEXT,
           price INTEGER, market_price INTEGER, url TEXT, scraped_at TEXT)
    PRIMARY KEY (date, canonical_id)  -- one row per unique product per day

Products are identified by (product_name + market_price) rather than
momo's goodsCode, so items that share the same name and original price
are merged into a single price-history trend line.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path(__file__).parent / "momo_prices.db"


def make_canonical_id(product_name: str, market_price: int | None) -> str:
    """Derive a stable canonical ID from (name + original/market price).

    Returns a short hex hash so the DB key stays compact.
    """
    key = f"{product_name.strip()}|{market_price or 0}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
@contextmanager
def get_conn(db_path: Path = DB_PATH):
    """Context-managed sqlite3 connection with row factory and foreign keys."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the prices table and helpful indexes if they don't yet exist.

    If the old schema (keyed by product_id) exists, migrate it first.
    """
    # Migrate old schema if needed (must happen before CREATE TABLE IF NOT EXISTS)
    if db_path.exists():
        migrate_old_schema(db_path)

    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prices (
                date          TEXT    NOT NULL,         -- ISO date 'YYYY-MM-DD'
                canonical_id  TEXT    NOT NULL,         -- hash of name+market_price
                product_id    TEXT    NOT NULL,         -- momo goodsCode (kept for URL)
                product_name  TEXT    NOT NULL,
                price         INTEGER NOT NULL,         -- current sale price (TWD)
                market_price  INTEGER,                  -- list price, optional
                url           TEXT,
                scraped_at    TEXT,                      -- ISO timestamp of scrape
                PRIMARY KEY (date, canonical_id)
            );

            CREATE INDEX IF NOT EXISTS idx_prices_cid_date
                ON prices(canonical_id, date);
            CREATE INDEX IF NOT EXISTS idx_prices_date
                ON prices(date);
            """
        )


# ---------------------------------------------------------------------------
# Migration: import old data keyed by product_id into new schema
# ---------------------------------------------------------------------------
def migrate_old_schema(db_path: Path = DB_PATH) -> None:
    """One-time migration from old (date, product_id) PK to (date, canonical_id).

    If the old table exists without a canonical_id column, copy data into the
    new schema and drop the old table.
    """
    with get_conn(db_path) as conn:
        # Check if canonical_id column already exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(prices)").fetchall()]
        if "canonical_id" in cols:
            return  # already migrated

        print("[migrate] Moving old data to new schema (canonical_id)...")
        # Rename old table
        conn.execute("ALTER TABLE prices RENAME TO prices_old")

        # Create new table
        conn.executescript(
            """
            CREATE TABLE prices (
                date          TEXT    NOT NULL,
                canonical_id  TEXT    NOT NULL,
                product_id    TEXT    NOT NULL,
                product_name  TEXT    NOT NULL,
                price         INTEGER NOT NULL,
                market_price  INTEGER,
                url           TEXT,
                scraped_at    TEXT,
                PRIMARY KEY (date, canonical_id)
            );
            CREATE INDEX IF NOT EXISTS idx_prices_cid_date
                ON prices(canonical_id, date);
            CREATE INDEX IF NOT EXISTS idx_prices_date
                ON prices(date);
            """
        )

        # Copy rows, generating canonical_id for each
        old_rows = conn.execute(
            "SELECT date, product_id, product_name, price, market_price, url, "
            "CASE WHEN EXISTS(SELECT 1 FROM pragma_table_info('prices_old') WHERE name='scraped_at') "
            "THEN scraped_at ELSE NULL END AS scraped_at "
            "FROM prices_old"
        ).fetchall()

        # Simpler: just read all columns that exist
        old_rows = conn.execute(
            "SELECT * FROM prices_old"
        ).fetchall()

        migrated = 0
        for row in old_rows:
            row_dict = dict(row)
            cid = make_canonical_id(
                row_dict["product_name"],
                row_dict.get("market_price"),
            )
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO prices
                       (date, canonical_id, product_id, product_name, price,
                        market_price, url, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row_dict["date"],
                        cid,
                        row_dict["product_id"],
                        row_dict["product_name"],
                        row_dict["price"],
                        row_dict.get("market_price"),
                        row_dict.get("url"),
                        row_dict.get("scraped_at"),
                    ),
                )
                migrated += 1
            except sqlite3.IntegrityError:
                pass  # duplicate canonical_id on same date — skip

        conn.execute("DROP TABLE prices_old")
        print(f"[migrate] Done — {migrated} rows migrated.")


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------
def already_ran_today(run_date: Optional[str] = None,
                      db_path: Path = DB_PATH) -> bool:
    """Return True if at least one row already exists for the given date."""
    run_date = run_date or date.today().isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM prices WHERE date = ? LIMIT 1", (run_date,)
        ).fetchone()
    return row is not None


def insert_prices(records: Iterable[dict],
                  run_date: Optional[str] = None,
                  db_path: Path = DB_PATH) -> int:
    """
    Insert today's scraped records. Each record must contain:
        canonical_id, product_id, product_name, price
    Optional: market_price, url

    Uses INSERT OR IGNORE so re-running on the same day is a safe no-op
    for already-stored products.

    Returns the number of rows actually inserted.
    """
    run_date = run_date or date.today().isoformat()
    now_ts = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            run_date,
            str(r["canonical_id"]),
            str(r["product_id"]),
            r["product_name"],
            int(r["price"]),
            int(r["market_price"]) if r.get("market_price") else None,
            r.get("url"),
            now_ts,
        )
        for r in records
    ]
    with get_conn(db_path) as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO prices
                  (date, canonical_id, product_id, product_name, price,
                   market_price, url, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------
def get_price_history(canonical_id: str,
                      days: int = 7,
                      db_path: Path = DB_PATH) -> list[sqlite3.Row]:
    """Return rows for the last `days` days, oldest first."""
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    with get_conn(db_path) as conn:
        return conn.execute(
            """SELECT date, product_name, price, market_price
                 FROM prices
                WHERE canonical_id = ? AND date >= ?
                ORDER BY date ASC""",
            (canonical_id, cutoff),
        ).fetchall()


def list_tracked_products(db_path: Path = DB_PATH) -> list[sqlite3.Row]:
    """Return distinct (canonical_id, product_name) seen in the database."""
    with get_conn(db_path) as conn:
        return conn.execute(
            """SELECT canonical_id,
                      MAX(product_name) AS product_name,
                      COUNT(*)          AS days_tracked,
                      MIN(date)         AS first_seen,
                      MAX(date)         AS last_seen
                 FROM prices
             GROUP BY canonical_id
             ORDER BY product_name"""
        ).fetchall()


if __name__ == "__main__":
    # Quick smoke test / inspection helper:  python db_manager.py
    init_db()
    print(f"DB initialised at {DB_PATH}")
    products = list_tracked_products()
    print(f"Tracked products: {len(products)}")
    for p in products[:10]:
        print(f"  {p['canonical_id']:>16}  {p['days_tracked']:>3}d  "
              f"{p['first_seen']}→{p['last_seen']}  {p['product_name'][:50]}")
