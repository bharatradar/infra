-- Migration v4: Add RapidAPI unit usage tracking and Telegram alert columns
ALTER TABLE download_config
  ADD COLUMN IF NOT EXISTS rapidapi_units_used INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS rapidapi_units_limit INTEGER DEFAULT 600,
  ADD COLUMN IF NOT EXISTS rapidapi_unit_cost INTEGER DEFAULT 2,
  ADD COLUMN IF NOT EXISTS rapidapi_last_alert_at TIMESTAMP;
