"""
app.py — Flask dashboard for the momo HUEI YEH price tracker.

Routes
------
GET  /                            Dashboard: products + latest prices + sparkline
GET  /product/<canonical_id>      Detail page with full price history chart
GET  /full-list                   Every record ever saved since Day 1
GET  /api/products                JSON list of all tracked products
GET  /api/history/<canonical_id>  JSON price history for one product
POST /api/scrape                  Trigger a scrape (background thread)
GET  /api/scrape/status           Current scrape job status

Run:
    python app.py
Then open http://127.0.0.1:5000
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import db_manager
import scraper

app = Flask(__name__)
app.json.ensure_ascii = False   # emit real CJK chars, not \uXXXX escapes
db_manager.init_db()

# ---------------------------------------------------------------------------
# Background scrape job state (single-job, in-memory)
# ---------------------------------------------------------------------------
_job = {
    "running":  False,
    "started":  None,
    "finished": None,
    "message":  "idle",
    "inserted": 0,
    "found":    0,
    "error":    None,
}
_job_lock = threading.Lock()


def _run_scrape_job(force: bool) -> None:
    """Worker run inside a background thread."""
    global _job
    try:
        today = date.today().isoformat()
        if db_manager.already_ran_today(today) and not force:
            with _job_lock:
                _job.update(
                    running=False,
                    finished=datetime.now().isoformat(timespec="seconds"),
                    message=f"Skipped — {today} already in DB. Use force to override.",
                )
            return

        with _job_lock:
            _job["message"] = "Launching headless Chromium…"

        # Each thread needs its own asyncio event loop
        unique_records, total_scraped = asyncio.run(scraper.scrape(headed=False))

        if not unique_records:
            with _job_lock:
                _job.update(
                    running=False,
                    finished=datetime.now().isoformat(timespec="seconds"),
                    message="No products parsed — nothing written.",
                    error="empty result",
                )
            return

        inserted = db_manager.insert_prices(unique_records, run_date=today)
        with _job_lock:
            _job.update(
                running=False,
                finished=datetime.now().isoformat(timespec="seconds"),
                found=len(unique_records),
                inserted=inserted,
                message=f"Done — {total_scraped} scraped, "
                        f"{len(unique_records)} unique, "
                        f"{inserted} new rows for {today}.",
            )
    except Exception as exc:  # noqa: BLE001
        with _job_lock:
            _job.update(
                running=False,
                finished=datetime.now().isoformat(timespec="seconds"),
                error=str(exc),
                message=f"Failed: {exc}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _latest_for_each_product(days: int = 30) -> list[dict]:
    """
    Return one row per product with their latest price plus the price from
    `days` ago (or the earliest known) so we can show movement.
    """
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    with db_manager.get_conn() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT canonical_id, MAX(date) AS last_date
                  FROM prices
              GROUP BY canonical_id
            ),
            earliest AS (
                SELECT canonical_id, MIN(date) AS first_date
                  FROM prices
                 WHERE date >= ?
              GROUP BY canonical_id
            )
            SELECT  p.canonical_id,
                    p.product_id,
                    p.product_name,
                    p.price        AS latest_price,
                    p.market_price AS market_price,
                    p.url          AS url,
                    p.date         AS latest_date,
                    p0.price       AS first_price,
                    p0.date        AS first_date
              FROM latest l
              JOIN prices p
                ON p.canonical_id = l.canonical_id AND p.date = l.last_date
              LEFT JOIN earliest e
                ON e.canonical_id = p.canonical_id
              LEFT JOIN prices p0
                ON p0.canonical_id = e.canonical_id AND p0.date = e.first_date
          ORDER BY p.product_name
            """,
            (cutoff,),
        ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        if r.get("first_price") and r.get("latest_price"):
            r["delta"] = r["latest_price"] - r["first_price"]
            r["delta_pct"] = (r["delta"] / r["first_price"] * 100) if r["first_price"] else 0
        else:
            r["delta"], r["delta_pct"] = 0, 0
    return rows


def _history_series(canonical_id: str, days: int) -> list[dict]:
    rows = db_manager.get_price_history(canonical_id, days=days)
    return [{"date": r["date"], "price": r["price"],
             "market_price": r["market_price"]} for r in rows]


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    products = _latest_for_each_product(days=30)
    # Attach 7-day sparkline data
    for p in products:
        p["spark"] = _history_series(p["canonical_id"], days=7)
    today = date.today().isoformat()
    has_today = db_manager.already_ran_today(today)
    return render_template("index.html",
                           products=products,
                           today=today,
                           has_today=has_today)


@app.route("/full-list")
def full_list():
    """Display every single record ever saved in the database."""
    with db_manager.get_conn() as conn:
        rows = conn.execute(
            """SELECT date, canonical_id, product_id, product_name, price,
                      market_price, url
                 FROM prices
             ORDER BY date DESC, product_name ASC"""
        ).fetchall()
    records = [dict(r) for r in rows]
    return render_template("full_list.html", records=records)


@app.route("/product/<canonical_id>")
def product_detail(canonical_id: str):
    days = int(request.args.get("days", 30))
    history = _history_series(canonical_id, days=days)
    if not history:
        return f"No history for canonical_id={canonical_id}", 404
    with db_manager.get_conn() as conn:
        latest = conn.execute(
            """SELECT product_name, product_id, url, market_price
                 FROM prices
                WHERE canonical_id = ?
                ORDER BY date DESC LIMIT 1""",
            (canonical_id,),
        ).fetchone()
    return render_template("product.html",
                           product_id=latest["product_id"],
                           canonical_id=canonical_id,
                           product_name=latest["product_name"],
                           market_price=latest["market_price"],
                           url=latest["url"],
                           history=history,
                           days=days)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@app.get("/api/products")
def api_products():
    return jsonify(_latest_for_each_product(days=30))


@app.get("/api/history/<canonical_id>")
def api_history(canonical_id: str):
    days = int(request.args.get("days", 30))
    return jsonify(_history_series(canonical_id, days=days))


@app.post("/api/scrape")
def api_scrape():
    force = bool(request.json and request.json.get("force"))
    with _job_lock:
        if _job["running"]:
            return jsonify({"ok": False, "message": "A scrape is already running."}), 409
        _job.update(
            running=True,
            started=datetime.now().isoformat(timespec="seconds"),
            finished=None,
            message="Starting…",
            inserted=0, found=0, error=None,
        )
    threading.Thread(target=_run_scrape_job, args=(force,), daemon=True).start()
    return jsonify({"ok": True, "message": "Scrape started."})


@app.get("/api/scrape/status")
def api_scrape_status():
    with _job_lock:
        return jsonify(dict(_job))


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser.")
    app.run(host="127.0.0.1", port=5000, debug=False)
