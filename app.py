"""
app.py — Flask dashboard for the momo HUEI YEH price tracker.

Deployed on Vercel as a serverless function. Reads from Vercel Postgres
via the POSTGRES_URL environment variable.

Routes
------
GET  /           Dashboard: top products + 7-day price trend chart
GET  /full-list  Every record ever saved since Day 1 (sortable table)
GET  /product/<unique_key>  Detail page with full price history chart
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

# ---------------------------------------------------------------------------
# App + DB setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.json.ensure_ascii = False  # emit real CJK chars, not \uXXXX escapes

db_url = os.environ.get("POSTGRES_URL", "")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

TW_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class MomoPrice(db.Model):
    __tablename__ = "momo_prices"

    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.Text, nullable=False)
    original_price = db.Column(db.Integer)
    discount_price = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(TW_TZ))
    unique_key = db.Column(db.Text, nullable=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tw_now():
    return datetime.now(TW_TZ)


def _latest_for_each_product(days: int = 30) -> list[dict]:
    """One row per product: latest price + price from `days` ago for delta."""
    cutoff = _tw_now() - timedelta(days=days)

    rows = (
        MomoPrice.query
        .filter(MomoPrice.timestamp >= cutoff)
        .order_by(MomoPrice.timestamp.asc())
        .all()
    )

    # Group by unique_key
    products: dict[str, list] = {}
    for r in rows:
        products.setdefault(r.unique_key, []).append(r)

    result = []
    for uk, recs in products.items():
        latest = recs[-1]
        first = recs[0]
        delta = latest.discount_price - first.discount_price
        delta_pct = (delta / first.discount_price * 100) if first.discount_price else 0

        result.append({
            "unique_key": uk,
            "product_name": latest.product_name,
            "discount_price": latest.discount_price,
            "original_price": latest.original_price,
            "latest_date": latest.timestamp.strftime("%Y-%m-%d"),
            "delta": delta,
            "delta_pct": delta_pct,
        })

    result.sort(key=lambda x: x["product_name"])
    return result


def _history_series(unique_key: str, days: int) -> list[dict]:
    """Price history for one product over `days` days."""
    cutoff = _tw_now() - timedelta(days=days)
    rows = (
        MomoPrice.query
        .filter(MomoPrice.unique_key == unique_key,
                MomoPrice.timestamp >= cutoff)
        .order_by(MomoPrice.timestamp.asc())
        .all()
    )
    return [
        {
            "date": r.timestamp.strftime("%Y-%m-%d"),
            "price": r.discount_price,
            "original_price": r.original_price,
        }
        for r in rows
    ]


def _has_today_data() -> bool:
    """Check if there's data for today (Asia/Taipei)."""
    today = _tw_now().date()
    row = (
        MomoPrice.query
        .filter(db.func.date(MomoPrice.timestamp) >= str(today))
        .first()
    )
    return row is not None


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    products = _latest_for_each_product(days=30)
    for p in products:
        p["spark"] = _history_series(p["unique_key"], days=7)
    today = _tw_now().strftime("%Y-%m-%d")
    has_today = _has_today_data()
    return render_template("index.html",
                           products=products,
                           today=today,
                           has_today=has_today)


@app.route("/full-list")
def full_list():
    """Every single record ever saved in the database."""
    rows = (
        MomoPrice.query
        .order_by(MomoPrice.timestamp.desc(), MomoPrice.product_name.asc())
        .all()
    )
    records = [
        {
            "date": r.timestamp.strftime("%Y-%m-%d"),
            "unique_key": r.unique_key,
            "product_name": r.product_name,
            "discount_price": r.discount_price,
            "original_price": r.original_price,
        }
        for r in rows
    ]
    return render_template("full_list.html", records=records)


@app.route("/product/<unique_key>")
def product_detail(unique_key: str):
    days = int(request.args.get("days", 30))
    history = _history_series(unique_key, days=days)
    if not history:
        return f"No history for unique_key={unique_key}", 404
    latest = (
        MomoPrice.query
        .filter(MomoPrice.unique_key == unique_key)
        .order_by(MomoPrice.timestamp.desc())
        .first()
    )
    return render_template("product.html",
                           unique_key=unique_key,
                           product_name=latest.product_name,
                           original_price=latest.original_price,
                           history=history,
                           days=days)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@app.get("/api/products")
def api_products():
    return jsonify(_latest_for_each_product(days=30))


@app.get("/api/history/<unique_key>")
def api_history(unique_key: str):
    days = int(request.args.get("days", 30))
    return jsonify(_history_series(unique_key, days=days))


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser.")
    app.run(host="127.0.0.1", port=5000, debug=False)
