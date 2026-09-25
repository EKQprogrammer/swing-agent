-- OHLCV data for any ticker fetched via yfinance, INCLUDING VIX (ticker='^VIX').
-- date is ISO-8601 'YYYY-MM-DD' TEXT (zero-padded) so that lexicographic
-- ordering/comparison matches chronological ordering, since SQLite has no
-- native DATE type.
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL NOT NULL,
    volume      INTEGER,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, date)
);

-- Generic long-format table for scalar FRED time series (DGS10 now; extensible
-- to other series later without a schema change).
CREATE TABLE IF NOT EXISTS macro_series (
    series_id   TEXT NOT NULL,
    date        TEXT NOT NULL,
    value       REAL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (series_id, date)
);

-- Phase 3 fundamentals cache, created now to avoid a future migration.
-- Unused until Phase 3.
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker              TEXT NOT NULL,
    filed_date          TEXT NOT NULL,
    roic                REAL,
    fcf                 REAL,
    fcf_margin          REAL,
    revenue_growth_yoy  REAL,
    earnings_growth_yoy REAL,
    relative_strength   REAL,
    price               REAL,
    avg_daily_volume    REAL,
    fetched_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, filed_date)
);

-- Historical earnings report dates (Tier 1 earnings-calendar filter), from
-- EODHD's fundamentals payload's Earnings.History[*].reportDate -- extracted
-- during the same fetch as fundamentals, no separate API call.
CREATE TABLE IF NOT EXISTS earnings_dates (
    ticker       TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, report_date)
);
