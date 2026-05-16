-- Migration v5: Add is_admin to api_users, RapidAPI config columns to download_config
ALTER TABLE api_users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;

ALTER TABLE download_config
  ADD COLUMN IF NOT EXISTS rapidapi_daily_burn INTEGER DEFAULT 280,
  ADD COLUMN IF NOT EXISTS rapidapi_alert_days INTEGER DEFAULT 23,
  ADD COLUMN IF NOT EXISTS rapidapi_key_hash VARCHAR(64) DEFAULT '';
