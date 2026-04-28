"""
app.py — Flask dashboard for the momo HUEI YEH price tracker.

Deployed on Vercel as a serverless function. Reads from Vercel Postgres
via the POSTGRES_URL environment variable.

Routes
------
GET  /                          Dashboard: top products + 7-day price trend chart
GET  /full-list                 Every record ever saved since Day 1 (sortable table)
GET  /product/<unique_key>      Detail page with full price history chart
GET  /register                  Registration form
POST /register                  Create account
GET  /login                     Login form
POST /login                     Authenticate
GET  /logout                    Log out
GET  /settings                  User scrape settings
POST /settings                  Update scrape settings
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone, time as dt_time

from flask import Flask, jsonify, redirect, render_template, request, url_for, flash
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# App + DB setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.json.ensure_ascii = False  # emit real CJK chars, not \uXXXX escapes
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

db_url = os.environ.get("POSTGRES_URL", "")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

TW_TZ = timezone(timedelta(hours=8))

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "請先登入。"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.Text, unique=True, nullable=False)
    email = db.Column(db.Text, unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)
    scrape_time = db.Column(db.Time, nullable=False, default=lambda: datetime.strptime("11:00", "%H:%M").time())
    history_days = db.Column(db.Integer, nullable=False, default=7)
    last_scrape_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(TW_TZ))

    prices = db.relationship("MomoPrice", backref="owner", lazy="dynamic")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class MomoPrice(db.Model):
    __tablename__ = "momo_prices"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    product_name = db.Column(db.Text, nullable=False)
    original_price = db.Column(db.Integer)
    discount_price = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(TW_TZ))
    unique_key = db.Column(db.Text, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tw_now():
    return datetime.now(TW_TZ)


def _user_prices(user_id: int):
    """Return a query scoped to the current user's prices."""
    return MomoPrice.query.filter(MomoPrice.user_id == user_id)


def _next_scrape_display(user) -> str:
    """Compute a human-readable string for the user's next scrape time."""
    now = _tw_now()
    today = now.date()
    scrape_hour = user.scrape_time.hour

    # Has the scraper already run today for this user?
    already_today = (
        user.last_scrape_at is not None
        and user.last_scrape_at.astimezone(TW_TZ).date() == today
    )

    if already_today:
        # Next scrape is tomorrow at user's scrape_time
        next_date = today + timedelta(days=1)
        return f"明��� {user.scrape_time.strftime('%H:%M')}（台北時間）"
    elif now.hour >= scrape_hour:
        # Due now — will run at next hourly check
        return "即將執行（下一個整點）"
    else:
        return f"今天 {user.scrape_time.strftime('%H:%M')}（台北時間）"


def _latest_for_each_product(user_id: int, days: int = 30) -> list[dict]:
    """One row per product: latest price + price from `days` ago for delta."""
    cutoff = _tw_now() - timedelta(days=days)

    rows = (
        _user_prices(user_id)
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


def _history_series(user_id: int, unique_key: str, days: int) -> list[dict]:
    """Price history for one product over `days` days."""
    cutoff = _tw_now() - timedelta(days=days)
    rows = (
        _user_prices(user_id)
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


def _has_today_data(user_id: int) -> bool:
    """Check if there's data for today (Asia/Taipei) for this user."""
    today = _tw_now().date()
    row = (
        _user_prices(user_id)
        .filter(db.func.date(MomoPrice.timestamp) >= str(today))
        .first()
    )
    return row is not None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("所有欄位皆為必填。", "error")
            return render_template("register.html", registered=False)

        if len(password) < 6:
            flash("密碼須至少 6 個字元。", "error")
            return render_template("register.html", registered=False)

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("使用者名稱或電子郵件已被使用。", "error")
            return render_template("register.html", registered=False)

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return render_template("register.html", registered=True)

    return render_template("register.html", registered=False)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))

        flash("使用者名稱或密碼錯誤。", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Settings route
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        scrape_time_str = request.form.get("scrape_time", "11:00").strip()
        history_days = request.form.get("history_days", "7").strip()

        try:
            parsed_time = datetime.strptime(scrape_time_str, "%H:%M").time()
        except ValueError:
            flash("時間格式錯誤，請使用 HH:MM（例如 14:00）。", "error")
            return render_template("settings.html", user=current_user,
                                   next_scrape_display=_next_scrape_display(current_user))

        try:
            parsed_days = int(history_days)
            if parsed_days < 1 or parsed_days > 365:
                raise ValueError
        except ValueError:
            flash("歷史天數須為 1 到 365 之間的數字。", "error")
            return render_template("settings.html", user=current_user,
                                   next_scrape_display=_next_scrape_display(current_user))

        # If scrape_time changed, reset last_scrape_at so the scheduler
        # treats this user as "not yet scraped today" — enabling a re-scrape
        # at the new time even if the old time already ran today.
        if current_user.scrape_time != parsed_time:
            current_user.last_scrape_at = None

        current_user.scrape_time = parsed_time
        current_user.history_days = parsed_days
        db.session.commit()
        flash("設定已儲存。", "success")

    return render_template("settings.html", user=current_user,
                           next_scrape_display=_next_scrape_display(current_user))


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    days = current_user.history_days
    products = _latest_for_each_product(current_user.id, days=days)
    for p in products:
        p["spark"] = _history_series(current_user.id, p["unique_key"], days=7)
    today = _tw_now().strftime("%Y-%m-%d")
    has_today = _has_today_data(current_user.id)
    return render_template("index.html",
                           products=products,
                           today=today,
                           has_today=has_today,
                           user=current_user,
                           next_scrape_display=_next_scrape_display(current_user))


@app.route("/full-list")
@login_required
def full_list():
    """Every single record ever saved in the database for this user."""
    rows = (
        _user_prices(current_user.id)
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
@login_required
def product_detail(unique_key: str):
    days = int(request.args.get("days", current_user.history_days))
    history = _history_series(current_user.id, unique_key, days=days)
    if not history:
        return f"找不到 unique_key={unique_key} 的歷史紀錄", 404
    latest = (
        _user_prices(current_user.id)
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
@login_required
def api_products():
    return jsonify(_latest_for_each_product(current_user.id, days=30))


@app.get("/api/history/<unique_key>")
@login_required
def api_history(unique_key: str):
    days = int(request.args.get("days", 30))
    return jsonify(_history_series(current_user.id, unique_key, days=days))


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser.")
    app.run(host="127.0.0.1", port=5000, debug=False)
