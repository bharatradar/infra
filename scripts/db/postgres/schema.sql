-- BharatRadar PostgreSQL Schema
-- Creates all tables for flight tracking platform

-- ============================================================================
-- Core Tables
-- ============================================================================

-- Airports table
CREATE TABLE IF NOT EXISTS airports (
    icao VARCHAR(10) PRIMARY KEY,
    iata VARCHAR(10),
    name VARCHAR(255) NOT NULL,
    city VARCHAR(100),
    state VARCHAR(100),
    region VARCHAR(50),
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    elev INT,
    type VARCHAR(50),
    timezone VARCHAR(50),
    hub_for TEXT[],
    metro_connected BOOLEAN DEFAULT FALSE,
    mag_var_w DOUBLE PRECISION DEFAULT 0.0,
    download_schedules BOOLEAN DEFAULT FALSE
);

-- Runways table
CREATE TABLE IF NOT EXISTS runways (
    id SERIAL PRIMARY KEY,
    airport_icao VARCHAR(10) REFERENCES airports(icao),
    runway_id VARCHAR(20),
    name1 VARCHAR(10),
    hdg1 DOUBLE PRECISION,
    lat1 DOUBLE PRECISION,
    lon1 DOUBLE PRECISION,
    name2 VARCHAR(10),
    hdg2 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION,
    lon2 DOUBLE PRECISION,
    width_m DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_runways_airport ON runways(airport_icao);

-- ============================================================================
-- Flight Tracking Tables
-- ============================================================================

-- Live flights in air
CREATE TABLE IF NOT EXISTS flights_in_air (
    id SERIAL PRIMARY KEY,
    hexid VARCHAR(20) NOT NULL,
    callsign VARCHAR(20),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    alt INT DEFAULT 0,
    speed INT DEFAULT 0,
    heading DOUBLE PRECISION DEFAULT 0,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    origin_icao VARCHAR(10),
    origin_iata VARCHAR(5),
    origin_lat DOUBLE PRECISION,
    origin_lon DOUBLE PRECISION,
    dest_icao VARCHAR(10),
    dest_iata VARCHAR(5),
    dest_lat DOUBLE PRECISION,
    dest_lon DOUBLE PRECISION,
    callsign_iata VARCHAR(5),
    UNIQUE(hexid)
);
CREATE INDEX IF NOT EXISTS idx_flights_in_air_hex ON flights_in_air(hexid);
CREATE INDEX IF NOT EXISTS idx_flights_in_air_lastseen ON flights_in_air(last_seen DESC);

-- Arrivals log
CREATE TABLE IF NOT EXISTS arrivals_log (
    id SERIAL PRIMARY KEY,
    hex_id VARCHAR(20),
    callsign VARCHAR(20),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    origin VARCHAR(10),
    airport VARCHAR(10),
    runway VARCHAR(10),
    anomaly_flag VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_airport ON arrivals_log(airport);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_timestamp ON arrivals_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_hex ON arrivals_log(hex_id);

-- Departures log
CREATE TABLE IF NOT EXISTS departures_log (
    id SERIAL PRIMARY KEY,
    hex_id VARCHAR(20),
    callsign VARCHAR(20),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    destination VARCHAR(10),
    airport VARCHAR(10),
    runway VARCHAR(10),
    anomaly_flag VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_departures_log_airport ON departures_log(airport);
CREATE INDEX IF NOT EXISTS idx_departures_log_timestamp ON departures_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_departures_log_hex ON departures_log(hex_id);

-- Flight events
CREATE TABLE IF NOT EXISTS flight_events (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hex_id VARCHAR(20),
    callsign VARCHAR(20),
    event_type VARCHAR(50),
    details TEXT,
    airport VARCHAR(10),
    origin VARCHAR(10),
    destination VARCHAR(10),
    runway VARCHAR(10),
    anomaly_flag VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_flight_events_timestamp ON flight_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_flight_events_hex ON flight_events(hex_id);
CREATE INDEX IF NOT EXISTS idx_flight_events_type ON flight_events(event_type);
CREATE INDEX IF NOT EXISTS idx_flight_events_airport ON flight_events(airport);

-- Ground operations
CREATE TABLE IF NOT EXISTS ground_ops (
    hex_id VARCHAR(10) PRIMARY KEY,
    current_callsign VARCHAR(10),
    inbound_callsign VARCHAR(10),
    airport VARCHAR(10),
    origin VARCHAR(10),
    landed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ground_ops_airport ON ground_ops(airport);

-- Flight schedules
CREATE TABLE IF NOT EXISTS flight_schedules (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4),
    direction VARCHAR(10) CHECK (direction IN ('ARRIVALS', 'DEPARTURES')),
    hex_id VARCHAR(6),
    flight_number VARCHAR(10),
    callsign VARCHAR(10),
    changed_callsign VARCHAR(10),
    route_airport VARCHAR(4),
    scheduled_time TIMESTAMP,
    actual_time TIMESTAMP,
    anomaly_flag VARCHAR(50),
    status VARCHAR(20),
    estimated_time TIMESTAMP,
    terminal VARCHAR(20),
    gate VARCHAR(10),
    runway VARCHAR(10),
    airline_iata VARCHAR(3),
    airline_icao VARCHAR(3),
    airline_name VARCHAR(100),
    aircraft_reg VARCHAR(20),
    aircraft_model VARCHAR(50),
    is_cargo BOOLEAN DEFAULT FALSE,
    is_codeshare BOOLEAN DEFAULT FALSE,
    created_from VARCHAR(50),
    updated_from VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_flight_schedules_unique
ON flight_schedules (airport_code, direction, flight_number, route_airport, COALESCE(scheduled_time, 'epoch'::timestamp));
CREATE INDEX IF NOT EXISTS idx_flight_schedules_time ON flight_schedules(scheduled_time);
CREATE INDEX IF NOT EXISTS idx_flight_schedules_airport_dir ON flight_schedules(airport_code, direction);
CREATE INDEX IF NOT EXISTS idx_flight_schedules_callsign ON flight_schedules(callsign);

-- ============================================================================
-- User Management Tables
-- ============================================================================

-- API Users
CREATE TABLE IF NOT EXISTS api_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    callsign VARCHAR(20),
    google_id VARCHAR(255),
    tier VARCHAR(20) DEFAULT 'free',
    contributor_status VARCHAR(20) DEFAULT 'STANDARD',
    contributor_since TIMESTAMP,
    contributor_changed_at TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (email)
);
CREATE INDEX IF NOT EXISTS idx_api_users_email ON api_users(email);
CREATE INDEX IF NOT EXISTS idx_api_users_contributor ON api_users(contributor_status);

-- API Keys
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES api_users(id) ON DELETE CASCADE,
    api_key VARCHAR(100) UNIQUE NOT NULL,
    description VARCHAR(255),
    tier VARCHAR(20) DEFAULT 'free',
    daily_limit INTEGER DEFAULT 100,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(api_key);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- ============================================================================
-- Community Feeder Tables
-- ============================================================================

-- Feeders
CREATE TABLE IF NOT EXISTS feeders (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(255),
    user_email VARCHAR(255),
    station_uuid VARCHAR(100) UNIQUE,
    location VARCHAR(100),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    altitude_m INT DEFAULT 0,
    antenna_type VARCHAR(50),
    receiver_type VARCHAR(50),
    status VARCHAR(20) DEFAULT 'PENDING',
    tier VARCHAR(20) DEFAULT 'BRONZE',
    verified BOOLEAN DEFAULT FALSE,
    last_seen_at TIMESTAMP,
    total_active_hours INT DEFAULT 0,
    notify_after_hours INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feeders_station_uuid ON feeders(station_uuid);
CREATE INDEX IF NOT EXISTS idx_feeders_status ON feeders(status);
CREATE INDEX IF NOT EXISTS idx_feeders_tier ON feeders(tier);
CREATE INDEX IF NOT EXISTS idx_feeders_user_email ON feeders(user_email);
CREATE INDEX IF NOT EXISTS idx_feeders_last_seen ON feeders(last_seen_at DESC);

-- Add foreign key from feeders to api_users if api_users exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_users') THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_feeders_user_email'
            AND conrelid = 'feeders'::regclass
        ) THEN
            ALTER TABLE feeders
                ADD CONSTRAINT fk_feeders_user_email
                FOREIGN KEY (user_email) REFERENCES api_users(email)
                ON DELETE SET NULL;
        END IF;
    END IF;
END $$;

-- Feeder daily stats
CREATE TABLE IF NOT EXISTS feeder_daily_stats (
    id SERIAL PRIMARY KEY,
    feeder_id INTEGER REFERENCES feeders(id) ON DELETE CASCADE,
    stat_date DATE NOT NULL,
    messages_count BIGINT DEFAULT 0,
    aircraft_count INT DEFAULT 0,
    positions_count INT DEFAULT 0,
    max_range_km INT DEFAULT 0,
    avg_range_km INT DEFAULT 0,
    uptime_minutes INT DEFAULT 0,
    UNIQUE(feeder_id, stat_date)
);
CREATE INDEX IF NOT EXISTS idx_feeder_daily_stats_date ON feeder_daily_stats(stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_feeder_daily_stats_feeder ON feeder_daily_stats(feeder_id);

-- Feeder achievements
CREATE TABLE IF NOT EXISTS feeder_achievements (
    id SERIAL PRIMARY KEY,
    feeder_id INTEGER REFERENCES feeders(id) ON DELETE CASCADE,
    achievement_type VARCHAR(50) NOT NULL,
    achievement_name VARCHAR(100),
    description TEXT,
    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feeder_achievements_feeder ON feeder_achievements(feeder_id);

-- Coverage gaps
CREATE TABLE IF NOT EXISTS coverage_gaps (
    id SERIAL PRIMARY KEY,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    radius_km INT DEFAULT 50,
    region VARCHAR(100),
    priority VARCHAR(20) DEFAULT 'MEDIUM',
    notes TEXT,
    reported_by INTEGER REFERENCES feeders(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coverage_gaps_region ON coverage_gaps(region);

-- ============================================================================
-- Alert & Notification Tables
-- ============================================================================

-- User alerts
CREATE TABLE IF NOT EXISTS user_alerts (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    session_id VARCHAR(255),
    target_callsign VARCHAR(20),
    alert_type VARCHAR(50),
    threshold_mins INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'ACTIVE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Web push subscriptions
CREATE TABLE IF NOT EXISTS web_subscriptions (
    session_id VARCHAR(255) PRIMARY KEY,
    sub_data JSONB,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- AI Enrichment Tables
-- ============================================================================

-- AI enrichment audit
CREATE TABLE IF NOT EXISTS ai_enrichment_audit (
    id SERIAL PRIMARY KEY,
    target_table VARCHAR(50),
    record_id INT,
    hex_id VARCHAR(20),
    callsign VARCHAR(20),
    original_value VARCHAR(100),
    ai_inferred_value VARCHAR(100),
    ai_reasoning TEXT,
    confidence_score DECIMAL(3,2),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_enrichment_timestamp ON ai_enrichment_audit(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ai_enrichment_hex ON ai_enrichment_audit(hex_id);

-- AI insights log
CREATE TABLE IF NOT EXISTS ai_insights_log (
    id SERIAL PRIMARY KEY,
    insight_type VARCHAR(50),
    trigger_event VARCHAR(100),
    insight_text TEXT,
    target_airport VARCHAR(10),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_insights_timestamp ON ai_insights_log(timestamp DESC);

-- ============================================================================
-- Performance Indexes
-- ============================================================================

-- Add performance indexes
CREATE INDEX IF NOT EXISTS idx_flight_events_timestamp ON flight_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_flight_events_hex_ts ON flight_events(hex_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_flight_events_airport ON flight_events(airport);
CREATE INDEX IF NOT EXISTS idx_flight_events_type_time ON flight_events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_flight_events_anomaly ON flight_events(anomaly_flag);
CREATE INDEX IF NOT EXISTS idx_flight_schedules_time ON flight_schedules(scheduled_time);
CREATE INDEX IF NOT EXISTS idx_flight_schedules_airport_dir ON flight_schedules(airport_code, direction);
CREATE INDEX IF NOT EXISTS idx_flight_schedules_callsign ON flight_schedules(callsign);
CREATE INDEX IF NOT EXISTS idx_flights_in_air_lastseen ON flights_in_air(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_timestamp ON arrivals_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_airport ON arrivals_log(airport);
CREATE INDEX IF NOT EXISTS idx_arrivals_log_hex ON arrivals_log(hex_id);
CREATE INDEX IF NOT EXISTS idx_departures_log_timestamp ON departures_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_departures_log_airport ON departures_log(airport);
CREATE INDEX IF NOT EXISTS idx_ground_ops_aircraft ON ground_ops(hex_id);
CREATE INDEX IF NOT EXISTS idx_feeders_tier ON feeders(tier);
CREATE INDEX IF NOT EXISTS idx_feeders_status ON feeders(status);
CREATE INDEX IF NOT EXISTS idx_feeders_station_uuid ON feeders(station_uuid);
CREATE INDEX IF NOT EXISTS idx_feeders_user_email ON feeders(user_email);
CREATE INDEX IF NOT EXISTS idx_feeders_last_seen ON feeders(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_users_contributor ON api_users(contributor_status);
CREATE INDEX IF NOT EXISTS idx_feeder_daily_stats_date ON feeder_daily_stats(stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_feeder_daily_stats_feeder ON feeder_daily_stats(feeder_id);
CREATE INDEX IF NOT EXISTS idx_coverage_gaps_region ON coverage_gaps(region);

-- ============================================================================
-- Schedule Downloader Configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS download_config (
    id SERIAL PRIMARY KEY,
    schedule_time TIME NOT NULL DEFAULT '22:00:00',
    scheduler_enabled BOOLEAN DEFAULT FALSE,
    enabled BOOLEAN DEFAULT TRUE,
    last_run TIMESTAMP,
    last_status TEXT,
    next_run TIMESTAMP,
    rapidapi_units_used INTEGER DEFAULT 0,
    rapidapi_units_limit INTEGER DEFAULT 600,
    rapidapi_unit_cost INTEGER DEFAULT 2,
    rapidapi_daily_burn INTEGER DEFAULT 280,
    rapidapi_alert_days INTEGER DEFAULT 23,
    rapidapi_key_hash VARCHAR(64) DEFAULT '',
    rapidapi_last_alert_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO download_config (schedule_time, scheduler_enabled, enabled)
SELECT '22:00:00', FALSE, TRUE
WHERE NOT EXISTS (SELECT 1 FROM download_config);

-- migration: add next_run column if missing (safe for existing DBs)
ALTER TABLE download_config ADD COLUMN IF NOT EXISTS next_run TIMESTAMP;

-- ============================================================================
-- Missing Tables from db_reset.py
-- ============================================================================

-- Airport weather (METAR data)
CREATE TABLE IF NOT EXISTS airport_weather (
    airport_icao VARCHAR(4) PRIMARY KEY REFERENCES airports(icao),
    metar_raw TEXT,
    wind_dir INT,
    wind_speed INT,
    visibility DOUBLE PRECISION,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Route cache for flight lookups
CREATE TABLE IF NOT EXISTS route_cache (
    callsign VARCHAR(10) PRIMARY KEY,
    origin_icao VARCHAR(4),
    destination_icao VARCHAR(4),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Aircraft info (registration, type lookup by hex_id)
CREATE TABLE IF NOT EXISTS aircraft_info (
    hex_id VARCHAR(10) PRIMARY KEY,
    registration VARCHAR(20),
    type VARCHAR(20),
    airline_icao VARCHAR(5),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Flight schedules history (audit log)
CREATE TABLE IF NOT EXISTS flight_schedules_history (
    history_id SERIAL PRIMARY KEY,
    schedule_id INT,
    airport_code VARCHAR(4),
    direction VARCHAR(10),
    hex_id VARCHAR(6),
    flight_number VARCHAR(10),
    callsign VARCHAR(10),
    changed_callsign VARCHAR(10),
    route_airport VARCHAR(4),
    scheduled_time TIMESTAMP,
    actual_time TIMESTAMP,
    anomaly_flag VARCHAR(50),
    original_id INTEGER,
    created_from VARCHAR(50),
    updated_from VARCHAR(50),
    changed_at TIMESTAMP DEFAULT NOW(),
    operation VARCHAR(10)
);

-- Trigger function to log changes to flight_schedules
CREATE OR REPLACE FUNCTION log_flight_schedule_changes()
RETURNS TRIGGER AS $$
BEGIN
    IF (TG_OP = 'UPDATE') THEN
        IF (OLD.airport_code IS DISTINCT FROM NEW.airport_code) OR
           (OLD.hex_id IS DISTINCT FROM NEW.hex_id) OR
           (OLD.flight_number IS DISTINCT FROM NEW.flight_number) OR
           (OLD.callsign IS DISTINCT FROM NEW.callsign) OR
           (OLD.route_airport IS DISTINCT FROM NEW.route_airport) OR
           (OLD.updated_from IS DISTINCT FROM NEW.updated_from) THEN

            INSERT INTO flight_schedules_history (
                original_id, operation, airport_code, direction, hex_id,
                flight_number, callsign, changed_callsign, route_airport,
                scheduled_time, actual_time, anomaly_flag,
                created_from, updated_from
            ) VALUES (
                OLD.id, 'UPDATE', OLD.airport_code, OLD.direction, OLD.hex_id,
                OLD.flight_number, OLD.callsign, OLD.changed_callsign, OLD.route_airport,
                OLD.scheduled_time, OLD.actual_time, OLD.anomaly_flag,
                OLD.created_from, OLD.updated_from
            );
        END IF;
        RETURN NEW;
    ELSIF (TG_OP = 'DELETE') THEN
        INSERT INTO flight_schedules_history (
            original_id, operation, airport_code, direction, hex_id,
            flight_number, callsign, changed_callsign, route_airport,
            scheduled_time, actual_time, anomaly_flag,
            created_from, updated_from
        ) VALUES (
            OLD.id, 'DELETE', OLD.airport_code, OLD.direction, OLD.hex_id,
            OLD.flight_number, OLD.callsign, OLD.changed_callsign, OLD.route_airport,
            OLD.scheduled_time, OLD.actual_time, OLD.anomaly_flag,
            OLD.created_from, OLD.updated_from
        );
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Create trigger on flight_schedules
DROP TRIGGER IF EXISTS flight_schedules_audit_trigger ON flight_schedules;
CREATE TRIGGER flight_schedules_audit_trigger
AFTER UPDATE OR DELETE ON flight_schedules
FOR EACH ROW EXECUTE FUNCTION log_flight_schedule_changes();