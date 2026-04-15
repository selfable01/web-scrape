-- schema.sql — PostgreSQL schema for momo price tracker
-- Run once against your Vercel Postgres database.

CREATE TABLE IF NOT EXISTS momo_prices (
    id              SERIAL PRIMARY KEY,
    product_name    TEXT        NOT NULL,
    original_price  INTEGER,                    -- market / list price (strikethrough)
    discount_price  INTEGER     NOT NULL,       -- current sale price
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    unique_key      TEXT        NOT NULL        -- hash of (product_name + original_price)
);

-- Prevent duplicate product on the same calendar day
CREATE UNIQUE INDEX IF NOT EXISTS uix_momo_prices_key_day
    ON momo_prices (unique_key, (timestamp::date));

-- Fast lookups by product
CREATE INDEX IF NOT EXISTS idx_momo_prices_key
    ON momo_prices (unique_key);

-- Fast date-range queries
CREATE INDEX IF NOT EXISTS idx_momo_prices_ts
    ON momo_prices (timestamp);
