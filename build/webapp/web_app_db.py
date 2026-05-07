import csv
import os
import logging
import asyncpg
import orjson
import redis.asyncio as redis
from datetime import datetime
from config import Config

logger = logging.getLogger(__name__)

# Global pool reference
_db_pool = None
_redis_client = None

def get_db_pool(pool):
    """Get or create database pool for web_app_db functions"""
    global _db_pool
    if _db_pool is None:
        _db_pool = pool
    return _db_pool

async def get_redis_client():
    """Get or create Redis client"""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(**Config.REDIS_PARAMS)
    return _redis_client

# 🌟 GLOBAL MEMORY CACHES FOR FAST ANALYTICS
AIRPORT_MAP = {}
AIRPORT_COORDS = {}  
AIRLINE_MAP = {}
IATA_TO_ICAO_MEM = {} 

def load_airlines_csv():
    filepath = "data/airlines.csv" if os.path.exists("data/airlines.csv") else "airlines.csv"
    if not os.path.exists(filepath): return
    try:
        with open(filepath, mode="r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                icao = row.get("ICAO", "").strip().upper()
                iata = row.get("IATA", "").strip().upper()
                name = row.get("Name", "").strip()
                if icao and icao != "\\N": AIRLINE_MAP[icao] = name
                if iata and iata != "\\N": AIRLINE_MAP[iata] = name
    except Exception as e: logger.error(f"CSV Airline Load Error: {e}")

def normalize_ap(code):
    if not code or code.strip() in ('UNK', '\\N', 'ALL'): return code.strip() if code else code
    code = code.strip().upper()
    return Config.IATA_TO_ICAO.get(code, IATA_TO_ICAO_MEM.get(code, code))

def fmt_apt(code):
    if not code: return code
    norm_code = normalize_ap(code)
    if not norm_code or norm_code in ('UNK', '\\N', 'ALL'): return norm_code
    city = AIRPORT_MAP.get(norm_code)
    return f"{city} ({norm_code})" if city else norm_code

def fmt_aln(code):
    if not code or code == 'ALL': return code
    name = AIRLINE_MAP.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()

async def resolve_airport_codes(conn, airport: str):
    icao_code = airport.upper()
    for icao, data in getattr(Config, 'TARGET_AIRPORTS', {}).items():
        if airport.upper() == icao or airport.upper() == data.get('iata', ''):
            return icao
    try:
        ap_row = await conn.fetchrow("SELECT icao FROM airports WHERE icao = $1 OR iata = $1", airport.upper())
        if ap_row and ap_row.get('icao'): return ap_row.get('icao')
    except Exception: pass
    return icao_code

async def get_airport_coords(conn, airport_code):
    code = await resolve_airport_codes(conn, airport_code)
    if code in getattr(Config, 'TARGET_AIRPORTS', {}):
        return getattr(Config, 'TARGET_AIRPORTS')[code].get('lat'), getattr(Config, 'TARGET_AIRPORTS')[code].get('lon')
    if code in AIRPORT_COORDS:
        return AIRPORT_COORDS[code]
    try:
        ap = await conn.fetchrow("SELECT lat, lon FROM airports WHERE icao = $1", code)
        if ap and ap['lat'] is not None and ap['lon'] is not None:
            AIRPORT_COORDS[code] = (float(ap['lat']), float(ap['lon']))
            return float(ap['lat']), float(ap['lon'])
    except Exception: pass
    return None, None

async def init_web_app_db(db_pool):
    """Handles migration and RAM caching on startup."""
    load_airlines_csv()
    try:
        logger.info("🔄 Running Silent IATA -> ICAO Database Migration...")
        async with db_pool.acquire() as conn:
            for icao, data in Config.TARGET_AIRPORTS.items():
                iata = data.get('iata')
                if iata:
                    await conn.execute("UPDATE flight_schedules SET route_airport = $1 WHERE route_airport = $2", icao, iata)
                    await conn.execute("UPDATE flight_schedules SET airport_code = $1 WHERE airport_code = $2", icao, iata)
                    await conn.execute("UPDATE arrivals_log SET airport = $1 WHERE airport = $2", icao, iata)
                    await conn.execute("UPDATE arrivals_log SET origin = $1 WHERE origin = $2", icao, iata)
                    await conn.execute("UPDATE departures_log SET airport = $1 WHERE airport = $2", icao, iata)
                    await conn.execute("UPDATE departures_log SET destination = $1 WHERE destination = $2", icao, iata)
                    await conn.execute("UPDATE flight_events SET airport = $1 WHERE airport = $2", icao, iata)
                    await conn.execute("UPDATE flight_events SET origin = $1 WHERE origin = $2", icao, iata)
                    await conn.execute("UPDATE flight_events SET destination = $1 WHERE destination = $2", icao, iata)
                    await conn.execute("UPDATE ground_ops SET airport = $1 WHERE airport = $2", icao, iata)
                    await conn.execute("UPDATE ground_ops SET origin = $1 WHERE origin = $2", icao, iata)
        logger.info("✅ Database Normalization Complete.")
    except Exception as e:
        logger.warning(f"⚠️ Database Normalization Error: {e}")
        
    try:
        async with db_pool.acquire() as conn:
            db_airports = await conn.fetch("SELECT * FROM airports")
            for r in db_airports:
                display = r.get('city') or r.get('name') or r.get('location') or ''
                icao = r.get('icao')
                iata = r.get('iata')
                lat, lon = r.get('lat'), r.get('lon')
                
                if icao:
                    if display: AIRPORT_MAP[icao.upper()] = display
                    if lat is not None and lon is not None: AIRPORT_COORDS[icao.upper()] = (float(lat), float(lon))
                if iata and icao:
                    IATA_TO_ICAO_MEM[iata.upper()] = icao.upper()
        logger.info(f"✅ Synced DB Airports. Total memory cache: {len(AIRPORT_MAP)} Airports.")
    except Exception as e:
        logger.warning(f"⚠️ Custom DB Airports sync skipped: {e}")

# =====================================================================
# 🌟 DATA FETCHING METHODS
# =====================================================================

async def fetch_filter_options(db_pool):
    async with db_pool.acquire() as conn:
        airports = await conn.fetch("SELECT icao, lat, lon FROM airports")
        airlines = await conn.fetch("SELECT DISTINCT SUBSTRING(callsign FROM 1 FOR 3) as code FROM flights_in_air WHERE callsign IS NOT NULL")
        ap_list = [{"code": r['icao'], "display": fmt_apt(r['icao']), "lat": r['lat'], "lon": r['lon']} for r in airports if r['icao']]
        al_list = [{"code": r['code'], "display": fmt_aln(r['code'])} for r in airlines if len(r['code']) >= 2]
        return {
            "airports": sorted(ap_list, key=lambda x: x["display"]),
            "airlines": sorted(al_list, key=lambda x: x["display"])
        }

async def fetch_live_flights(db_pool, airline, airport):
    # Try Redis first
    try:
        r = await get_redis_client()
        redis_key = Config.REDIS_LIVE_FLIGHTS_KEY
        
        # Check if Redis has data
        exists = await r.exists(redis_key)
        if exists:
            all_flights = await r.hgetall(redis_key)
            if all_flights:
                flights = []
                for hex_id, data in all_flights.items():
                    try:
                        fl = orjson.loads(data)
                    except:
                        fl = {"hexid": hex_id}
                    
                    # Apply airline filter
                    if airline and airline != 'ALL':
                        cs = fl.get('callsign', '')
                        if not cs or not cs.startswith(airline.upper()):
                            continue
                    
                    # Apply airport filter (simple substring match)
                    if airport and airport != 'ALL':
                        cs = fl.get('callsign', '')
                        if airport.upper() not in cs:
                            continue
                    
                    fl['hexid'] = hex_id
                    # Fix swapped lat/lon from binCraft decoder bug (lat should be ~6-37 for India, lon ~68-98)
                    lat, lon = fl.get('lat'), fl.get('lon')
                    if lat is not None and lon is not None:
                        if abs(lat) > 60 and abs(lon) < 60:
                            fl['lat'], fl['lon'] = lon, lat
                    flights.append(fl)

                meta_key = Config.REDIS_LIVE_FLIGHTS_META_KEY
                meta = await r.hgetall(meta_key) if await r.exists(meta_key) else {}
                return {"focus": None, "flights": flights, "meta": meta}
    except Exception as e:
        logger.warning(f"Redis fetch_live_flights error, falling back to DB: {e}")
    
    # Fallback to PostgreSQL
    async with db_pool.acquire() as conn:
        query = "SELECT hexid, callsign, lat, lon, alt, speed, heading FROM flights_in_air WHERE lat IS NOT NULL AND lon IS NOT NULL"
        params, focus = [], None
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            query += f" AND callsign LIKE ${len(params)}"
        if airport and airport != 'ALL':
            lat, lon = await get_airport_coords(conn, airport)
            if lat and lon:
                focus = {"lat": lat, "lon": lon}
                params.extend([lat, lon])
                query += f" AND abs(lat - ${len(params)-1}) < 1.5 AND abs(lon - ${len(params)}) < 1.5"
        rows = await conn.fetch(query, *params)
        flights = []
        for r in rows:
            fl = dict(r)
            # Fix swapped lat/lon from binCraft decoder bug (lat should be ~6-37 for India, lon ~68-98)
            lat, lon = fl.get('lat'), fl.get('lon')
            if lat is not None and lon is not None:
                if abs(lat) > 60 and abs(lon) < 60:
                    fl['lat'], fl['lon'] = lon, lat
            flights.append(fl)
        return {"focus": focus, "flights": flights}

async def fetch_congestion_heatmap(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT ROUND(lat::numeric * 2) / 2 AS lat_grid, ROUND(lon::numeric * 2) / 2 AS lon_grid, COUNT(*) as density FROM flights_in_air WHERE lat IS NOT NULL GROUP BY lat_grid, lon_grid HAVING COUNT(*) > 1")
        return [dict(r) for r in rows]

async def fetch_atc_stats(db_pool, airline, airport):
    async with db_pool.acquire() as conn:
        query = "SELECT COUNT(*) as active, COALESCE(AVG(speed), 0) as spd, COALESCE(AVG(alt), 0) as alt FROM flights_in_air WHERE lat IS NOT NULL"
        params = []
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            query += f" AND callsign LIKE ${len(params)}"
        if airport and airport != 'ALL':
            lat, lon = await get_airport_coords(conn, airport)
            if lat and lon:
                params.extend([lat, lon])
                query += f" AND abs(lat - ${len(params)-1}) < 1.5 AND abs(lon - ${len(params)}) < 1.5"
        row = await conn.fetchrow(query, *params)
        return dict(row)

async def fetch_altitude_bands(db_pool, airline, airport):
    async with db_pool.acquire() as conn:
        query = "SELECT CASE WHEN alt < 10000 THEN 'Approach (<10k)' WHEN alt < 20000 THEN 'Terminal (10k-20k)' ELSE 'Enroute (>20k)' END as band, COUNT(*) as count FROM flights_in_air WHERE lat IS NOT NULL"
        params = []
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            query += f" AND callsign LIKE ${len(params)}"
        if airport and airport != 'ALL':
            lat, lon = await get_airport_coords(conn, airport)
            if lat and lon:
                params.extend([lat, lon])
                query += f" AND abs(lat - ${len(params)-1}) < 1.5 AND abs(lon - ${len(params)}) < 1.5"
        query += " GROUP BY band"
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

async def fetch_live_anomalies(db_pool, airport, airline):
    async with db_pool.acquire() as conn:
        query = "SELECT TO_CHAR(timestamp, 'HH24:MI') as time, callsign, hex_id, airport, anomaly_flag FROM flight_events WHERE anomaly_flag IS NOT NULL AND anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED' AND timestamp >= NOW() - INTERVAL '12 hours'"
        params = []
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            query += f" AND airport = ${len(params)}"
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            query += f" AND callsign LIKE ${len(params)}"
        query += " ORDER BY timestamp DESC LIMIT 10"
        rows = await conn.fetch(query, *params)
        return [{**dict(r), "airport_display": fmt_apt(r['airport'])} for r in rows]

async def fetch_tarmac_squatters(db_pool, airport, airline):
    async with db_pool.acquire() as conn:
        params, where_clauses = [], []
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            where_clauses.append(f"airport = ${len(params)}")
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            where_clauses.append(f"current_callsign LIKE ${len(params)}")
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        rows = await conn.fetch(f"SELECT airport, current_callsign as callsign, hex_id, TO_CHAR(landed_at, 'HH24:MI') as landed_time, ROUND(EXTRACT(EPOCH FROM (NOW() - landed_at))/60) as mins FROM ground_ops {where_sql} ORDER BY mins DESC LIMIT 10", *params)
        return [{**dict(r), "airport_display": fmt_apt(r['airport'])} for r in rows]

async def fetch_turnarounds(db_pool, airport, airline):
    async with db_pool.acquire() as conn:
        params = []
        where_clauses = ["d.timestamp >= NOW() - INTERVAL '48 hours'"]
        lateral_cond = "TRIM(a.hex_id) = TRIM(d.hex_id) AND TRIM(a.airport) = TRIM(d.airport) AND a.timestamp < d.timestamp AND a.timestamp > d.timestamp - INTERVAL '24 hours'"
        
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            where_clauses.append(f"TRIM(d.airport) = ${len(params)}")
        
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            idx = len(params)
            where_clauses.append(f"(TRIM(d.callsign) LIKE ${idx} OR TRIM(a.callsign) LIKE ${idx})")
            
        where_sql = " AND ".join(where_clauses)
        if where_sql: where_sql = f"WHERE {where_sql}"
            
        query = f"""
            SELECT SUBSTRING(TRIM(d.callsign) FROM '^[A-Z]+') AS airline_code, 
                   AVG(EXTRACT(EPOCH FROM (d.timestamp - a.timestamp))/60) AS avg_turnaround_mins 
            FROM departures_log d 
            JOIN LATERAL (
                SELECT timestamp, callsign FROM arrivals_log a 
                WHERE {lateral_cond} ORDER BY a.timestamp DESC LIMIT 1
            ) a ON true 
            {where_sql}
            GROUP BY airline_code HAVING COUNT(*) > 0 ORDER BY avg_turnaround_mins ASC
        """
        try:
            rows = await conn.fetch(query, *params)
            return [{"airline": r['airline_code'], "airline_display": fmt_aln(r['airline_code']), "time": float(r['avg_turnaround_mins'])} for r in rows if r.get('airline_code')]
        except Exception as e:
            logger.error(f"❌ SQL ERROR IN TURNAROUNDS: {e}")
            return []

async def fetch_drilldown_turnaround(db_pool, target_airline, airport):
    async with db_pool.acquire() as conn:
        params = []
        where_clauses = ["d.timestamp >= NOW() - INTERVAL '48 hours'"]
        lateral_cond = "TRIM(a.hex_id) = TRIM(d.hex_id) AND TRIM(a.airport) = TRIM(d.airport) AND a.timestamp < d.timestamp AND a.timestamp > d.timestamp - INTERVAL '24 hours'"
        
        params.append(f"{target_airline.upper()}%")
        idx_al = len(params)
        where_clauses.append(f"(TRIM(d.callsign) LIKE ${idx_al} OR TRIM(a.callsign) LIKE ${idx_al})")
        
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            where_clauses.append(f"TRIM(d.airport) = ${len(params)}")
            
        where_sql = " AND ".join(where_clauses)
        if where_sql: where_sql = f"WHERE {where_sql}"
            
        query = f"""
            SELECT TRIM(d.hex_id) as hex_id, TRIM(a.callsign) AS landing_callsign, TRIM(d.callsign) AS takeoff_callsign, 
                   TO_CHAR(a.timestamp, 'YYYY-MM-DD HH24:MI') AS landing_time, 
                   TO_CHAR(d.timestamp, 'YYYY-MM-DD HH24:MI') AS takeoff_time, 
                   ROUND(EXTRACT(EPOCH FROM (d.timestamp - a.timestamp))/60) AS turnaround_mins
            FROM departures_log d 
            JOIN LATERAL (
                SELECT callsign, timestamp FROM arrivals_log a 
                WHERE {lateral_cond} ORDER BY a.timestamp DESC LIMIT 1
            ) a ON true 
            {where_sql} ORDER BY turnaround_mins ASC LIMIT 100
        """
        try:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"❌ SQL ERROR IN DRILLDOWN: {e}")
            return []

async def fetch_runway_demand(db_pool, airport):
    async with db_pool.acquire() as conn:
        params, ap_sql = [], ""
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            ap_sql = f"AND airport = ${len(params)}"
        rows = await conn.fetch(f"SELECT TO_CHAR(timestamp, 'HH24:00') as hour_bucket, COUNT(*) as arrivals FROM arrivals_log WHERE timestamp >= NOW() - INTERVAL '24 hours' AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR') {ap_sql} GROUP BY hour_bucket ORDER BY hour_bucket ASC", *params)
        return [dict(r) for r in rows]

async def fetch_fleet_utilization(db_pool, airline):
    async with db_pool.acquire() as conn:
        params, al_sql = [], ""
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            al_sql = "AND callsign LIKE $1"
        rows = await conn.fetch(f"""
            WITH combined_events AS (
                SELECT hex_id, callsign, timestamp, 'ARRIVAL' as event_type 
                FROM arrivals_log WHERE timestamp >= NOW() - INTERVAL '8 days' AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR')
                UNION ALL
                SELECT hex_id, callsign, timestamp, 'DEPARTURE' as event_type 
                FROM departures_log WHERE timestamp >= NOW() - INTERVAL '8 days' AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR')
            ),
            sequenced_events AS (
                SELECT hex_id, callsign, timestamp as event_time, event_type,
                    LEAD(event_type) OVER (PARTITION BY hex_id ORDER BY timestamp) as next_event_type,
                    LEAD(timestamp) OVER (PARTITION BY hex_id ORDER BY timestamp) as next_event_time
                FROM combined_events
            )
            SELECT hex_id, COUNT(*) as flights, ROUND(COALESCE(SUM(EXTRACT(EPOCH FROM (next_event_time - event_time))/3600), 0)::numeric, 1) as hours_flown
            FROM sequenced_events WHERE event_type = 'DEPARTURE' AND next_event_type = 'ARRIVAL' AND event_time >= NOW() - INTERVAL '7 days' {al_sql}
            GROUP BY hex_id HAVING COUNT(*) > 0 ORDER BY hours_flown DESC LIMIT 500
        """, *params)
        return [{"hex": r['hex_id'], "flights": r['flights'], "hours": float(r['hours_flown'])} for r in rows]

async def fetch_otp_data(db_pool, airport, airline):
    async with db_pool.acquire() as conn:
        params, where_clauses = [], ["actual_time IS NOT NULL", "direction = 'ARRIVALS'", "scheduled_time >= NOW() - INTERVAL '24 hours'"]
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            where_clauses.append(f"airport_code = ${len(params)}")
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            where_clauses.append(f"callsign LIKE ${len(params)}")
        where_sql = " AND ".join(where_clauses)
        rows = await conn.fetch(f"SELECT SUBSTRING(callsign FROM 1 FOR 3) as airline, AVG(EXTRACT(EPOCH FROM (actual_time - scheduled_time))/60) as avg_delay_mins FROM flight_schedules WHERE {where_sql} GROUP BY airline HAVING COUNT(*) > 0 ORDER BY avg_delay_mins DESC", *params)
        return [{"airline": r['airline'], "airline_display": fmt_aln(r['airline']), "delay": float(r['avg_delay_mins'])} for r in rows]

async def fetch_airport_schedules(db_pool, airport, direction, target_date):
    async with db_pool.acquire() as conn:
        icao = await resolve_airport_codes(conn, airport)
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        is_today = not target_date or target_date == today_str
        
        if not is_today:
            rows = await conn.fetch(f"""
                SELECT flight_number, callsign, hex_id, route_airport, anomaly_flag, TO_CHAR(scheduled_time, 'YYYY-MM-DD HH24:MI') as sched_time, TO_CHAR(actual_time, 'YYYY-MM-DD HH24:MI') as act_time
                FROM flight_schedules WHERE airport_code = $1 AND direction = $2 
                  AND ((scheduled_time >= TO_TIMESTAMP($3, 'YYYY-MM-DD') AND scheduled_time < TO_TIMESTAMP($3, 'YYYY-MM-DD') + INTERVAL '1 day')
                      OR (scheduled_time IS NULL AND actual_time >= TO_TIMESTAMP($3, 'YYYY-MM-DD') AND actual_time < TO_TIMESTAMP($3, 'YYYY-MM-DD') + INTERVAL '1 day'))
                ORDER BY COALESCE(scheduled_time, actual_time) ASC LIMIT 1500
            """, icao, direction.upper(), target_date)
        else:
            rows = await conn.fetch(f"""
                SELECT flight_number, callsign, hex_id, route_airport, anomaly_flag, TO_CHAR(scheduled_time, 'YYYY-MM-DD HH24:MI') as sched_time, TO_CHAR(actual_time, 'YYYY-MM-DD HH24:MI') as act_time
                FROM flight_schedules WHERE airport_code = $1 AND direction = $2 
                  AND (scheduled_time >= NOW() - INTERVAL '4 hours' AND scheduled_time <= NOW() + INTERVAL '14 hours'
                      OR actual_time >= NOW() - INTERVAL '4 hours' OR (scheduled_time IS NULL AND actual_time >= NOW() - INTERVAL '4 hours'))
                ORDER BY COALESCE(scheduled_time, actual_time) ASC LIMIT 500
            """, icao, direction.upper())
            
        results = []
        for r in rows:
            d = dict(r)
            d['route_airport'] = normalize_ap(d['route_airport'])
            d['route_airport_display'] = fmt_apt(d['route_airport'])
            if not d['callsign']: d['callsign'] = d['flight_number']
            if not d['sched_time']:
                d['sched_time'] = '---'
                d['remark'] = 'UNSCHEDULED'
            else:
                d['remark'] = 'PLANNED'
            results.append(d)
        return results

async def fetch_airport_logs(db_pool, airport, direction, target_date):
    async with db_pool.acquire() as conn:
        icao = await resolve_airport_codes(conn, airport)
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        is_today = not target_date or target_date == today_str
        table = "arrivals_log" if direction.upper() == 'ARRIVALS' else "departures_log"
        route_col = "origin" if direction.upper() == 'ARRIVALS' else "destination"

        if not is_today:
            rows = await conn.fetch(f"SELECT callsign, hex_id, {route_col} as route_airport, anomaly_flag, runway, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI') as act_time FROM {table} WHERE airport = $1 AND timestamp >= TO_TIMESTAMP($2, 'YYYY-MM-DD') AND timestamp < TO_TIMESTAMP($2, 'YYYY-MM-DD') + INTERVAL '1 day' ORDER BY timestamp DESC LIMIT 1000", icao, target_date)
        else:
            rows = await conn.fetch(f"SELECT callsign, hex_id, {route_col} as route_airport, anomaly_flag, runway, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI') as act_time FROM {table} WHERE airport = $1 AND timestamp >= NOW() - INTERVAL '24 hours' ORDER BY timestamp DESC LIMIT 500", icao)
            
        results = []
        for r in rows:
            d = dict(r)
            d['route_airport'] = normalize_ap(d['route_airport'])
            d['route_airport_display'] = fmt_apt(d['route_airport'])
            d['sched_time'] = None 
            d['remark'] = 'PHYSICAL_LOG' 
            results.append(d)
        return results

async def fetch_safety_index(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT TO_CHAR(DATE(timestamp), 'Mon DD') as date, COUNT(*) as incidents FROM flight_events WHERE anomaly_flag IS NOT NULL AND anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED' AND timestamp >= NOW() - INTERVAL '14 days' GROUP BY DATE(timestamp) ORDER BY DATE(timestamp) ASC")
        return [dict(r) for r in rows]

async def fetch_top_routes(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT origin, destination, COUNT(*) as flights FROM flight_events WHERE event_type = 'DEPARTED' AND origin IS NOT NULL AND destination IS NOT NULL AND (anomaly_flag IS NULL OR (anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED')) GROUP BY origin, destination ORDER BY flights DESC")
        merged_routes = {}
        for r in rows:
            o = normalize_ap(r['origin'])
            d = normalize_ap(r['destination'])
            key = f"{o}|{d}"
            if key not in merged_routes: merged_routes[key] = 0
            merged_routes[key] += r['flights']
        sorted_routes = sorted(merged_routes.items(), key=lambda x: x[1], reverse=True)[:7]
        results = []
        for k, v in sorted_routes:
            o, d = k.split('|')
            results.append({"origin": o, "destination": d, "flights": v, "origin_display": fmt_apt(o), "destination_display": fmt_apt(d)})
        return results

async def fetch_cdo_efficiency(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT SUBSTRING(a.callsign FROM 1 FOR 3) as airline, AVG(EXTRACT(EPOCH FROM (a.timestamp - e.timestamp))/60) as approach_mins FROM arrivals_log a JOIN flight_events e ON a.hex_id = e.hex_id AND e.event_type = 'APPROACHING' AND e.timestamp < a.timestamp AND e.timestamp > a.timestamp - INTERVAL '1 hour' WHERE a.timestamp >= NOW() - INTERVAL '7 days' AND (a.anomaly_flag IS NULL OR (a.anomaly_flag != 'SYSTEM_ERROR' AND a.anomaly_flag != 'AI_ENRICHED')) GROUP BY airline HAVING COUNT(*) > 5 ORDER BY approach_mins ASC LIMIT 20")
        return [{"airline": r['airline'], "airline_display": fmt_aln(r['airline']), "time": float(r['approach_mins'])} for r in rows]

async def fetch_unscheduled_arrivals(db_pool, airport):
    async with db_pool.acquire() as conn:
        params, ap_sql = [], ""
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            ap_sql = f"AND a.airport = ${len(params)}"
        rows = await conn.fetch(f"SELECT a.callsign, a.hex_id, a.airport, TO_CHAR(a.timestamp, 'HH24:MI') as time FROM arrivals_log a LEFT JOIN flight_schedules s ON a.callsign = s.callsign AND DATE(a.timestamp) = DATE(s.scheduled_time) WHERE a.timestamp >= NOW() - INTERVAL '24 hours' AND s.callsign IS NULL AND (a.anomaly_flag IS NULL OR (a.anomaly_flag != 'SYSTEM_ERROR' AND a.anomaly_flag != 'AI_ENRICHED')) {ap_sql} ORDER BY a.timestamp DESC LIMIT 8", *params)
        results = []
        for r in rows:
            d = dict(r)
            d['airport'] = normalize_ap(d['airport'])
            d['airport_display'] = fmt_apt(d['airport'])
            results.append(d)
        return results

async def fetch_training_activity(db_pool, airport):
    async with db_pool.acquire() as conn:
        params, ap_sql = [], ""
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            ap_sql = f"AND airport = ${len(params)}"
        rows = await conn.fetch(f"SELECT airport, COUNT(*) as tg_count FROM flight_events WHERE event_type = 'TOUCH_AND_GO' AND timestamp >= NOW() - INTERVAL '7 days' AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR') {ap_sql} GROUP BY airport ORDER BY tg_count DESC LIMIT 5", *params)
        results = []
        for r in rows:
            d = dict(r)
            d['airport'] = normalize_ap(d['airport'])
            d['airport_display'] = fmt_apt(d['airport'])
            results.append(d)
        return results

async def fetch_drilldown_otp(db_pool, target_airline, airport):
    async with db_pool.acquire() as conn:
        params, where_clauses = [], ["actual_time IS NOT NULL", "direction = 'ARRIVALS'", "scheduled_time >= NOW() - INTERVAL '24 hours'"]
        params.append(f"{target_airline.upper()}%")
        where_clauses.append(f"callsign LIKE ${len(params)}")
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            where_clauses.append(f"airport_code = ${len(params)}")
        where_sql = " AND ".join(where_clauses)
        rows = await conn.fetch(f"SELECT hex_id, callsign, route_airport, TO_CHAR(scheduled_time, 'YYYY-MM-DD HH24:MI') as sched_time, TO_CHAR(actual_time, 'YYYY-MM-DD HH24:MI') as act_time, ROUND(EXTRACT(EPOCH FROM (actual_time - scheduled_time))/60) as delay_mins FROM flight_schedules WHERE {where_sql} ORDER BY delay_mins DESC LIMIT 100", *params)
        results = []
        for r in rows:
            d = dict(r)
            d['route_airport'] = normalize_ap(d['route_airport'])
            d['route_airport_display'] = fmt_apt(d['route_airport'])
            results.append(d)
        return results

async def fetch_drilldown_fleet(db_pool, hex_id):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(f"""
            WITH combined_events AS (
                SELECT hex_id, callsign, airport, timestamp, 'ARRIVAL' as event_type 
                FROM arrivals_log WHERE hex_id = $1 AND timestamp >= NOW() - INTERVAL '8 days' AND (anomaly_flag IS NULL OR (anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED'))
                UNION ALL
                SELECT hex_id, callsign, airport, timestamp, 'DEPARTURE' as event_type 
                FROM departures_log WHERE hex_id = $1 AND timestamp >= NOW() - INTERVAL '8 days' AND (anomaly_flag IS NULL OR (anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED'))
            ),
            sequenced_events AS (
                SELECT event_type, callsign, timestamp as dep_time, airport as origin_airport,
                    LEAD(event_type) OVER (PARTITION BY callsign ORDER BY timestamp) as next_event_type,
                    LEAD(airport) OVER (PARTITION BY callsign ORDER BY timestamp) as dest_airport,
                    LEAD(timestamp) OVER (PARTITION BY callsign ORDER BY timestamp) as arr_time
                FROM combined_events
            )
            SELECT callsign, origin_airport, dest_airport, TO_CHAR(dep_time, 'YYYY-MM-DD HH24:MI') as dep_time, TO_CHAR(arr_time, 'YYYY-MM-DD HH24:MI') as arr_time, ROUND(EXTRACT(EPOCH FROM (arr_time - dep_time))/60) as duration_mins
            FROM sequenced_events WHERE event_type = 'DEPARTURE' AND next_event_type = 'ARRIVAL' ORDER BY dep_time DESC LIMIT 50
        """, hex_id.lower())
        results = []
        for r in rows:
            d = dict(r)
            d['origin_airport'] = normalize_ap(d['origin_airport'])
            d['dest_airport'] = normalize_ap(d['dest_airport'])
            d['origin_display'] = fmt_apt(d['origin_airport'])
            d['dest_display'] = fmt_apt(d['dest_airport'])
            results.append(d)
        return results

async def fetch_drilldown_safety(db_pool, target_date):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT TO_CHAR(timestamp, 'HH24:MI') as time, callsign, hex_id, airport, anomaly_flag, details FROM flight_events WHERE anomaly_flag IS NOT NULL AND anomaly_flag != 'SYSTEM_ERROR' AND anomaly_flag != 'AI_ENRICHED' AND TO_CHAR(DATE(timestamp), 'Mon DD') = $1 ORDER BY timestamp DESC LIMIT 100", target_date)
        return [dict(r) for r in rows]

async def fetch_drilldown_cdo(db_pool, target_airline):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT a.callsign, a.hex_id, a.airport, TO_CHAR(a.timestamp, 'YYYY-MM-DD HH24:MI') as landing_time, ROUND(EXTRACT(EPOCH FROM (a.timestamp - e.timestamp))/60) as approach_mins FROM arrivals_log a JOIN flight_events e ON a.hex_id = e.hex_id AND e.event_type = 'APPROACHING' AND e.timestamp < a.timestamp AND e.timestamp > a.timestamp - INTERVAL '1 hour' WHERE a.timestamp >= NOW() - INTERVAL '7 days' AND SUBSTRING(a.callsign FROM 1 FOR 3) = $1 AND (a.anomaly_flag IS NULL OR (a.anomaly_flag != 'SYSTEM_ERROR' AND a.anomaly_flag != 'AI_ENRICHED')) ORDER BY a.timestamp DESC LIMIT 100", target_airline.upper())
        results = []
        for r in rows:
            d = dict(r)
            d['airport'] = normalize_ap(d['airport'])
            d['airport_display'] = fmt_apt(d['airport'])
            results.append(d)
        return results

async def fetch_drilldown_route(db_pool, origin, destination):
    async with db_pool.acquire() as conn:
        o_icao = await resolve_airport_codes(conn, origin)
        d_icao = await resolve_airport_codes(conn, destination)
        o_iata = Config.ICAO_TO_IATA.get(o_icao)
        d_iata = Config.ICAO_TO_IATA.get(d_icao)
        if not o_iata:
            o_iatas = [k for k, v in IATA_TO_ICAO_MEM.items() if v == o_icao]
            o_iata = o_iatas[0] if o_iatas else o_icao
        if not d_iata:
            d_iatas = [k for k, v in IATA_TO_ICAO_MEM.items() if v == d_icao]
            d_iata = d_iatas[0] if d_iatas else d_icao

        rows = await conn.fetch("SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI') as time, callsign, hex_id FROM flight_events WHERE event_type = 'DEPARTED' AND (origin = $1 OR origin = $2) AND (destination = $3 OR destination = $4) AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR') ORDER BY timestamp DESC LIMIT 100", o_icao, o_iata, d_icao, d_iata)
        return [dict(r) for r in rows]

async def fetch_drilldown_demand(db_pool, hour_bucket, airport):
    async with db_pool.acquire() as conn:
        params, ap_sql = [hour_bucket], ""
        if airport and airport != 'ALL':
            icao = await resolve_airport_codes(conn, airport)
            params.append(icao)
            ap_sql = f"AND airport = ${len(params)}"
        rows = await conn.fetch(f"SELECT TO_CHAR(timestamp, 'HH24:MI') as time, callsign, hex_id, origin, runway, airport FROM arrivals_log WHERE timestamp >= NOW() - INTERVAL '24 hours' AND TO_CHAR(timestamp, 'HH24:00') = $1 AND (anomaly_flag IS NULL OR anomaly_flag != 'SYSTEM_ERROR') {ap_sql} ORDER BY timestamp DESC LIMIT 100", *params)
        results = []
        for r in rows:
            d = dict(r)
            d['origin'] = normalize_ap(d['origin'])
            d['airport'] = normalize_ap(d['airport'])
            d['origin_display'] = fmt_apt(d['origin'])
            d['airport_display'] = fmt_apt(d['airport'])
            results.append(d)
        return results
    
async def fetch_drilldown_altitude(db_pool, band, airline, airport):
    async with db_pool.acquire() as conn:
        if '<10k' in band: alt_condition = "alt < 10000"
        elif '10k-20k' in band: alt_condition = "alt >= 10000 AND alt < 20000"
        else: alt_condition = "alt >= 20000"
        query = f"SELECT hexid as hex_id, callsign, alt, speed, heading FROM flights_in_air WHERE lat IS NOT NULL AND {alt_condition}"
        params = []
        if airline and airline != 'ALL':
            params.append(f"{airline.upper()}%")
            query += f" AND callsign LIKE ${len(params)}"
        if airport and airport != 'ALL':
            lat, lon = await get_airport_coords(conn, airport)
            if lat and lon:
                params.extend([lat, lon])
                query += f" AND abs(lat - ${len(params)-1}) < 1.5 AND abs(lon - ${len(params)}) < 1.5"
        query += " ORDER BY alt DESC LIMIT 100"
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

async def fetch_ai_enrichment_ledger(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT TO_CHAR(timestamp, 'Mon DD, HH24:MI') as time, hex_id, callsign, original_value, ai_inferred_value, confidence_score, ai_reasoning, target_table FROM ai_enrichment_audit ORDER BY timestamp DESC LIMIT 100")
        return [dict(r) for r in rows]

async def fetch_ai_insights_feed(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT TO_CHAR(timestamp, 'Mon DD, HH24:MI') as time, insight_type, trigger_event, insight_text, target_airport FROM ai_insights_log ORDER BY timestamp DESC LIMIT 50")
        return [dict(r) for r in rows]

async def fetch_flight_ai_audit(db_pool, hex_id, callsign):
    if not hex_id and not callsign: return []
    async with db_pool.acquire() as conn:
        params, where_clauses = [], []
        if hex_id and hex_id != 'null':
            params.append(hex_id)
            where_clauses.append(f"LOWER(hex_id) = LOWER(${len(params)})")
        if callsign and callsign != 'null':
            params.append(callsign)
            where_clauses.append(f"LOWER(callsign) = LOWER(${len(params)})")
        where_sql = " OR ".join(where_clauses)
        rows = await conn.fetch(f"SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI') as time, original_value, ai_inferred_value, ai_reasoning, confidence_score FROM ai_enrichment_audit WHERE {where_sql} ORDER BY timestamp DESC LIMIT 5", *params)
        return [dict(r) for r in rows]

async def fetch_telemetry_track(influx_query_api, hex_id):
    if not influx_query_api: return {"error": "InfluxDB not connected."}
    query = f'''
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "flight_path" and r.hex_id == "{hex_id.lower()}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
    '''
    try:
        tables = await influx_query_api.query(query)
        results = []
        for table in tables:
            for record in table.records:
                results.append({
                    "time": record.get_time().strftime('%H:%M:%S'),
                    "lat": record.values.get("lat"),
                    "lon": record.values.get("lon"),
                    "alt": record.values.get("alt"),
                    "speed": record.values.get("speed")
                })
        return results
    except Exception as e:
        logger.error(f"❌ [INFLUX ERROR] Query failed: {e}")
        return []