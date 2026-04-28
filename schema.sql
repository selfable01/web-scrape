-- schema.sql — PostgreSQL schema for momo price tracker
-- Run once against your Vercel Postgres database.

-- ---------------------------------------------------------------
-- Users table (auth + per-user scrape settings)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        TEXT        NOT NULL UNIQUE,
    email           TEXT        NOT NULL UNIQUE,
    password_hash   TEXT        NOT NULL,
    scrape_time     TIME        NOT NULL DEFAULT '11:00',   -- daily scrape time (Asia/Taipei)
    history_days    INTEGER     NOT NULL DEFAULT 7,          -- how many days of history to fetch/view
    last_scrape_at  TIMESTAMPTZ,                             -- when the scraper last ran for this user
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------
-- Prices table (now linked to a user)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS momo_prices (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER     REFERENCES users(id) ON DELETE CASCADE,
    product_name    TEXT        NOT NULL,
    original_price  INTEGER,                    -- market / list price (strikethrough)
    discount_price  INTEGER     NOT NULL,       -- current sale price
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    unique_key      TEXT        NOT NULL        -- hash of (product_name + original_price)
);

-- Prevent duplicate product on the same calendar day per user
CREATE UNIQUE INDEX IF NOT EXISTS uix_momo_prices_user_key_day
    ON momo_prices (user_id, unique_key, (timestamp::date));

-- Fast lookups by product
CREATE INDEX IF NOT EXISTS idx_momo_prices_key
    ON momo_prices (unique_key);

-- Fast date-range queries
CREATE INDEX IF NOT EXISTS idx_momo_prices_ts
    ON momo_prices (timestamp);

-- Fast lookups by user
CREATE INDEX IF NOT EXISTS idx_momo_prices_user
    ON momo_prices (user_id);
