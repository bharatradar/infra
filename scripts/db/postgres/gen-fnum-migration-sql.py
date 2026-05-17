"""Generate SQL to normalize flight_numbers by removing spaces.

Handles unique constraint by deleting rows where normalized version exists.
Usage: python gen-fnum-migration-sql.py | psql ...
"""
import re
import sys

def normalize_number(number):
    return "".join(number.upper().split())

# Generate SQL to find all distinct flight numbers with spaces
print("BEGIN;")
print("CREATE TEMP TABLE _fnum_fix (old_fnum TEXT, new_fnum TEXT);")

# We'll use a DO block to process each row
print("""
DO $$
DECLARE
  rec RECORD;
  new_fnum TEXT;
  dup_count INT;
BEGIN
  FOR rec IN SELECT DISTINCT flight_number FROM flight_schedules WHERE flight_number ~ '\\s' LOOP
    new_fnum := upper(regexp_replace(rec.flight_number, '\s', '', 'g'));
    
    -- delete rows where normalized version exists (avoid unique violation)
    DELETE FROM flight_schedules s
    WHERE s.flight_number = rec.flight_number
      AND EXISTS (
        SELECT 1 FROM flight_schedules s2
        WHERE s2.airport_code = s.airport_code
          AND s2.direction = s.direction
          AND s2.flight_number = new_fnum
          AND s2.route_airport = s.route_airport
          AND s2.scheduled_time IS NOT DISTINCT FROM s.scheduled_time
      );
    GET DIAGNOSTICS dup_count = ROW_COUNT;
    
    -- update remaining rows to normalized flight_number
    UPDATE flight_schedules s
    SET flight_number = new_fnum
    WHERE s.flight_number = rec.flight_number;
    
    IF dup_count > 0 OR rec.flight_number != new_fnum THEN
      RAISE NOTICE '% -> % (deleted % dupes)', rec.flight_number, new_fnum, dup_count;
    END IF;
  END LOOP;
END;
$$;
""")

print("DROP TABLE _fnum_fix;")
print("COMMIT;")

print("""
SELECT 'remaining with spaces' AS status, count(*) FROM flight_schedules WHERE flight_number ~ '\\s';
""")

if __name__ == "__main__":
    main()
