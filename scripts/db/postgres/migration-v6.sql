-- Migration v6: Add rapidapi_keys JSONB column for multi-key management
ALTER TABLE download_config
  ADD COLUMN IF NOT EXISTS rapidapi_keys JSONB DEFAULT '[]'::jsonb;

-- Migrate existing single key hash into the new JSONB array if not already there
UPDATE download_config
SET rapidapi_keys = (
  SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
  FROM (
    SELECT jsonb_build_object(
      'hash', rapidapi_key_hash,
      'tier', 'pro',
      'active', true
    ) AS elem
    WHERE rapidapi_key_hash != ''
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(rapidapi_keys) e
        WHERE e->>'hash' = rapidapi_key_hash
      )
  ) sub
)
WHERE id = 1;
