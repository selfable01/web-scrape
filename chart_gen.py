"""
chart_gen.py
------------
Generate a 7-day price-history line chart for one momo product.

Usage
-----
    python chart_gen.py <canonical_id>              # last 7 days, PNG output
    python chart_gen.py <canonical_id> --days 14
    python chart_gen.py --list                      # show tracked products
    python chart_gen.py <canonical_id> --out my.png
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager

import db_manager

# ---------------------------------------------------------------------------
# CJK font setup
# ---------------------------------------------------------------------------
_CJK_CANDIDATES = [
    "Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans TC", "Noto Sans SC",
    "PingFang TC", "PingFang SC", "Microsoft JhengHei", "Microsoft YaHei",
    "Heiti TC", "SimHei", "Arial Unicode MS",
]
_available = {f.name for f in font_manager.fontManager.ttflist}
for _f in _CJK_CANDIDATES:
    if _f in _available:
        matplotlib.rcParams["font.sans-serif"] = [_f, "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        break


def make_chart(canonical_id: str,
               days: int = 7,
               out_path: Path | None = None) -> Path:
    """Render the chart and return the file path it was saved to."""
    rows = db_manager.get_price_history(canonical_id, days=days)
    if not rows:
        raise SystemExit(
            f"No price history for canonical_id={canonical_id} "
            f"in the last {days} days."
        )

    dates  = [datetime.fromisoformat(r["date"]) for r in rows]
    prices = [r["price"] for r in rows]
    name   = rows[-1]["product_name"]

    pmin, pmax = min(prices), max(prices)
    latest     = prices[-1]
    delta      = latest - prices[0]
    pct        = (delta / prices[0] * 100) if prices[0] else 0.0

    fig, ax = plt.subplots(figsize=(10, 5.2))

    line_color = "#2e7d32" if delta < 0 else ("#c62828" if delta > 0 else "#455a64")

    ax.plot(dates, prices, marker="o", linewidth=2,
            color=line_color, markerfacecolor="white",
            markeredgewidth=2, markeredgecolor=line_color,
            zorder=3)

    ax.fill_between(dates, pmin, pmax, color=line_color, alpha=0.07, zorder=1)
    ax.axhline(pmin, color=line_color, alpha=0.25,
               linestyle="--", linewidth=0.8, zorder=2)
    ax.axhline(pmax, color=line_color, alpha=0.25,
               linestyle="--", linewidth=0.8, zorder=2)

    ax.annotate(
        f"NT${latest:,}",
        xy=(dates[-1], latest),
        xytext=(8, 8), textcoords="offset points",
        fontsize=11, fontweight="bold", color=line_color,
    )

    short_name = name if len(name) <= 38 else name[:37] + "…"
    arrow = "DOWN" if delta < 0 else ("UP" if delta > 0 else "FLAT")
    ax.set_title(
        f"{short_name}\n"
        f"{days}-day trend  {arrow} NT${delta:+,}  ({pct:+.1f}%)   "
        f"cid={canonical_id[:8]}",
        fontsize=12, loc="left",
    )

    ax.set_ylabel("Price (TWD)")
    ax.grid(True, linestyle=":", linewidth=0.6, alpha=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    fig.autofmt_xdate()

    span = max(pmax - pmin, 1)
    ax.set_ylim(pmin - span * 0.25 - 1, pmax + span * 0.25 + 1)

    fig.tight_layout()

    out_path = out_path or Path(f"price_{canonical_id[:8]}_{days}d.png")
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_tracked() -> None:
    products = db_manager.list_tracked_products()
    if not products:
        print("No products tracked yet. Run scraper.py first.")
        return
    print(f"{'canonical_id':>16}  {'days':>4}  {'first':>10}  "
          f"{'last':>10}  name")
    for p in products:
        print(f"{p['canonical_id']:>16}  {p['days_tracked']:>4}  "
              f"{p['first_seen']:>10}  {p['last_seen']:>10}  "
              f"{p['product_name'][:60]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("canonical_id", nargs="?",
                    help="canonical ID to chart")
    ap.add_argument("--days", type=int, default=7,
                    help="window length in days (default: 7)")
    ap.add_argument("--out",  type=Path, default=None,
                    help="output PNG path")
    ap.add_argument("--list", action="store_true",
                    help="list all tracked products and exit")
    args = ap.parse_args()

    if args.list:
        _print_tracked()
        return 0
    if not args.canonical_id:
        ap.error("canonical_id is required (or pass --list)")

    out = make_chart(args.canonical_id, days=args.days, out_path=args.out)
    print(f"Saved chart → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
