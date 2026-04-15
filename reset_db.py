"""
reset_db.py
-----------
Clears all garbled data from momo_prices.db and re-initializes the schema.
Run this ONCE, then re-run scraper.py to populate with clean Chinese text.

Usage:
    python reset_db.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "momo_prices.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH} — nothing to reset.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]

    print(f"Database: {DB_PATH}")
    print(f"Rows to delete: {count}")

    if count == 0:
        print("Already empty — nothing to do.")
        conn.close()
        return 0

    confirm = input("Delete all rows and start fresh? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        conn.close()
        return 0

    conn.execute("DELETE FROM prices")
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    print(f"Deleted {count} rows. Database is now empty.")
    print("Run 'python scraper.py --force' to re-scrape with clean data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
