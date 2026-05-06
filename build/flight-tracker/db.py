# db.py
import time
import logging
import asyncpg
import re
import os
import csv
from datetime import datetime, timedelta
from config import Config
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client import Point

logger = logging.getLogger(__name__)

class AsyncDatabaseManager:
    def __init__(self, pool):
        self.pool = pool
        self._iata_to_icao_map = None 
        self._icao_to_iata_airline_map = None 
        
        if getattr(Config, 'INFLUXDB_TOKEN', None):
            self.influx_client = InfluxDBClientAsync(
                url=Config.INFLUXDB_URL,
                token=Config.INFLUXDB_TOKEN,
                org=Config.INFLUXDB_ORG
            )
            self.influx_write_api = self.influx_client.write_api()
        else:
            self.influx_client = None

    async def _resolve_icao(self, code):
        """Automatically converts a 3-letter IATA airport code to a 4-letter ICAO code."""
        if not code: 
            return code
            
        code = str(code).strip().upper()
 
        if code == 'UNK':
            return None  # Return None instead of 'UNK' to avoid mapping to PAUN
 
        if len(code) == 3:
            # Check config first
            for icao, data in getattr(Config, 'TARGET_AIRPORTS', {}).items():
                if data.get('iata', '').upper() == code:
                    return icao
                     
            # Initialize cache if needed
            if self._iata_to_icao_map is None:
                self._iata_to_icao_map = await self._load_iata_to_icao_map()
                     
            return self._iata_to_icao_map.get(code, code)
 
        return code

    async def _load_iata_to_icao_map(self):
        """Load IATA to ICAO mapping from database with caching."""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT iata, icao FROM airports WHERE iata IS NOT NULL")
                return {r['iata'].upper(): r['icao'].upper() for r in rows}
        except Exception as e:
            logger.error(f"Failed to load IATA-ICAO map: {e}")
            return {}

    async def _resolve_flight_number(self, code):
        """Converts an ICAO flight number (e.g. AIC0123) to an IATA flight number (e.g. AI123)."""
        if not code: 
            return code
            
        code = str(code).strip().upper()        
        if self._icao_to_iata_airline_map is None:
            self._icao_to_iata_airline_map = await self._load_icao_to_iata_airline_map()
                 
        # 🌟 FIX: Allow Alphanumeric ATC callsigns (like AKJ988M) to be parsed
        match = re.match(r"^([A-Z]{3})0*([A-Z0-9]+)$", code)
        if match:
            prefix = match.group(1)
            suffix = match.group(2)
            if prefix in self._icao_to_iata_airline_map:
                return f"{self._icao_to_iata_airline_map[prefix]}{suffix}"
                 
        match_iata = re.match(r"^([A-Z0-9]{2})0*([A-Z0-9]+)$", code)
        if match_iata:
            return f"{match_iata.group(1)}{match_iata.group(2)}"
             
        return code

    async def _load_icao_to_iata_airline_map(self):
        """Load ICAO to IATA airline mapping from file with caching."""
        try:
            if os.path.exists(Config.AIRLINES_FILE):
                with open(Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                    return {row.get('ICAO', '').strip().upper(): row.get('IATA', '').strip().upper() 
                            for row in csv.DictReader(f) 
                            if row.get('ICAO') and row.get('IATA') and 
                               row.get('ICAO') not in ('\\N', 'N/A', '-')}
        except Exception as e:
            logger.error(f"Failed to load ICAO-IATA airline map: {e}")
            return {}
                
    async def reset_system_state(self, redis_client=None):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM flights_in_air")
            if redis_client:
                await redis_client.flushdb()
            logger.info("♻️ System State Reset: Cleared flights_in_air and flushed Redis cache.")
        except Exception as e:
            logger.error(f"❌ [DB ERROR] reset_system_state failed: {e}")

    async def log_event(self, hex_id, callsign, event_type, details="", airport=None, origin=None, destination=None, runway=None, anomaly_flag=None, manual_timestamp=None):
        try:
            hex_id = hex_id.upper() if hex_id else None
            callsign = callsign.upper() if callsign else None
            airport = await self._resolve_icao(airport)
            origin = await self._resolve_icao(origin)
            destination = await self._resolve_icao(destination)
            
            ts = manual_timestamp if manual_timestamp else time.time()
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO flight_events (timestamp, hex_id, callsign, event_type, details, airport, origin, destination, runway, anomaly_flag) 
                    VALUES (TO_TIMESTAMP($1), $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """, ts, hex_id, callsign, event_type, details, airport, origin, destination, runway, anomaly_flag)
            
            if getattr(Config, 'DEBUG_MODE', False): 
                anom_str = f" | ⚠️ {anomaly_flag}" if anomaly_flag else ""
                rwy_str = f" | RWY: {runway}" if runway else ""
                orig_str = f" | From: {origin}" if origin else ""
                dest_str = f" | To: {destination}" if destination else ""
                logger.info(f"📝 {event_type} | {callsign} | {airport}{orig_str}{dest_str}{rwy_str}{anom_str}")
        except Exception as e: 
            logger.error(f"❌ [DB ERROR] log_event failed for {callsign} ({event_type}): {e}")

    async def bulk_log_events(self, events_list):
        """Bulk insert multiple flight events for better performance.
        
        events_list: [
            (timestamp, hex_id, callsign, event_type, details, airport, origin, destination, runway, anomaly_flag),
            ...
        ]
        """
        if not events_list:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany("""
                    INSERT INTO flight_events (timestamp, hex_id, callsign, event_type, details, airport, origin, destination, runway, anomaly_flag) 
                    VALUES (TO_TIMESTAMP($1), $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """, events_list)
            logger.info(f"💾 Bulk inserted {len(events_list)} flight events")
        except Exception as e:
            logger.error(f"❌ [DB ERROR] bulk_log_events failed: {e}")

    async def log_arrival(self, hex_id, callsign, airport, origin, runway=None, anomaly_flag=None, manual_timestamp=None):
        try:
            if not hex_id or not callsign:
                return
            
            hex_id = hex_id.upper().strip()
            callsign = callsign.upper().strip()
            
            airport = await self._resolve_icao(airport)
            origin = await self._resolve_icao(origin)
            
            if not airport:
                return
                
            ts = datetime.fromtimestamp(float(manual_timestamp)) if manual_timestamp else datetime.fromtimestamp(time.time())
            time_30_mins_ago = ts - timedelta(minutes=30)
            
            async with self.pool.acquire() as conn:
                # Check if there's an existing arrival for this hex_id at this airport in last 30 minutes
                existing = await conn.fetchrow("""
                    SELECT id FROM arrivals_log 
                    WHERE hex_id = $1 AND airport = $2 
                      AND timestamp > $3
                    LIMIT 1
                """, hex_id, airport, time_30_mins_ago)
                
                if existing:
                    # Update existing record instead of creating new one (touch-and-go pattern)
                    await conn.execute("""
                        UPDATE arrivals_log SET timestamp = $1, runway = $2, anomaly_flag = $3
                        WHERE id = $4
                    """, ts, runway, anomaly_flag, existing['id'])
                else:
                    # Insert new record
                    await conn.execute("""
                        INSERT INTO arrivals_log (hex_id, callsign, timestamp, origin, airport, runway, anomaly_flag)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """, hex_id, callsign, ts, origin, airport, runway, anomaly_flag)
        except Exception as e: 
            logger.error(f"❌ [DB ERROR] log_arrival failed for {callsign}: {e}")

    async def log_departure(self, hex_id, callsign, airport, destination, runway=None, anomaly_flag=None, manual_timestamp=None):
        try:
            if not hex_id or not callsign:
                return
            
            hex_id = hex_id.upper().strip()
            callsign = callsign.upper().strip()
            
            airport = await self._resolve_icao(airport)
            destination = await self._resolve_icao(destination)
            
            if not airport:
                return
                
            ts = datetime.fromtimestamp(float(manual_timestamp)) if manual_timestamp else datetime.fromtimestamp(time.time())
            time_30_mins_ago = ts - timedelta(minutes=30)
            async with self.pool.acquire() as conn:
                # Check if there's an existing departure for this hex_id at this airport in last 30 minutes
                existing = await conn.fetchrow("""
                    SELECT id FROM departures_log 
                    WHERE hex_id = $1 AND airport = $2 
                      AND timestamp > $3
                    LIMIT 1
                """, hex_id, airport, time_30_mins_ago)
                
                if existing:
                    # Update existing record instead of creating new one
                    await conn.execute("""
                        UPDATE departures_log SET timestamp = $1, runway = $2, anomaly_flag = $3
                        WHERE id = $4
                    """, ts, runway, anomaly_flag, existing['id'])
                else:
                    # Insert new record
                    await conn.execute("""
                        INSERT INTO departures_log (hex_id, callsign, timestamp, destination, airport, runway, anomaly_flag)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """, hex_id, callsign, ts, destination, airport, runway, anomaly_flag)
        except Exception as e: 
            logger.error(f"❌ [DB ERROR] log_departure failed for {callsign}: {e}")

    async def has_recent_departure(self, hex_id, hours=12):
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id FROM departures_log WHERE hex_id = $1 AND timestamp >= NOW() - INTERVAL '1 hour' * $2", hex_id, hours)
                return row is not None
        except Exception as e:
            return False

    async def register_landing_ops(self, hex_id, callsign, airport_code, origin_airport=None):
        try:
            hex_id = hex_id.upper() if hex_id else None
            callsign = callsign.upper() if callsign else None
            airport_code = await self._resolve_icao(airport_code)
            origin_airport = await self._resolve_icao(origin_airport)
            
            async with self.pool.acquire() as conn:
                existing = await conn.fetchrow("SELECT current_callsign FROM ground_ops WHERE hex_id = $1", hex_id)
                if existing:
                    await conn.execute("UPDATE ground_ops SET current_callsign = $1, airport = $2, origin = $3 WHERE hex_id = $4", callsign, airport_code, origin_airport, hex_id)
                else:
                    await conn.execute("INSERT INTO ground_ops (hex_id, current_callsign, inbound_callsign, landed_at, airport, origin) VALUES ($1, $2, $3, TO_TIMESTAMP($4), $5, $6)", hex_id, callsign, callsign, time.time(), airport_code, origin_airport)
        except Exception as e: 
            logger.error(f"❌ [DB ERROR] register_landing_ops failed for {callsign}: {e}")

    async def log_wake_up(self, hex_id, callsign, ap_code):
        try:
            hex_id = hex_id.upper() if hex_id else None
            callsign = callsign.upper() if callsign else None
            ap_code = await self._resolve_icao(ap_code)
            
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ground_ops (hex_id, current_callsign, inbound_callsign, origin, landed_at, airport)
                    VALUES ($1, $2, NULL, NULL, TO_TIMESTAMP($3), $4) ON CONFLICT DO NOTHING
                """, hex_id, callsign, time.time(), ap_code)
        except Exception as e:
            logger.warning(f"Failed to log wake-up for {callsign}: {e}")

    async def get_ground_info(self, hex_id):
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT current_callsign, inbound_callsign, EXTRACT(EPOCH FROM landed_at) as landed_at, airport, origin FROM ground_ops WHERE hex_id = $1", hex_id)
                return dict(row) if row else None
        except Exception as e: 
            return None

    async def clear_ground_op(self, hex_id):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM ground_ops WHERE hex_id = $1", hex_id)
        except Exception as e:
            logger.warning(f"Failed to clear ground_op for {hex_id}: {e}")

    async def is_on_ground(self, hex_id):
        return await self.get_ground_info(hex_id) is not None

    async def get_incomplete_arrivals(self):
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch("SELECT id, hex_id, callsign FROM arrivals_log WHERE origin IS NULL AND timestamp > TO_TIMESTAMP($1)", time.time() - 86400)
        except Exception as e:
            return []

    async def update_arrival_broadcast(self, row_id, hex_id, origin, anomaly_flag=None, original_value=None, ai_reasoning=None, confidence_score=1.0, callsign=None):
        try:
            hex_id = hex_id.upper() if hex_id else None
            callsign = callsign.upper() if callsign else None
            origin = await self._resolve_icao(origin)
            
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if anomaly_flag:
                        await conn.execute("UPDATE arrivals_log SET origin = $1, anomaly_flag = $3 WHERE id = $2", origin, row_id, anomaly_flag)
                        await conn.execute("UPDATE ground_ops SET origin = $1 WHERE hex_id = $2", origin, hex_id)
                        await conn.execute("UPDATE flight_events SET origin = $1, anomaly_flag = $3 WHERE hex_id = $2 AND event_type='LANDED'", origin, hex_id, anomaly_flag)
                        
                        if anomaly_flag == 'AI_ENRICHED':
                            await conn.execute("""
                                INSERT INTO ai_enrichment_audit 
                                (target_table, record_id, hex_id, callsign, original_value, ai_inferred_value, ai_reasoning, confidence_score)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            """, 'arrivals_log', row_id, hex_id, callsign, original_value, origin, ai_reasoning, confidence_score)
                    else:
                        await conn.execute("UPDATE arrivals_log SET origin = $1 WHERE id = $2", origin, row_id)
                        await conn.execute("UPDATE ground_ops SET origin = $1 WHERE hex_id = $2", origin, hex_id)
                        await conn.execute("UPDATE flight_events SET origin = $1 WHERE hex_id = $2 AND event_type='LANDED'", origin, hex_id)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] update_arrival_broadcast failed: {e}")

    async def get_aircraft_info(self, hex_id):
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT registration, type FROM aircraft_info WHERE hex_id = $1", hex_id)
                return dict(row) if row else None
        except Exception as e: 
            return None

    async def upsert_flight_in_air(self, hexid, callsign, lat, lon, alt, speed, heading):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO flights_in_air (hexid, callsign, lat, lon, alt, speed, heading, last_seen)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC')
                    ON CONFLICT (hexid) DO UPDATE
                    SET callsign = EXCLUDED.callsign, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                        alt = EXCLUDED.alt, speed = EXCLUDED.speed, heading = EXCLUDED.heading,
                        last_seen = NOW() AT TIME ZONE 'UTC'
                """, hexid, callsign, lat, lon, alt, speed, heading)
        except Exception as e:
            logger.warning(f"Failed to upsert flight in air {callsign}: {e}")

    async def bulk_upsert_flights_in_air(self, flights_list):
        if not flights_list: return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany("""
                    INSERT INTO flights_in_air (hexid, callsign, lat, lon, alt, speed, heading, last_seen, origin_icao, dest_icao, origin_iata, dest_iata, origin_lat, origin_lon, dest_lat, dest_lon, callsign_iata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC', $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    ON CONFLICT (hexid) DO UPDATE 
                    SET callsign = EXCLUDED.callsign, lat = EXCLUDED.lat, lon = EXCLUDED.lon, 
                        alt = EXCLUDED.alt, speed = EXCLUDED.speed, heading = EXCLUDED.heading, 
                        last_seen = NOW() AT TIME ZONE 'UTC',
                        origin_icao = COALESCE(EXCLUDED.origin_icao, flights_in_air.origin_icao),
                        dest_icao = COALESCE(EXCLUDED.dest_icao, flights_in_air.dest_icao),
                        origin_iata = COALESCE(EXCLUDED.origin_iata, flights_in_air.origin_iata),
                        dest_iata = COALESCE(EXCLUDED.dest_iata, flights_in_air.dest_iata),
                        origin_lat = COALESCE(EXCLUDED.origin_lat, flights_in_air.origin_lat),
                        origin_lon = COALESCE(EXCLUDED.origin_lon, flights_in_air.origin_lon),
                        dest_lat = COALESCE(EXCLUDED.dest_lat, flights_in_air.dest_lat),
                        dest_lon = COALESCE(EXCLUDED.dest_lon, flights_in_air.dest_lon),
                        callsign_iata = COALESCE(EXCLUDED.callsign_iata, flights_in_air.callsign_iata)
                """, flights_list)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] bulk_upsert_flights_in_air failed: {e}")

    async def update_flight_in_air_route(self, hex_id, callsign, origin_icao, dest_icao, origin_iata, dest_iata, origin_lat, origin_lon, dest_lat, dest_lon, callsign_iata):
        """Update flights_in_air with route data (origin/destination, coords, iata)"""
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute("""
                    UPDATE flights_in_air 
                    SET origin_icao = $1, dest_icao = $2, origin_iata = $3, dest_iata = $4,
                        origin_lat = $5, origin_lon = $6, dest_lat = $7, dest_lon = $8, callsign_iata = $9
                    WHERE hexid = $10
                """, origin_icao, dest_icao, origin_iata, dest_iata, origin_lat, origin_lon, dest_lat, dest_lon, callsign_iata, hex_id)
                logger.info(f"💾 Updated flights_in_air route for {callsign} ({hex_id}): {origin_icao} -> {dest_icao} | Rows affected: {result}")
        except Exception as e:
            logger.error(f"❌ Failed to update flight route for {callsign} ({hex_id}): {e}")

    async def cleanup_stale_flights(self):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM flights_in_air WHERE last_seen < (NOW() AT TIME ZONE 'UTC') - INTERVAL '3 minutes'")
        except Exception as e:
            logger.warning(f"Failed to cleanup stale flights: {e}")

    async def remove_flight_from_air(self, hexid):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM flights_in_air WHERE hexid = $1", hexid)
        except Exception as e:
            logger.warning(f"Failed to remove flight from air {hexid}: {e}")

    async def get_historical_route(self, hex_id, callsign=None):
        try:
            async with self.pool.acquire() as conn:
                if callsign:
                    row = await conn.fetchrow("""
                        SELECT origin, destination FROM flight_events 
                        WHERE hex_id = $1 AND callsign = $2 
                          AND (origin IS NOT NULL OR destination IS NOT NULL)
                        ORDER BY timestamp DESC LIMIT 1
                    """, hex_id, callsign)
                    if row:
                        return (row['origin'], row['destination'])
                    return (None, None) 
                
                row = await conn.fetchrow("""
                    SELECT origin, destination FROM flight_events 
                    WHERE hex_id = $1 AND (origin IS NOT NULL OR destination IS NOT NULL)
                    ORDER BY timestamp DESC LIMIT 1
                """, hex_id)
                return (row['origin'], row['destination']) if row else (None, None)
        except Exception as e:
            return None, None

    async def upsert_flight_schedule(self, airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time):
        try:
            airport_code = await self._resolve_icao(airport_code)
            route_airport = await self._resolve_icao(route_airport)
            flight_number = await self._resolve_flight_number(flight_number)
            
            direction = direction.upper() if direction else None
            callsign = callsign.upper() if callsign else None
            hex_id = hex_id.upper() if hex_id else None
            
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO flight_schedules (airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time)
                    VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7))
                    ON CONFLICT (airport_code, direction, flight_number, hex_id, route_airport, scheduled_time)
                    DO UPDATE SET
                        hex_id = COALESCE(EXCLUDED.hex_id, flight_schedules.hex_id),
                        route_airport = EXCLUDED.route_airport
                """, airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time)
        except Exception as e:
            logger.warning(f"Failed to upsert flight schedule {callsign}: {e}")

    async def get_route_from_schedule(self, callsign, current_airport=None):
        try:
            async with self.pool.acquire() as conn:
                raw_cs = callsign.upper().strip() if callsign else None
                flt_num_iata = await self._resolve_flight_number(raw_cs)
                
                if current_airport:
                    # 🌟 FIX: Strict Diurnal Cycle Window [-18h to +6h]
                    row = await conn.fetchrow("""
                        SELECT airport_code, direction, route_airport 
                        FROM flight_schedules 
                        WHERE (TRIM(flight_number) = $1 OR TRIM(callsign) = $2)
                          AND (
                              (direction = 'DEPARTURES' AND airport_code = $3) OR 
                              (direction = 'ARRIVALS' AND route_airport = $3)
                          )
                          AND (
                              scheduled_time BETWEEN NOW() - INTERVAL '18 hours' AND NOW() + INTERVAL '6 hours'
                              OR scheduled_time IS NULL
                          )
                        ORDER BY 
                          CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                               WHEN scheduled_time IS NULL THEN 2 
                               ELSE 1 END,
                          ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, NOW()) - NOW()))) ASC 
                        LIMIT 1
                    """, flt_num_iata, raw_cs, current_airport)
                    if row:
                        return (row['airport_code'], row['route_airport']) if row['direction'] == 'DEPARTURES' else (row['route_airport'], row['airport_code'])

                row = await conn.fetchrow("""
                    SELECT airport_code, direction, route_airport 
                    FROM flight_schedules 
                    WHERE (TRIM(flight_number) = $1 OR TRIM(callsign) = $2)
                    AND actual_time IS NULL 
                    AND (
                        scheduled_time BETWEEN NOW() - INTERVAL '18 hours' AND NOW() + INTERVAL '6 hours'
                        OR scheduled_time IS NULL
                    )
                    ORDER BY 
                      CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                           WHEN scheduled_time IS NULL THEN 2 
                           ELSE 1 END,
                      ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, NOW()) - NOW()))) ASC 
                    LIMIT 1
                """, flt_num_iata, raw_cs)
                
                if row:
                    return (row['airport_code'], row['route_airport']) if row['direction'] == 'DEPARTURES' else (row['route_airport'], row['airport_code'])
                        
                return None, None
        except Exception as e:
            return None, None

    async def get_planes_missing_enrichment(self):
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch("""
                    SELECT f.hexid, f.callsign FROM flights_in_air f
                    LEFT JOIN flight_events e ON f.callsign = e.callsign AND e.event_type = 'ENRICHMENT'
                    WHERE e.id IS NULL
                """)
        except Exception as e:
            return []

    async def cleanup_stale_ground_ops(self, hours=4):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM ground_ops WHERE landed_at < NOW() - INTERVAL '1 hour' * $1", hours)
        except Exception as e:
            logger.warning(f"Failed to cleanup stale ground_ops: {e}")

    async def get_avg_approach_time(self, airport_code):
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT AVG(EXTRACT(EPOCH FROM (a.timestamp - e.timestamp)))/60 as avg_mins
                    FROM arrivals_log a JOIN flight_events e ON a.hex_id = e.hex_id AND e.event_type = 'APPROACHING' AND e.airport = a.airport
                    WHERE a.airport = $1 AND a.timestamp > e.timestamp AND a.timestamp < e.timestamp + INTERVAL '1 hour'
                """, airport_code)
                return int(row['avg_mins']) if row and row['avg_mins'] else None
        except Exception as e:
            return None
            
    async def log_user_alert(self, chat_id, target_callsign, alert_type, threshold_mins):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO user_alerts (chat_id, target_callsign, alert_type, threshold_mins, status)
                    VALUES ($1, $2, $3, $4, 'ACTIVE')
                """, chat_id, target_callsign, alert_type, threshold_mins)
        except Exception as e:
            logger.warning(f"Failed to log user alert for {target_callsign}: {e}")

    async def update_schedule_status(self, airport_code, direction, callsign, status_flag, hex_id=None, route_airport=None):
        airport_code = await self._resolve_icao(airport_code)
        route_airport = await self._resolve_icao(route_airport)
        direction = direction.upper() if direction else None
        status_flag = str(status_flag) if status_flag else None
        
        raw_cs = callsign.upper().strip() if callsign else None
        flt_num_iata = await self._resolve_flight_number(raw_cs)
        hex_id = hex_id.upper() if hex_id else None
        
        try:
            async with self.pool.acquire() as conn:
                # 🌟 FIX: Strict Diurnal Cycle Window [-18h to +6h]
                # Step 1: Fast Path (Try updating by Callsign)
                row = await conn.fetchrow("""
                    UPDATE flight_schedules 
                    SET anomaly_flag = $1::VARCHAR(50)
                    WHERE id = (
                        SELECT id FROM flight_schedules 
                        WHERE airport_code = $2 
                          AND direction = $3 
                          AND (TRIM(flight_number) = $4 OR TRIM(callsign) = $5)
                          AND actual_time IS NULL 
                          AND (
                              scheduled_time BETWEEN NOW() - INTERVAL '18 hours' AND NOW() + INTERVAL '6 hours'
                              OR scheduled_time IS NULL
                          )
                        ORDER BY 
                          CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                               WHEN scheduled_time IS NULL THEN 2 
                               ELSE 1 END,
                          ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, NOW()) - NOW()))) ASC 
                        LIMIT 1
                    )
                    RETURNING id
                """, status_flag, airport_code, direction, flt_num_iata, raw_cs)

                # Step 2: The Alphanumeric Fallback (Protected during PRE_FLIGHT)
                if not row and hex_id:
                    row = await conn.fetchrow("""
                        UPDATE flight_schedules 
                        SET anomaly_flag = $1::VARCHAR(50), 
                            changed_callsign = CASE 
                                WHEN $1::VARCHAR(50) != 'PRE_FLIGHT' AND callsign != $4 THEN callsign 
                                ELSE changed_callsign 
                            END,
                            callsign = CASE 
                                WHEN $1::VARCHAR(50) != 'PRE_FLIGHT' THEN $4 
                                ELSE callsign 
                            END
                        WHERE id = (
                            SELECT id FROM flight_schedules 
                            WHERE airport_code = $2 
                              AND direction = $3 
                              AND hex_id = $5
                              AND actual_time IS NULL 
                              AND (
                                  scheduled_time BETWEEN NOW() - INTERVAL '18 hours' AND NOW() + INTERVAL '6 hours'
                                  OR scheduled_time IS NULL
                              )
                            ORDER BY 
                              CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                                   WHEN scheduled_time IS NULL THEN 2 
                                   ELSE 1 END,
                              ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, NOW()) - NOW()))) ASC 
                            LIMIT 1
                        )
                        RETURNING id
                    """, status_flag, airport_code, direction, raw_cs, hex_id)

                # Step 3: Only INSERT if it's a true Ghost Flight
                if not row and hex_id:
                    await conn.execute("""
                        INSERT INTO flight_schedules 
                        (airport_code, direction, flight_number, callsign, hex_id, route_airport, actual_time, anomaly_flag)
                        VALUES ($1, $2, $3, $4, $5, $6, NULL, $7::VARCHAR(50))
                        ON CONFLICT DO NOTHING
                    """, airport_code, direction, flt_num_iata, raw_cs, hex_id, route_airport, status_flag)

        except Exception as e:
            logger.exception(f"❌ [DB ERROR] update_schedule_status failed for callsign={callsign}")
            logger.error(f"  Parameters: airport_code={airport_code} (type: {type(airport_code)}), "
                        f"direction={direction} (type: {type(direction)}), "
                        f"status_flag={status_flag} (type: {type(status_flag)}), "
                        f"hex_id={hex_id} (type: {type(hex_id)}), "
                        f"route_airport={route_airport} (type: {type(route_airport)}), "
                        f"flt_num_iata={flt_num_iata} (type: {type(flt_num_iata)}), "
                        f"raw_cs={raw_cs} (type: {type(raw_cs)})")

    async def update_enriched_route(self, hex_id, callsign, origin, destination):
        try:
            hex_id = hex_id.upper() if hex_id else None
            
            raw_cs = callsign.upper().strip() if callsign else None
            flt_num_iata = await self._resolve_flight_number(raw_cs)
            
            origin = await self._resolve_icao(origin)
            destination = await self._resolve_icao(destination)
            
            async with self.pool.acquire() as conn:
                if destination:
                    await conn.execute("""
                        UPDATE flight_schedules 
                        SET route_airport = $1 
                        WHERE (hex_id = $2 OR TRIM(flight_number) = $3 OR TRIM(callsign) = $4) 
                          AND direction = 'DEPARTURES' AND (route_airport IS NULL OR route_airport = 'UNK') AND actual_time IS NULL
                    """, destination, hex_id, flt_num_iata, raw_cs)
                if origin:
                    await conn.execute("""
                        UPDATE flight_schedules 
                        SET route_airport = $1
                        WHERE (hex_id = $2 OR TRIM(flight_number) = $3 OR TRIM(callsign) = $4)
                          AND direction = 'ARRIVALS' AND (route_airport IS NULL OR route_airport = 'UNK') AND actual_time IS NULL
                    """, origin, hex_id, flt_num_iata, raw_cs)
        except Exception as e:
            logger.warning(f"Failed to update schedule with CSV for {raw_cs}: {e}")

    async def link_actual_flight_to_schedule(self, airport_code, direction, callsign, hex_id, timestamp, route_airport=None):
        airport_code = await self._resolve_icao(airport_code)
        route_airport = await self._resolve_icao(route_airport)
        direction = direction.upper() if direction else None
        
        raw_cs = callsign.upper().strip() if callsign else None
        flt_num_iata = await self._resolve_flight_number(raw_cs)
        hex_id = hex_id.upper() if hex_id else None

        try:
            async with self.pool.acquire() as conn:
                # 🌟 FIX: Strict Diurnal Cycle Window [-18h to +6h]
                row = await conn.fetchrow("""
                    SELECT id, callsign, hex_id, anomaly_flag FROM flight_schedules 
                    WHERE airport_code = $1 AND direction = $2 
                      AND (TRIM(flight_number) = $3 OR TRIM(callsign) = $4)
                      AND (actual_time IS NULL OR (hex_id = $6 AND hex_id IS NOT NULL)) 
                      AND (
                          scheduled_time BETWEEN TO_TIMESTAMP($5) - INTERVAL '18 hours' AND TO_TIMESTAMP($5) + INTERVAL '6 hours'
                          OR anomaly_flag = 'PRE_FLIGHT'
                          OR scheduled_time IS NULL
                      )
                    ORDER BY 
                      CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                           WHEN actual_time IS NULL THEN 1 
                           ELSE 2 END,
                      ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, TO_TIMESTAMP($5)) - TO_TIMESTAMP($5)))) ASC 
                    LIMIT 1
                """, airport_code, direction, flt_num_iata, raw_cs, timestamp, hex_id)

                if not row and hex_id:
                    row = await conn.fetchrow("""
                        SELECT id, callsign, hex_id, anomaly_flag FROM flight_schedules 
                        WHERE airport_code = $1 AND direction = $2 
                          AND hex_id = $3 
                          AND (actual_time IS NULL OR actual_time >= TO_TIMESTAMP($4) - INTERVAL '12 hours')
                          AND (
                              scheduled_time BETWEEN TO_TIMESTAMP($4) - INTERVAL '18 hours' AND TO_TIMESTAMP($4) + INTERVAL '6 hours'
                              OR anomaly_flag = 'PRE_FLIGHT'
                              OR scheduled_time IS NULL
                          )
                        ORDER BY 
                          CASE WHEN anomaly_flag = 'PRE_FLIGHT' THEN 0 
                               WHEN actual_time IS NULL THEN 1 
                               ELSE 2 END,
                          ABS(EXTRACT(EPOCH FROM (COALESCE(scheduled_time, TO_TIMESTAMP($4)) - TO_TIMESTAMP($4)))) ASC 
                        LIMIT 1
                    """, airport_code, direction, hex_id, timestamp)

                if row:
                    record_id = row['id']
                    existing_flag = row['anomaly_flag']
                    
                    final_flag = None
                    if existing_flag and existing_flag not in ['PRE_FLIGHT', 'ARRIVING_SHORTLY']:
                        final_flag = existing_flag

                    try:
                        await conn.execute("""
                            UPDATE flight_schedules 
                            SET actual_time = COALESCE(actual_time, TO_TIMESTAMP($1)), 
                                hex_id = $2, 
                                changed_callsign = CASE WHEN callsign != $3 THEN callsign ELSE changed_callsign END,
                                callsign = $3,
                                anomaly_flag = $4,
                                updated_from = 'RADAR_LIVE_TRACKER',
                                route_airport = CASE 
                                    WHEN $6::VARCHAR IS NOT NULL THEN $6::VARCHAR 
                                    ELSE route_airport 
                                END
                            WHERE id = $5
                        """, timestamp, hex_id, raw_cs, final_flag, record_id, route_airport)
                        
                    except asyncpg.UniqueViolationError:
                        logger.info(f"🔄 Resolving DB Schedule Collision for {raw_cs}. Merging duplicates.")
                        await conn.execute("DELETE FROM flight_schedules WHERE id = $1", record_id)
                        await conn.execute("""
                            UPDATE flight_schedules 
                            SET actual_time = COALESCE(actual_time, TO_TIMESTAMP($1)), 
                                hex_id = $7,
                                changed_callsign = CASE WHEN callsign != $6 THEN callsign ELSE changed_callsign END,
                                callsign = $6,
                                anomaly_flag = COALESCE($4, anomaly_flag),
                                updated_from = 'RADAR_LIVE_TRACKER'
                            WHERE airport_code = $2 AND direction = $3 AND (TRIM(flight_number) = $5 OR TRIM(callsign) = $6)
                              AND (actual_time IS NULL OR hex_id = $7)
                        """, timestamp, airport_code, direction, final_flag, flt_num_iata, raw_cs, hex_id)
                else:
                    await conn.execute("""
                        INSERT INTO flight_schedules 
                        (airport_code, direction, flight_number, callsign, hex_id, route_airport, actual_time, anomaly_flag, created_from, updated_from)
                        VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7), 'UNSCHEDULED', 'RADAR_LIVE_TRACKER', 'RADAR_LIVE_TRACKER')
                        ON CONFLICT DO NOTHING
                    """, airport_code, direction, flt_num_iata, raw_cs, hex_id, route_airport, timestamp)

        except asyncpg.ForeignKeyViolationError as e:
            if e.constraint_name == 'flight_schedules_airport_code_fkey':
                logger.info(f"⚠️ Missing airport detected. Auto-adding to DB and retrying...")
                async with self.pool.acquire() as new_conn:
                    await new_conn.execute("INSERT INTO airports (icao) VALUES ($1) ON CONFLICT DO NOTHING", airport_code)
                    if route_airport:
                        await new_conn.execute("INSERT INTO airports (icao) VALUES ($1) ON CONFLICT DO NOTHING", route_airport)
                return await self.link_actual_flight_to_schedule(airport_code, direction, callsign, hex_id, timestamp, route_airport)
            else:
                logger.error(f"❌ [DB ERROR] Unhandled FK violation for {callsign}: {e}")
        except Exception as e:
            logger.error(f"❌ [DB ERROR] link_actual_flight_to_schedule failed for {callsign}: {e}")

    async def log_telemetry(self, hex_id, callsign, lat, lon, alt, speed, heading):
        if not self.influx_client: return
        try:
            p = Point("flight_path") \
                .tag("hex_id", hex_id) \
                .tag("callsign", callsign) \
                .field("lat", float(lat)) \
                .field("lon", float(lon)) \
                .field("alt", float(alt)) \
                .field("speed", float(speed)) \
                .field("heading", float(heading))

            await self.influx_write_api.write(bucket=Config.INFLUXDB_BUCKET, record=p)
        except Exception as e:
            logger.warning(f"Failed to write telemetry to InfluxDB for {callsign}: {e}")

    async def bulk_refresh_schedules(self, airport_code, min_timestamp, records_list):
        airport_code = await self._resolve_icao(airport_code)
        
        processed_records = []
        for r in records_list:
            ap_code = await self._resolve_icao(r[0])
            dir_code = r[1].upper() if r[1] else None
            flt_num = await self._resolve_flight_number(r[2].upper() if r[2] else None)
            cs = r[3].upper() if r[3] else None
            hexid = r[4].upper() if r[4] else None
            rt_ap = await self._resolve_icao(r[5])
            processed_records.append((ap_code, dir_code, flt_num, cs, hexid, rt_ap, r[6]))
            
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    DELETE FROM flight_schedules 
                    WHERE airport_code = $1 
                      AND actual_time IS NULL 
                      AND scheduled_time >= TO_TIMESTAMP($2)
                """, airport_code, min_timestamp)

                await conn.executemany("""
                    INSERT INTO flight_schedules 
                    (airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time)
                    VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7))
                    ON CONFLICT (airport_code, direction, flight_number, hex_id, route_airport, scheduled_time) 
                    DO NOTHING
                """, processed_records)

    async def log_ai_enrichment(self, target_table, record_id, hex_id, callsign, original_value, ai_inferred_value, ai_reasoning, confidence_score=1.0):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ai_enrichment_audit 
                    (target_table, record_id, hex_id, callsign, original_value, ai_inferred_value, ai_reasoning, confidence_score)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """, target_table, record_id, hex_id, callsign, original_value, ai_inferred_value, ai_reasoning, confidence_score)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] log_ai_enrichment failed: {e}")

    async def log_ai_insight(self, insight_type, trigger_event, insight_text, target_airport=None):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ai_insights_log 
                    (insight_type, trigger_event, insight_text, target_airport)
                    VALUES ($1, $2, $3, $4)
                """, insight_type, trigger_event, insight_text, target_airport)
        except Exception as e:
            logger.error(f"❌ [DB ERROR] log_ai_insight failed: {e}")

    async def update_schedule_with_csv(self, callsign, hex_id, origin, destination):
        """Heals the database using offline routes.csv data when the API fails."""
        try:
            raw_cs = callsign.upper().strip() if callsign else None
            flt_num_iata = await self._resolve_flight_number(raw_cs)
            hex_id = hex_id.upper().strip() if hex_id else None
            origin = await self._resolve_icao(origin)
            destination = await self._resolve_icao(destination)
            
            async with self.pool.acquire() as conn:
                # --- 1. DEPARTURES HEALING ---
                try:
                    dep_row = await conn.fetchrow("""
                        SELECT id, actual_time FROM flight_schedules 
                        WHERE (TRIM(flight_number) = $1 OR TRIM(callsign) = $2)
                          AND direction = 'DEPARTURES' 
                          AND (actual_time IS NULL OR actual_time >= NOW() - INTERVAL '24 hours')
                        ORDER BY COALESCE(actual_time, scheduled_time, NOW()) DESC LIMIT 1
                    """, flt_num_iata, raw_cs)
                    
                    if dep_row:
                        if dep_row['actual_time'] is not None:
                            await conn.execute("""
                                UPDATE flight_schedules 
                                SET airport_code = $1, route_airport = $2
                                WHERE id = $3 AND (route_airport IS NULL OR route_airport = 'UNK')
                            """, origin, destination, dep_row['id'])
                        else:
                            await conn.execute("""
                                UPDATE flight_schedules 
                                SET airport_code = $1, route_airport = $2, anomaly_flag = 'OFFLINE_CSV_ROUTE', updated_from = 'RADAR_CSV_HEALER'
                                WHERE id = $3
                            """, origin, destination, dep_row['id'])
                    else:
                        await conn.execute("""
                            INSERT INTO flight_schedules 
                            (airport_code, direction, flight_number, callsign, hex_id, route_airport, anomaly_flag, created_from, updated_from) 
                            VALUES ($1, 'DEPARTURES', $2, $3, $4, $5, 'OFFLINE_CSV_ROUTE', 'RADAR_CSV_HEALER', 'RADAR_CSV_HEALER') 
                            ON CONFLICT DO NOTHING
                        """, origin, flt_num_iata, raw_cs, hex_id, destination)
                except asyncpg.UniqueViolationError:
                    pass 
                except Exception as e:
                    logger.error(f"❌ [DB ERROR] Departure healing failed for {callsign}: {e}")

                # --- 2. ARRIVALS HEALING ---
                try:
                    arr_row = await conn.fetchrow("""
                        SELECT id, actual_time FROM flight_schedules 
                        WHERE (TRIM(flight_number) = $1 OR TRIM(callsign) = $2)
                          AND direction = 'ARRIVALS' 
                          AND (actual_time IS NULL OR actual_time >= NOW() - INTERVAL '24 hours')
                        ORDER BY COALESCE(actual_time, scheduled_time, NOW()) DESC LIMIT 1
                    """, flt_num_iata, raw_cs)
                    
                    if arr_row:
                        if arr_row['actual_time'] is not None:
                            await conn.execute("""
                                UPDATE flight_schedules 
                                SET airport_code = $1, route_airport = $2
                                WHERE id = $3 AND (route_airport IS NULL OR route_airport = 'UNK')
                            """, destination, origin, arr_row['id'])
                        else:
                            await conn.execute("""
                                UPDATE flight_schedules 
                                SET airport_code = $1, route_airport = $2, anomaly_flag = 'OFFLINE_CSV_ROUTE', updated_from = 'RADAR_CSV_HEALER'
                                WHERE id = $3
                            """, destination, origin, arr_row['id'])
                    else:
                        await conn.execute("""
                            INSERT INTO flight_schedules 
                            (airport_code, direction, flight_number, callsign, hex_id, route_airport, anomaly_flag, created_from, updated_from) 
                            VALUES ($1, 'ARRIVALS', $2, $3, $4, $5, 'OFFLINE_CSV_ROUTE', 'RADAR_CSV_HEALER', 'RADAR_CSV_HEALER') 
                            ON CONFLICT DO NOTHING
                        """, destination, flt_num_iata, raw_cs, hex_id, origin)
                except asyncpg.UniqueViolationError:
                    pass 
                except Exception as e:
                    logger.error(f"❌ [DB ERROR] Arrival healing failed for {callsign}: {e}")
                    
                # --- 3. LOG FORENSIC EVENT ---
                try:
                    await conn.execute("""
                        INSERT INTO flight_events
                        (timestamp, hex_id, callsign, event_type, details, origin, destination, anomaly_flag)
                        VALUES (NOW(), $1, $2, 'OFFLINE_ENRICHMENT', 'Schedule healed via routes.csv fallback', $3, $4, 'OFFLINE_CSV_ROUTE')
                    """, hex_id, raw_cs, origin, destination)
                except Exception as inner_e:
                    logger.warning(f"Failed to log OFFLINE_ENRICHMENT event: {inner_e}")

        except Exception as e:
            logger.error(f"❌ [DB ERROR] update_schedule_with_csv failed for {callsign}: {e}")