-- Migration v8: Fix flight_schedules duplicate rows when scheduled_time IS NULL
-- PostgreSQL UNIQUE constraints treat NULL != NULL, so rows with NULL scheduled_time
-- are never considered duplicate. Replace with a unique index using COALESCE.

-- 1. Find and drop the old UNIQUE constraint (auto-named, may be truncated to 63 chars)
DO $$
DECLARE
    con_name text;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'flight_schedules'::regclass
      AND contype = 'u'
      AND conkey = (
          SELECT array_agg(attnum ORDER BY attnum)
          FROM pg_attribute
          WHERE attrelid = 'flight_schedules'::regclass
            AND attname IN ('airport_code', 'direction', 'flight_number', 'route_airport', 'scheduled_time')
      );
    IF con_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE flight_schedules DROP CONSTRAINT ' || con_name;
        RAISE NOTICE 'Dropped constraint: %', con_name;
    END IF;
END $$;

-- 2. Delete existing duplicates BEFORE creating the unique index (keep oldest id)
DELETE FROM flight_schedules a
USING flight_schedules b
WHERE a.id > b.id
  AND a.airport_code = b.airport_code
  AND a.direction = b.direction
  AND a.flight_number = b.flight_number
  AND a.route_airport IS NOT DISTINCT FROM b.route_airport
  AND COALESCE(a.scheduled_time, 'epoch'::timestamp) IS NOT DISTINCT FROM COALESCE(b.scheduled_time, 'epoch'::timestamp);

-- 3. Create unique index that treats NULL scheduled_time as equal via COALESCE
CREATE UNIQUE INDEX IF NOT EXISTS idx_flight_schedules_unique
ON flight_schedules (airport_code, direction, flight_number, route_airport, COALESCE(scheduled_time, 'epoch'::timestamp));
