-- BharatRadar v2 Migration
-- Adds download_schedules column to airports table for existing databases.
-- Run this once against an existing database when upgrading from v1 schema.

ALTER TABLE airports ADD COLUMN IF NOT EXISTS download_schedules BOOLEAN DEFAULT FALSE;

-- Enable schedule downloads for the 35 target airports
UPDATE airports SET download_schedules = TRUE WHERE icao IN (
    'VABB', 'VIDP', 'VOBL', 'VOMM', 'VECC', 'VAPO', 'VAAH', 'VOHS',
    'VOCI', 'VOTV', 'VANP', 'VOGO', 'VOGA', 'VILK', 'VIJP', 'VICG',
    'VEPT', 'VEGT', 'VEBS', 'VABP', 'VAUD', 'VASU', 'VEBN', 'VIJO',
    'VARK', 'VEGK', 'VISR', 'VERC', 'VABO', 'VEBD', 'VIBL', 'VANR',
    'VIAR', 'VOCB', 'VOML'
);
