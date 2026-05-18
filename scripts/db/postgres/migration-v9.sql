-- migration-v9.sql
-- Clean up garbage flight_number values that don't match IATA format
-- Root cause: _resolve_flight_number airlines.csv prefix swap produced
-- invalid IATA numbers like "VTJS" (from VTA→VT map for callsign VTAJS)

DELETE FROM flight_schedules WHERE id IN (
    268763, 293501,   -- N
    258079, 258647, 269406, 272728,  -- VTHI
    256393, 257519, 258639  -- VTJS
);

-- Verify cleanup
-- SELECT flight_number, count(*) FROM flight_schedules
-- WHERE flight_number IN ('VTJS', 'VTHI', 'N')
-- GROUP BY flight_number;
