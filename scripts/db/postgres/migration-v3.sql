-- BharatRadar Schema Migration v3
-- Adds AeroDataBox enrichment columns to flight_schedules
-- Safe to run on existing databases (IF NOT EXISTS)

ALTER TABLE flight_schedules
  ADD COLUMN IF NOT EXISTS status VARCHAR(20),
  ADD COLUMN IF NOT EXISTS estimated_time TIMESTAMP,
  ADD COLUMN IF NOT EXISTS terminal VARCHAR(20),
  ADD COLUMN IF NOT EXISTS gate VARCHAR(10),
  ADD COLUMN IF NOT EXISTS runway VARCHAR(10),
  ADD COLUMN IF NOT EXISTS airline_iata VARCHAR(3),
  ADD COLUMN IF NOT EXISTS airline_icao VARCHAR(3),
  ADD COLUMN IF NOT EXISTS airline_name VARCHAR(100),
  ADD COLUMN IF NOT EXISTS aircraft_reg VARCHAR(20),
  ADD COLUMN IF NOT EXISTS aircraft_model VARCHAR(50),
  ADD COLUMN IF NOT EXISTS is_cargo BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_codeshare BOOLEAN DEFAULT FALSE;
