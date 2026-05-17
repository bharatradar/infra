"""Generate SQL to convert 3-letter IATA codes to 4-letter ICAO.

Handles unique constraint violations by deleting 3-letter dupes first.
Usage: python gen-icao-migration-sql.py < airports.csv | psql ...
"""
import csv
import sys

def main():
    reader = csv.DictReader(sys.stdin)
    mappings = []
    for row in reader:
        iata = row.get('IATA', '').strip().upper()
        icao = row.get('ICAO', '').strip().upper()
        if iata and icao and len(iata) == 3 and len(icao) == 4:
            mappings.append((iata, icao))
    
    print(f"-- IATA->ICAO migration: {len(mappings)} mappings", file=sys.stderr)
    print("BEGIN;")

    # Create temp table with mappings
    print("CREATE TEMP TABLE _iata_icao (iata TEXT PRIMARY KEY, icao TEXT);")
    batch = []
    for iata, icao in mappings:
        batch.append(f"('{iata}','{icao}')")
        if len(batch) >= 500:
            print(f"INSERT INTO _iata_icao VALUES {','.join(batch)} ON CONFLICT DO NOTHING;")
            batch = []
    if batch:
        print(f"INSERT INTO _iata_icao VALUES {','.join(batch)} ON CONFLICT DO NOTHING;")

    print("SELECT 'Loaded ' || count(*) || ' mappings' AS status FROM _iata_icao;")

    # flight_schedules.route_airport: delete rows where 4-letter already exists (unique conflict)
    print("DELETE FROM flight_schedules s")
    print("USING _iata_icao m")
    print("WHERE s.route_airport = m.iata AND length(s.route_airport) = 3")
    print("  AND EXISTS (")
    print("    SELECT 1 FROM flight_schedules s2")
    print("    WHERE s2.airport_code = s.airport_code")
    print("      AND s2.direction = s.direction")
    print("      AND s2.flight_number = s.flight_number")
    print("      AND s2.scheduled_time = s.scheduled_time")
    print("      AND s2.route_airport = m.icao")
    print("  );")

    # Update remaining 3-letter codes to 4-letter ICAO
    print("UPDATE flight_schedules s")
    print("SET route_airport = m.icao")
    print("FROM _iata_icao m")
    print("WHERE s.route_airport = m.iata AND length(s.route_airport) = 3;")

    # Other tables — no unique constraint issues expected here
    for table, column in [
        ("flight_schedules", "airport_code"),
        ("arrivals_log", "origin"),
        ("arrivals_log", "airport"),
        ("departures_log", "destination"),
        ("departures_log", "airport"),
        ("flight_events", "airport"),
        ("flight_events", "origin"),
        ("flight_events", "destination"),
        ("ground_ops", "airport"),
        ("ground_ops", "origin"),
    ]:
        print(f"UPDATE {table} t SET {column} = m.icao FROM _iata_icao m WHERE t.{column} = m.iata AND length(t.{column}) = 3;")

    print("DROP TABLE _iata_icao;")
    print("COMMIT;")

    # Verify remaining 3-letter codes
    for table, column in [
        ("flight_schedules", "route_airport"),
        ("flight_schedules", "airport_code"),
        ("arrivals_log", "origin"),
        ("arrivals_log", "airport"),
        ("departures_log", "destination"),
        ("departures_log", "airport"),
    ]:
        print(f"SELECT '{table}.{column}' AS table_col, count(*) AS remaining FROM {table} WHERE length({column}) = 3 AND {column} != '';")

if __name__ == "__main__":
    main()
