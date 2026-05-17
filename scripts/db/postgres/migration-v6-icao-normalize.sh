#!/bin/bash
# One-time migration: convert 3-letter IATA codes to 4-letter ICAO
# Reads airports.csv and generates UPDATEs for all affected columns.
set -e

CSV="${1:-build/cortex-webapp/data/airports.csv}"
DB_HOST="${DB_HOST:-45.88.189.38}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-flight_db}"
DB_USER="${DB_USER:-flight_db_user}"
DB_PASSWORD="${DB_PASSWORD:-}"

if [ ! -f "$CSV" ]; then
  echo "Error: $CSV not found"
  exit 1
fi

if [ -z "$DB_PASSWORD" ]; then
  echo "Error: DB_PASSWORD not set"
  exit 1
fi

# Build VALUES clause for temp table
VALUES=""
while IFS=',' read -r icao name iata_iata iata rest; do
  # airports.csv format: ICAO,Name,ICAO(dup),IATA,City,Country,lat,lon,elev
  # fields: 0=ICAO, 1=Name, 2=ICAO(dup), 3=IATA, 4=City, 5=Country, ...
  iata_code=$(echo "$iata" | tr -d '"' | tr '[:lower:]' '[:upper:]' | xargs)
  icao_code=$(echo "$icao" | tr -d '"' | tr '[:lower:]' '[:upper:]' | xargs)
  if [ -n "$iata_code" ] && [ -n "$icao_code" ] && [ ${#iata_code} -eq 3 ] && [ ${#icao_code} -eq 4 ]; then
    if [ -n "$VALUES" ]; then VALUES="$VALUES,"; fi
    VALUES="$VALUES ('$iata_code','$icao_code')"
  fi
done < <(tail -n +2 "$CSV")

echo "Generated $(echo "$VALUES" | grep -o "'[A-Z][A-Z][A-Z]'" | wc -l) IATA->ICAO mappings"

if [ -z "$VALUES" ]; then
  echo "No mappings found"
  exit 1
fi

PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" <<SQL
-- Create temp mapping table
CREATE TEMP TABLE _iata_icao (iata TEXT PRIMARY KEY, icao TEXT);
INSERT INTO _iata_icao (iata, icao) VALUES $VALUES;

-- Update flight_schedules.route_airport
UPDATE flight_schedules s
SET route_airport = m.icao
FROM _iata_icao m
WHERE s.route_airport = m.iata AND length(s.route_airport) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'flight_schedules.route_airport: % rows updated', v;

-- Update flight_schedules.airport_code
UPDATE flight_schedules s
SET airport_code = m.icao
FROM _iata_icao m
WHERE s.airport_code = m.iata AND length(s.airport_code) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'flight_schedules.airport_code: % rows updated', v;

-- Update arrivals_log.origin
UPDATE arrivals_log a
SET origin = m.icao
FROM _iata_icao m
WHERE a.origin = m.iata AND length(a.origin) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'arrivals_log.origin: % rows updated', v;

-- Update arrivals_log.airport
UPDATE arrivals_log a
SET airport = m.icao
FROM _iata_icao m
WHERE a.airport = m.iata AND length(a.airport) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'arrivals_log.airport: % rows updated', v;

-- Update departures_log.destination
UPDATE departures_log d
SET destination = m.icao
FROM _iata_icao m
WHERE d.destination = m.iata AND length(d.destination) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'departures_log.destination: % rows updated', v;

-- Update departures_log.airport
UPDATE departures_log d
SET airport = m.icao
FROM _iata_icao m
WHERE d.airport = m.iata AND length(d.airport) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'departures_log.airport: % rows updated', v;

-- Update flight_events
UPDATE flight_events e
SET airport = m.icao
FROM _iata_icao m
WHERE e.airport = m.iata AND length(e.airport) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'flight_events.airport: % rows updated', v;

UPDATE flight_events e
SET origin = m.icao
FROM _iata_icao m
WHERE e.origin = m.iata AND length(e.origin) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'flight_events.origin: % rows updated', v;

UPDATE flight_events e
SET destination = m.icao
FROM _iata_icao m
WHERE e.destination = m.iata AND length(e.destination) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'flight_events.destination: % rows updated', v;

-- Update ground_ops
UPDATE ground_ops g
SET airport = m.icao
FROM _iata_icao m
WHERE g.airport = m.iata AND length(g.airport) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'ground_ops.airport: % rows updated', v;

UPDATE ground_ops g
SET origin = m.icao
FROM _iata_icao m
WHERE g.origin = m.iata AND length(g.origin) = 3;
GET DIAGNOSTICS v = ROW_COUNT;
RAISE NOTICE 'ground_ops.origin: % rows updated', v;

DROP TABLE _iata_icao;

-- Verify: count remaining 3-letter codes
SELECT 'REMAINING 3-LETTER CODES' AS check_point,
  (SELECT count(*) FROM flight_schedules WHERE length(route_airport) = 3 AND route_airport != '') AS fs_route,
  (SELECT count(*) FROM flight_schedules WHERE length(airport_code) = 3 AND airport_code != '') AS fs_code,
  (SELECT count(*) FROM arrivals_log WHERE length(origin) = 3 AND origin != '') AS al_origin,
  (SELECT count(*) FROM arrivals_log WHERE length(airport) = 3 AND airport != '') AS al_airport,
  (SELECT count(*) FROM departures_log WHERE length(destination) = 3 AND destination != '') AS dl_dest,
  (SELECT count(*) FROM departures_log WHERE length(airport) = 3 AND airport != '') AS dl_airport;
SQL
