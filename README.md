# 輝葉 (HUEI YEH) momo Price Tracker — Web Dashboard

A complete, runnable local web app that scrapes daily prices for HUEI YEH
products in the **按摩用品** (Massage Supplies) category on momoshop.com.tw,
stores them in SQLite, and visualizes them in your browser.

## What's inside

```
momo_web/
├── app.py              # Flask web server (dashboard + JSON API)
├── scraper.py          # Headless Playwright scraper
├── db_manager.py       # SQLite layer
├── chart_gen.py        # Standalone PNG chart generator (still works)
├── templates/
│   ├── index.html      # Dashboard with cards + sparklines
│   └── product.html    # Detail page with full SVG chart + table
├── requirements.txt
└── README.md
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

The dashboard shows every tracked product with its current price, percent
change vs 30 days ago, and a 7-day sparkline. Click any card to see the
full price history with chart, range selector (7/14/30/90 days), and
day-by-day table.

## Daily workflow

* The first time you load the page, the database will be empty. Click
  **Run scrape now** in the header — it kicks off a background job
  (status updates live, takes ~30 s for ~120 products across 4 pages).
* After that, run `python app.py` (or just leave it running) and click
  **Run scrape now** once a day. The button is no-op if today is already
  scraped; **Force re-scrape** overrides this.
* For unattended operation, schedule `python scraper.py` via cron/Task
  Scheduler — the web app reads from the same DB file (`momo_prices.db`).

## API (for your own scripts)

| Method | Path                          | Description                         |
| ------ | ----------------------------- | ----------------------------------- |
| GET    | `/api/products`               | All tracked products + 30-day delta |
| GET    | `/api/history/<product_id>`   | Price history (default 30 days; `?days=N`) |
| POST   | `/api/scrape`                 | Start a scrape (`{"force":true}`)   |
| GET    | `/api/scrape/status`          | Current scrape job status           |

## Notes

* momo's search results are now rendered by Next.js. The scraper extracts
  the embedded `__next_f.push([...])` JSON payload rather than scraping
  CSS selectors, which makes it survive layout changes.
* Pagination is auto-detected from the `maxPage` field in that payload.
* The visible chart is pure SVG rendered in the browser — no Chart.js
  dependency. Sparklines on the dashboard are also inline SVG.
* For Chinese product names to render correctly, use a system with a CJK
  font (macOS PingFang, Windows JhengHei, or `fonts-noto-cjk` on Linux).
