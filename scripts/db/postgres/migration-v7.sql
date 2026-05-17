-- Migration v7: Add api_usage_log table for per-endpoint, per-day API unit tracking
-- Also adds scheduler_enabled column to download_config

CREATE TABLE IF NOT EXISTS api_usage_log (
    id BIGSERIAL PRIMARY KEY,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    endpoint TEXT NOT NULL,
    airport_code VARCHAR(4),
    key_name VARCHAR(64),
    units_used INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER
);

ALTER TABLE download_config
  ADD COLUMN IF NOT EXISTS scheduler_enabled BOOLEAN DEFAULT FALSE;

UPDATE download_config SET scheduler_enabled = FALSE WHERE id = 1 AND scheduler_enabled IS NULL;
