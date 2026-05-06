import asyncio
import asyncpg
import aiohttp
import math
import re
import csv
import os
import redis.asyncio as redis
from datetime import datetime, timedelta
from typing import Optional
from mcp.server.fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from config import Config
from bot_router_mcp_client import CURRENT_CHAT_ID, CURRENT_SESSION_ID

# Import delay predictor for NLP delay queries
import delay_predictor as dp_module

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize FastMCP Server


DB_POOL = None
REDIS_POOL = None

@lifespan
async def init_mcp_db(server):
    global DB_POOL, REDIS_POOL
    print("🚀 Starting MCP Server and connecting to databases...")
    DB_POOL = await asyncpg.create_pool(**Config.DB_PARAMS)
    try:
        redis_config = getattr(Config, 'REDIS_PARAMS', {"host": "127.0.0.1", "port": 6379, "db": 0, "decode_responses": True})
        REDIS_POOL = redis.Redis(**redis_config)
        await REDIS_POOL.ping()
    except Exception as e:
        print(f"⚠️ Redis disabled on MCP Server: {e}")
        REDIS_POOL = None
    load_airlines_server()
    yield
    if DB_POOL: await DB_POOL.close()
    if REDIS_POOL: await REDIS_POOL.aclose()

# Initialize FastMCP Server
app = FastMCP("raga-radar-backend", lifespan=init_mcp_db)

class AirlineMapper:
    """Encapsulates airline and airport code mappings with caching."""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self.AIRLINE_MAP = {}
        self.IATA_TO_ICAO = {}
        self.SPOKEN_TO_ICAO = {}
        self._initialized = True
        self.load_mappings()
    
    def load_mappings(self):
        """Load airline mappings from CSV file."""
        if os.path.exists(Config.AIRLINES_FILE):
            with open(Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    if str(row.get('Active', 'Y')).strip().upper() != 'N':
                        icao = row.get('ICAO', '').strip().upper()
                        iata = row.get('IATA', '').strip().upper()
                        name = row.get('Name', '').strip().upper()
                        
                        if icao and len(icao) == 3 and icao not in ['N/A', '\\N']:
                            self.AIRLINE_MAP[icao] = name
                            if iata and len(iata) == 2 and iata not in ['N/A', '\\N', '-']:
                                self.IATA_TO_ICAO[iata] = icao
                            if name:
                                clean_spoken = name.replace(" AIRLINES", "").replace(" AIRWAYS", "").replace(" AIR", "").replace(" LIMITED", "").replace(" CORPORATION", "").strip().replace(" ", "")
                                if clean_spoken:
                                    self.SPOKEN_TO_ICAO[clean_spoken] = icao

# Global mapper instance
airline_mapper = AirlineMapper()

def load_airlines_server():
    AIRLINE_MAP = airline_mapper.AIRLINE_MAP
    IATA_TO_ICAO = airline_mapper.IATA_TO_ICAO
    SPOKEN_TO_ICAO = airline_mapper.SPOKEN_TO_ICAO
    if os.path.exists(Config.AIRLINES_FILE):
        with open(Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                if str(row.get('Active', 'Y')).strip().upper() != 'N':
                    icao = row.get('ICAO', '').strip().upper()
                    iata = row.get('IATA', '').strip().upper()
                    name = row.get('Name', '').strip().upper()
                    if icao and len(icao) == 3 and icao not in ['N/A', '\\N']:
                        AIRLINE_MAP[icao] = name
                        if iata and len(iata) == 2 and iata not in ['N/A', '\\N', '-']:
                            IATA_TO_ICAO[iata] = icao
                        if name:
                            clean_spoken = name.replace(" AIRLINES", "").replace(" AIRWAYS", "").replace(" AIR", "").replace(" LIMITED", "").replace(" CORPORATION", "").strip().replace(" ", "")
                            if clean_spoken: SPOKEN_TO_ICAO[clean_spoken] = icao

# --- UTILITIES ---
async def normalize_callsign(callsign):
    if not callsign: return None
    clean = callsign.upper().replace(" ", "").strip()
    cache_key = f"alias:{clean}"
    if REDIS_POOL:
        try:
            cached = await REDIS_POOL.get(cache_key)
            if cached: return cached
        except: pass

    icao_variant = clean
    for spoken, icao in airline_mapper.SPOKEN_TO_ICAO.items():
        if icao_variant.startswith(spoken):
            icao_variant = icao_variant.replace(spoken, icao, 1)
            break
             
    for iata, icao in airline_mapper.IATA_TO_ICAO.items():
        if icao_variant.startswith(iata) and len(icao_variant) > len(iata) and icao_variant[len(iata)].isdigit():
            icao_variant = icao_variant.replace(iata, icao, 1)
            break

    match = re.match(r"([A-Z0-9]+)(\d+)([A-Z]*)", icao_variant)
    final_icao = f"{match.group(1)}{int(match.group(2))}{match.group(3)}" if match else icao_variant

    if REDIS_POOL:
        try: await REDIS_POOL.setex(cache_key, 14400, final_icao)
        except: pass
    return final_icao

def calculate_haversine(lat1, lon1, lat2, lon2):
    R = 3440.065 
    dLat, dLon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dLat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def resolve_to_icao(code):
    if not code: return None
    code = code.upper().strip()
    for icao, data in Config.TARGET_AIRPORTS.items():
        if code == icao or code == data.get('iata', '') or code == data.get('name', '').upper():
            return icao
    return code

async def _get_cached_route(conn, callsign):
    if not REDIS_POOL:
        return None, None
    try:
        cache_key = f"route:{callsign.upper()}"
        cached = await REDIS_POOL.get(cache_key)
        if cached:
            import orjson
            data = orjson.loads(cached)
            return data.get('origin'), data.get('dest')
    except Exception as e:
        logger.warning(f"Cache read failed for {callsign}: {e}")
    return None, None

async def _set_cached_route(conn, callsign, origin, dest):
    if not REDIS_POOL or not origin or not dest:
        return
    try:
        import orjson
        import time
        cache_key = f"route:{callsign.upper()}"
        cache_data = orjson.dumps({'origin': origin, 'dest': dest, 'cached_at': time.time()})
        await REDIS_POOL.setex(cache_key, 86400, cache_data)  # 24hr TTL
    except Exception as e:
        logger.warning(f"Cache write failed for {callsign}: {e}")

async def _get_unified_eta_and_dest(conn, target_cs, c_lat, c_lon, c_alt, c_spd):
    norm_cs = await normalize_callsign(target_cs)
    dest_code, origin_code, dest_lat, dest_lon, dest_from_web = None, None, None, None, False
    
    # 0. CHECK REDIS CACHE FIRST
    cached_origin, cached_dest = await _get_cached_route(conn, norm_cs)
    if cached_origin and cached_dest:
        return None, cached_dest, cached_origin, False
    
    # 1. TRY FLIGHTRADAR24 (PRIMARY) - with rate limiting delay
    try:
        from utils import get_iata_from_icao_fr24
        async with aiohttp.ClientSession() as session:
            iata_code, iata_flight, operator = await get_iata_from_icao_fr24(norm_cs, session)
            if iata_flight:
                # Use IATA flight number for adsbdb lookup (adsbdb accepts both ICAO and IATA)
                async with aiohttp.ClientSession() as adsb_session:
                    async with adsb_session.get(f"https://api.adsbdb.com/v0/callsign/{iata_flight}", timeout=8) as r:
                        if r.status == 200:
                            d = (await r.json()).get("response", {}).get("flightroute", {})
                            origin_code = resolve_to_icao(d.get("origin", {}).get("icao_code") or d.get("origin", {}).get("iata_code", ""))
                            dest_code = resolve_to_icao(d.get("destination", {}).get("icao_code") or d.get("destination", {}).get("iata_code", ""))
                            if origin_code and dest_code:
                                await _set_cached_route(conn, norm_cs, origin_code, dest_code)
                                logger.info(f"✈️ FR24→adsbdb resolved {norm_cs} ({iata_flight}): {origin_code} -> {dest_code}")
    except Exception as e:
        logger.warning(f"FR24 resolution failed for {norm_cs}: {e}")

    # 2. FALLBACK: adsbdb with ICAO callsign
    if not dest_code or not origin_code:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.adsbdb.com/v0/callsign/{norm_cs}", timeout=8) as r:
                    if r.status == 200:
                        d = (await r.json()).get("response", {}).get("flightroute", {})
                        origin_code = resolve_to_icao(d.get("origin", {}).get("icao_code") or d.get("origin", {}).get("iata_code", ""))
                        dest_code = resolve_to_icao(d.get("destination", {}).get("icao_code") or d.get("destination", {}).get("iata_code", ""))
                        # Cache successful response
                        if origin_code and dest_code:
                            await _set_cached_route(conn, norm_cs, origin_code, dest_code)
                        if d.get("destination", {}).get("latitude"):
                            dest_lat, dest_lon = float(d["destination"]["latitude"]), float(d["destination"]["longitude"])
                            dest_from_web = True
        except: pass

    if not dest_code or not origin_code:
        sched = await conn.fetchrow("""
            SELECT route_airport, airport_code, direction FROM flight_schedules 
            WHERE (callsign = $1 OR flight_number = $1) 
            AND scheduled_time >= NOW() - INTERVAL '12 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours'
            ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
        """, norm_cs)
        if sched: 
            if not dest_code: dest_code = resolve_to_icao(sched['airport_code'] if sched['direction'] == 'ARRIVALS' else sched['route_airport'])
            if not origin_code: origin_code = resolve_to_icao(sched['route_airport'] if sched['direction'] == 'ARRIVALS' else sched['airport_code'])
            # Cache DB result if valid and nothing cached yet
            if dest_code and origin_code:
                await _set_cached_route(conn, norm_cs, origin_code, dest_code)

    if dest_code and not dest_lat:
        clean_dest = dest_code.strip().upper()
        for icao, ap_data in Config.TARGET_AIRPORTS.items():
            if icao == clean_dest or ap_data.get('iata', '') == clean_dest:
                dest_lat, dest_lon = float(ap_data['lat']), float(ap_data['lon'])
                break

    eta_mins = None
    if dest_lat and dest_lon and c_spd > 0:
        dist_nm = calculate_haversine(c_lat, c_lon, dest_lat, dest_lon)
        live_mins = (dist_nm / c_spd) * 60
        hist_buffer_mins = 15
        if dest_code:
            try:
                avg_app_row = await conn.fetchrow("""
                    SELECT AVG(EXTRACT(EPOCH FROM (a.timestamp - e.timestamp)))/60 as avg_mins 
                    FROM arrivals_log a JOIN flight_events e ON a.hex_id = e.hex_id AND e.event_type = 'APPROACHING' AND e.airport = a.airport 
                    WHERE a.airport = $1 AND a.timestamp > e.timestamp AND a.timestamp < e.timestamp + INTERVAL '1 hour'
                """, dest_code.strip().upper())
                if avg_app_row and avg_app_row['avg_mins']: hist_buffer_mins = max(5, min(45, int(avg_app_row['avg_mins'])))
            except: pass
        if dist_nm > 50: eta_mins = int(live_mins * 1.10) + hist_buffer_mins
        elif dist_nm > 15: eta_mins = int(live_mins) + int(hist_buffer_mins * ((dist_nm - 15) / 35.0))
        else: eta_mins = int(live_mins)
            
    return eta_mins, dest_code, origin_code, dest_from_web

async def _fetch_flight_status_logic(callsign_raw: str, depth: int = 0) -> str:
    if depth > 1: return ""
    raw = callsign_raw.upper().replace(" ", "").strip()
    norm = await normalize_callsign(raw)
    clean_raw = raw.strip()
    clean_norm = norm.strip() if norm else None
    
    try:
        async with DB_POOL.acquire() as conn:
            air = await conn.fetchrow("""
                SELECT hexid, lat, lon, alt, speed, heading 
                FROM flights_in_air 
                WHERE callsign = $1 OR callsign = $2 
                ORDER BY last_seen DESC LIMIT 1
            """, clean_raw, clean_norm)

            live_from_web = False
            if not air:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"https://api.adsb.lol/v2/callsign/{norm}", timeout=5) as r:
                            if r.status == 200:
                                d = await r.json()
                                if d.get("ac", []):
                                    ac = d["ac"][0]
                                    air = {'hexid': ac.get('hex', 'UNKNOWN'), 'lat': ac.get('lat', 0.0), 'lon': ac.get('lon', 0.0), 'alt': max(ac.get('alt_baro', 0) or 0, ac.get('alt_geom', 0) or 0), 'speed': ac.get('gs', 0) or 0, 'heading': ac.get('track', 0) or 0}
                                    live_from_web = True
                except: pass

            if air:
                hexid = air.get('hexid', 'UNKNOWN')
                lat, lon, speed, alt = float(air.get('lat') or 0.0), float(air.get('lon') or 0.0), int(air.get('speed') or 0), max(0, int(air.get('alt') or 0))
                eta_mins, dest_code, origin_code, dest_from_web = await _get_unified_eta_and_dest(conn, raw, lat, lon, alt, speed)
                
                alt_str = f"{alt:,}ft" if alt > 0 else ("Data Pending (In-Air)" if speed > 50 else "0ft (Ground)")
                m = f"🛰️ <b>Callsign:</b> {raw} | <b>Hex ID:</b> {hexid}\n"
                if lat != 0.0: m += f"📍 <code>{lat:.4f}, {lon:.4f}</code> | 📏 {alt_str}\n"
                m += f"🚀 {speed}kts | 🧭 {air.get('heading') or 0}°\n"
                if live_from_web: m += f"🌍 <i>(Live telemetry fetched from Global Network - out of local range)</i>\n"
                if origin_code or dest_code: m += f"🏁 <b>Route:</b> {origin_code or '???'} ➔ {dest_code or '???'}\n"
                
                if eta_mins is not None:
                    if eta_mins > 300: m += f"⏳ <b>ETA:</b> Calculating...\n"
                    else:
                        hrs, mins = eta_mins // 60, eta_mins % 60
                        eta_str = f"~{hrs}h {mins}m" if hrs > 0 else f"~{mins}m"
                        m += f"⏳ <b>ETA:</b> {eta_str} <i>(🌐 Dest {dest_code.strip().upper()} fetched from web)</i>\n" if dest_from_web else f"⏳ <b>ETA:</b> {eta_str}\n"
                else:
                    m += f"⏳ <b>ETA:</b> Unknown (Destination coordinates not filed)\n"
                return m + f"🔗 <a href='https://adsb.lol/?icao={hexid}'>Track on ADSB.lol</a>"

            # Check Ground Ops / History Fallbacks
            ground = await conn.fetchrow("""
                SELECT airport, origin, landed_at FROM ground_ops 
                WHERE TRIM(current_callsign) = $1 OR TRIM(current_callsign) = $2 OR TRIM(inbound_callsign) = $1 OR TRIM(inbound_callsign) = $2
                AND landed_at >= NOW() - INTERVAL '24 hours' ORDER BY landed_at DESC LIMIT 1
            """, raw, norm)

            if ground: return f"🅿️ <b>Ground Status:</b>\nFlight <b>{raw}</b> is currently parked at {ground['airport']}.\n✅ <b>Landed:</b> {ground['landed_at'].strftime('%H:%M')}"
            return f"❌ No live or historical data found for {raw} in the last 24 hours."
    except Exception as e: return f"⚠️ DB Error: {e}"


# ==========================================
# 🌟 EXPOSED MCP TOOLS
# ==========================================

# =======================================================
# 🌟 EXPOSED TOOLS (With Pydantic-Safe Optional Typing)
# =======================================================

@app.tool()
async def get_flight_status(callsign_raw: str) -> str:
    """Telemetry, route, ETA, Ground Ops, Schedule Delay & adsb.lol link for a live flight."""
    return await _fetch_flight_status_logic(callsign_raw)

@app.tool()
async def get_unified_airport_timetable(airport: str, board_type: str = "DEPARTURES", time_modifier: str = "all", partner_airport: Optional[str] = None, airline_code: Optional[str] = None) -> str:
    """Unified chronological timetable stitching past logs and future schedules."""
    icao = resolve_to_icao(airport)
    partner_icao = resolve_to_icao(partner_airport) if partner_airport else None
    
    now = datetime.now()
    start_time, end_time = now - timedelta(hours=2), now + timedelta(hours=6)
    
    tm = (time_modifier or "all").lower()
    is_tomorrow = "tomorrow" in tm
    base_date = now + timedelta(days=1) if is_tomorrow else now
    
    if "morning" in tm:
        start_time, end_time = base_date.replace(hour=6, minute=0, second=0), base_date.replace(hour=11, minute=59, second=59)
    elif "afternoon" in tm:
        start_time, end_time = base_date.replace(hour=12, minute=0, second=0), base_date.replace(hour=16, minute=59, second=59)
    elif "evening" in tm:
        start_time, end_time = base_date.replace(hour=17, minute=0, second=0), base_date.replace(hour=20, minute=59, second=59)
    elif "night" in tm:
        start_time = base_date.replace(hour=21, minute=0, second=0)
        end_time = (base_date + timedelta(days=1)).replace(hour=5, minute=59, second=59)
    elif "today" in tm or is_tomorrow:
        start_time, end_time = base_date.replace(hour=0, minute=0, second=0), base_date.replace(hour=23, minute=59, second=59)
        
    direction = 'DEPARTURES' if 'DEP' in (board_type or "DEPARTURES").upper() else 'ARRIVALS'
    
    try:
        async with DB_POOL.acquire() as conn:
            params = [icao, start_time, end_time]
            
            partner_sql = ""
            if partner_icao:
                params.append(partner_icao)
                if direction == 'DEPARTURES': partner_sql = f" AND destination = ${len(params)}"
                else: partner_sql = f" AND origin = ${len(params)}"
                    
            airline_sql = ""
            if airline_code:
                params.append(f"{airline_code.upper()}%")
                airline_sql = f" AND callsign LIKE ${len(params)}"

            log_table = "departures_log" if direction == 'DEPARTURES' else "arrivals_log"
            partner_col = "destination" if direction == 'DEPARTURES' else "origin"
            status_str = "Departed" if direction == 'DEPARTURES' else "Landed"
            
            query_past = f"""
                SELECT callsign, {partner_col} as partner, timestamp, '{status_str}' as status, runway 
                FROM {log_table} 
                WHERE airport = $1 AND timestamp >= $2 AND timestamp <= $3 {partner_sql} {airline_sql.replace("callsign", "callsign")}
            """
            
            sched_partner_sql = ""
            if partner_icao:
                sched_partner_sql = f" AND route_airport = ${params.index(partner_icao) + 1}"
            sched_airline_sql = ""
            if airline_code:
                sched_airline_sql = f" AND (callsign LIKE ${params.index(f'{airline_code.upper()}%') + 1} OR flight_number LIKE ${params.index(f'{airline_code.upper()}%') + 1})"
                
            query_future = f"""
                SELECT COALESCE(NULLIF(callsign, ''), flight_number) as callsign, route_airport as partner, scheduled_time as timestamp, 'Scheduled' as status, NULL::text as runway 
                FROM flight_schedules 
                WHERE airport_code = $1 AND direction = '{direction}' 
                AND scheduled_time >= $2 AND scheduled_time <= $3 
                AND scheduled_time > NOW()
                {sched_partner_sql} {sched_airline_sql}
            """
            
            full_query = f"({query_past}) UNION ALL ({query_future}) ORDER BY timestamp ASC LIMIT 30"
            rows = await conn.fetch(full_query, *params)
            
            if not rows:
                p_str = f" to/from {partner_icao}" if partner_icao else ""
                return f"No {direction.lower()} found for {icao}{p_str} in the requested time window ({tm})."
            
            title_icon = "🛫" if direction == 'DEPARTURES' else "🛬"
            p_str = f" (Route: {partner_icao})" if partner_icao else ""
            msg = f"{title_icon} <b>{icao} Timetable - {direction.title()}</b> {p_str} | Window: {tm.title()}\n\n"
            
            for r in rows:
                t_str = r['timestamp'].strftime('%H:%M')
                status_icon = "✅" if r['status'] in ['Landed', 'Departed'] else "⏳"
                rwy = f" [RWY {r['runway']}]" if r['runway'] else ""
                partner_str = f" ({'To' if direction=='DEPARTURES' else 'From'}: {r['partner'] or 'UNK'})" if not partner_icao else ""
                msg += f"• {status_icon} <b>{t_str}</b> | {r['callsign']}{partner_str} - <i>{r['status']}</i>{rwy}\n"
                
            return msg.strip()
    except Exception as e:
        return f"⚠️ Error generating timetable: {e}"

@app.tool()
async def get_inbound_aircraft_status(departing_callsign: str) -> str:
    """Finds the incoming physical aircraft that will operate a scheduled departing flight."""
    norm, raw = await normalize_callsign(departing_callsign), departing_callsign.upper().replace(" ", "").strip()
    try:
        async with DB_POOL.acquire() as conn:
            is_airborne = await conn.fetchrow("SELECT hexid FROM flights_in_air WHERE TRIM(callsign) = $1 OR TRIM(callsign) = $2 LIMIT 1", raw, norm)
            if is_airborne:
                status_text = await _fetch_flight_status_logic(departing_callsign)
                return f"✈️ <b>Flight {raw} is currently active and airborne!</b>\n\n{status_text}"
            
            inbound_cs, assignment_method = None, ""
            sched = await conn.fetchrow("""
                SELECT hex_id, airport_code, scheduled_time 
                FROM flight_schedules 
                WHERE (callsign = $1 OR flight_number = $1) AND direction = 'DEPARTURES' 
                AND scheduled_time >= NOW() - INTERVAL '12 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours' 
                AND hex_id IS NOT NULL 
                ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
            """, norm)
            
            if sched and sched['hex_id']:
                hex_assigned = sched['hex_id']
                inbound_sched = await conn.fetchrow("""
                    SELECT callsign FROM flight_schedules 
                    WHERE LOWER(hex_id) = LOWER($1) AND airport_code = $2 AND direction = 'ARRIVALS' 
                    AND scheduled_time <= $3 
                    ORDER BY scheduled_time DESC LIMIT 1
                """, hex_assigned, sched['airport_code'], sched['scheduled_time'])
                
                if inbound_sched and inbound_sched['callsign']:
                    inbound_cs, assignment_method = inbound_sched['callsign'], "📡 <b>Confirmed via Official Airline Schedule (Same Aircraft)</b>"
                else:
                    air = await conn.fetchrow("SELECT callsign FROM flights_in_air WHERE LOWER(hexid) = LOWER($1)", hex_assigned)
                    if air and air['callsign']: inbound_cs, assignment_method = air['callsign'], "📡 <b>Confirmed via Live Radar (Airborne Aircraft)</b>"
                    else:
                        ground = await conn.fetchrow("SELECT current_callsign FROM ground_ops WHERE LOWER(hex_id) = LOWER($1) ORDER BY landed_at DESC LIMIT 1", hex_assigned)
                        if ground and ground['current_callsign']: inbound_cs, assignment_method = ground['current_callsign'], "📡 <b>Confirmed via Live Schedule (Already on Ground)</b>"
                        else:
                            arr = await conn.fetchrow("SELECT callsign FROM arrivals_log WHERE LOWER(hex_id) = LOWER($1) ORDER BY timestamp DESC LIMIT 1", hex_assigned)
                            if arr: inbound_cs, assignment_method = arr['callsign'], "📡 <b>Confirmed via Live Schedule (Recently Arrived)</b>"
            
            if not inbound_cs: return f"⚠️ Could not determine the inbound connecting aircraft for {departing_callsign}. (Aircraft Hex ID not bound to schedule yet)."
            status_text = await _fetch_flight_status_logic(inbound_cs)
            return f"🔄 <b>Connecting Flight Identified:</b>\nThe physical aircraft for your flight <b>{departing_callsign}</b> is operating as <b>{inbound_cs}</b>.\n{assignment_method}\n\n<b>Live Status of Aircraft:</b>\n{status_text}"
    except Exception as e: return f"⚠️ Error finding connecting flight: {e}"

@app.tool()
async def set_flight_alert(callsign: str, alert_type: str, threshold_mins: int = 0) -> str:
    """Sets a proactive notification for a user."""
    chat_id = CURRENT_CHAT_ID.get()
    session_id = CURRENT_SESSION_ID.get()
    
    logger.info(f"🚨 BACKEND TRIGGERED: set_flight_alert called for {callsign} with type {alert_type}")
    
    clean_cs = await normalize_callsign(callsign)
    if not clean_cs: return "Invalid callsign provided."
    
    try:
        async with DB_POOL.acquire() as conn:
            if not chat_id or chat_id == 0:
                if not getattr(Config, 'ENABLE_WEB_NOTIFICATIONS', False):
                    return "⚠️ <b>Alerts are currently Telegram-exclusive!</b>\n\nBecause I need to push live notifications to your device when the aircraft approaches, this feature is only available on our Telegram bot. Please message me there to set up a tracker."
                
                sub = await conn.fetchrow("SELECT sub_data FROM web_subscriptions WHERE session_id = $1", session_id)
                if not sub or not sub['sub_data']:
                    return "⚠️ <b>Browser Notifications Not Enabled!</b>\n\nI need permission to send you popups. Please click the <b>'🔔 Enable Web Alerts'</b> button in the chat header to grant browser permissions before setting an alert."
                
                await conn.execute("INSERT INTO user_alerts (chat_id, session_id, target_callsign, alert_type, threshold_mins, status) VALUES (0, $1, $2, $3, $4, 'ACTIVE')", session_id, clean_cs, alert_type.upper(), threshold_mins)
            else:
                await conn.execute("INSERT INTO user_alerts (chat_id, target_callsign, alert_type, threshold_mins, status) VALUES ($1, $2, $3, $4, 'ACTIVE')", chat_id, clean_cs, alert_type.upper(), threshold_mins)
        
        if alert_type.upper() == 'ETA_WARNING': return f"✅ <b>Alert Configured!</b>\nI will keep a close eye on the radar and notify you the moment <b>{clean_cs}</b> is approximately {threshold_mins} minutes away from landing."
        elif alert_type.upper() == 'CONNECTING_ETA': return f"✅ <b>Connecting Flight ETA Configured!</b>\nI will monitor the incoming aircraft for <b>{clean_cs}</b> and notify you when it is {threshold_mins} minutes away."
        elif alert_type.upper() == 'CONNECTING_LANDED': return f"✅ <b>Connecting Flight Watchdog Configured!</b>\nI will monitor the incoming aircraft for <b>{clean_cs}</b> and instantly message you the moment its wheels touch the ground."
        else: return f"✅ <b>Landing Alert Configured!</b>\nI will instantly message you the exact moment <b>{clean_cs}</b> safely touches down."
    except Exception as e: 
        logger.error(f"⚠️ DB INSERT FAILED: {e}")
        return f"⚠️ Failed to set alert: {e}"

@app.tool()
async def get_airframe_history(identifier: str) -> str:
    """Trace daily multi-hop history & turnaround times for a callsign or Hex ID."""
    norm, hex_id, raw = await normalize_callsign(identifier), identifier.lower().strip(), identifier.upper().replace(" ", "").strip()
    try:
        async with DB_POOL.acquire() as conn:
            if len(identifier) > 6 or not all(c in '0123456789abcdef' for c in hex_id):
                row = await conn.fetchrow("SELECT hex_id FROM flight_events WHERE TRIM(callsign) = $1 OR TRIM(callsign) = $2 ORDER BY timestamp DESC LIMIT 1", raw, norm)
                if row: hex_id = row['hex_id'].lower()
                else: return f"No history found for {identifier}."
            msg = f"📖 <b>Daily Log for Hex ID: <code>{hex_id}</code></b>\n"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://api.adsbdb.com/v0/aircraft/{hex_id}", timeout=2) as r:
                        if r.status == 200:
                            ac_data = (await r.json()).get("response", {}).get("aircraft", {})
                            msg += f"✈️ <b>Aircraft:</b> {ac_data.get('type', 'Unknown')} ({ac_data.get('registration', 'Unknown')})\n"
            except: pass
            
            events = await conn.fetch("""
                SELECT 'ARRIVED' as type, timestamp, airport as loc, callsign, runway 
                FROM arrivals_log 
                WHERE LOWER(hex_id) = LOWER($1) AND timestamp > NOW() - INTERVAL '24 hours' 
                UNION ALL 
                SELECT 'DEPARTED' as type, timestamp, airport as loc, callsign, runway 
                FROM departures_log 
                WHERE LOWER(hex_id) = LOWER($1) AND timestamp > NOW() - INTERVAL '24 hours' 
                UNION ALL 
                SELECT 'TOUCH_AND_GO' as type, timestamp, airport as loc, callsign, runway 
                FROM flight_events 
                WHERE event_type = 'TOUCH_AND_GO' AND LOWER(hex_id) = LOWER($1) AND timestamp > NOW() - INTERVAL '24 hours' 
                ORDER BY timestamp ASC
            """, hex_id)
            
            if not events: return msg + "No recorded history in the last 24 hours."
            last_arr_time, last_arr_loc = None, None
            for e in events:
                ts = e['timestamp'].strftime('%H:%M')
                rwy_str = f" [RWY {e.get('runway')}]" if e.get('runway') else ""
                if e['type'] == 'ARRIVED':
                    msg += f"🛬 <b>{ts}</b> - Landed at {e['loc']}{rwy_str} (as {e['callsign']})\n"
                    last_arr_time, last_arr_loc = e['timestamp'], e['loc']
                elif e['type'] == 'TOUCH_AND_GO': msg += f"🛬🛫 <b>{ts}</b> - Touch & Go at {e['loc']}{rwy_str} (as {e['callsign']})\n"
                else:
                    turnaround = f" <i>(Turnaround: {int((e['timestamp'] - last_arr_time).total_seconds() / 60)} mins)</i>" if last_arr_time and last_arr_loc == e['loc'] else ""
                    msg += f"🛫 <b>{ts}</b> - Departed {e['loc']}{rwy_str} (as {e['callsign']}){turnaround}\n"
            return msg
    except: return "⚠️ DB error fetching history."

@app.tool()
async def predict_flight_assignment(future_callsign: str) -> str:
    """Predict which aircraft will operate a future flight based on the enriched schedule."""
    norm, raw = await normalize_callsign(future_callsign), future_callsign.upper().replace(" ", "").strip()
    try:
        async with DB_POOL.acquire() as conn:
            sched = await conn.fetchrow("""
                SELECT hex_id, airport_code, scheduled_time 
                FROM flight_schedules 
                WHERE (callsign = $1 OR flight_number = $1) AND direction = 'DEPARTURES' 
                AND scheduled_time >= NOW() - INTERVAL '12 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours' 
                AND hex_id IS NOT NULL 
                ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
            """, norm)
            
            if not sched or not sched['hex_id']:
                return f"⚠️ The physical aircraft (Hex ID) has not yet been assigned to {future_callsign} in the schedule."
            
            hex_id = sched['hex_id']
            arr_sched = await conn.fetchrow("""
                SELECT callsign FROM flight_schedules 
                WHERE LOWER(hex_id) = LOWER($1) AND airport_code = $2 AND direction = 'ARRIVALS' 
                AND scheduled_time <= $3 ORDER BY scheduled_time DESC LIMIT 1
            """, hex_id, sched['airport_code'], sched['scheduled_time'])
            
            if not arr_sched or not arr_sched['callsign']:
                return f"⚠️ Aircraft <code>{hex_id}</code> is assigned to {future_callsign}, but its inbound routing is unknown."
                
            inbound_cs = arr_sched['callsign']
            live = await conn.fetchrow("SELECT hexid, alt FROM flights_in_air WHERE TRIM(callsign) = $1", inbound_cs)
            p_msg = f"🔮 <b>Assignment Confirmed for {future_callsign}</b>\nThe schedule guarantees this route is operated by aircraft arriving as <b>{inbound_cs}</b> (Hex: <code>{hex_id}</code>).\n\n"
            if live: p_msg += f"✅ <b>Live Status:</b> {inbound_cs} is IN THE AIR at {live['alt']:,}ft. This plane will operate {future_callsign} next."
            else: p_msg += f"⚠️ <b>Live Status:</b> {inbound_cs} is not currently tracked on live radar."
            return p_msg
    except Exception as e: return f"⚠️ DB error predicting flight: {e}"

@app.tool()
async def get_airport_traffic(code: str) -> str:
    """Aircraft count and detailed list of planes on ground at given ICAO."""
    icao = resolve_to_icao(code)
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT current_callsign, landed_at FROM ground_ops WHERE airport = $1 ORDER BY landed_at DESC", icao)
            msg = f"🅿️ <b>Ground Traffic at {icao}: {len(rows)} aircraft</b>\n"
            for r in rows: msg += f"• {r['current_callsign']} (Since {r['landed_at'].strftime('%H:%M')})\n"
            return msg
    except: return "⚠️ Error fetching roster."

@app.tool()
async def get_airport_turnarounds(airport_code: str, airline_code: Optional[str] = None) -> str:
    """Get a comprehensive list of individual completed flight turnarounds (arrived and then departed)."""
    icao = resolve_to_icao(airport_code)
    try:
        async with DB_POOL.acquire() as conn:
            params, airline_filter_sql = [icao], ""
            if airline_code:
                params.append(f"{airline_code.upper()}%")
                airline_filter_sql = f" AND (d.callsign LIKE ${len(params)} OR a.callsign LIKE ${len(params)})"
            
            query = f"""
                SELECT d.hex_id, d.airport, a.origin, d.destination, a.callsign AS arrival_callsign, 
                d.callsign AS departure_callsign, a.timestamp AS arrival_time, d.timestamp AS departure_time, 
                EXTRACT(EPOCH FROM (d.timestamp - a.timestamp))/60 AS turnaround_mins 
                FROM departures_log d 
                JOIN LATERAL (
                    SELECT origin, callsign, timestamp FROM arrivals_log a 
                    WHERE a.hex_id = d.hex_id AND a.airport = d.airport AND a.airport = $1 
                    AND a.timestamp < d.timestamp ORDER BY a.timestamp DESC LIMIT 1
                ) a ON true 
                WHERE d.airport = $1 AND d.timestamp >= NOW() - INTERVAL '24 hours' {airline_filter_sql} 
                ORDER BY d.timestamp DESC LIMIT 20
            """
            rows = await conn.fetch(query, *params)
            if not rows: return f"No completed turnarounds recorded for airline {airline_code.upper()} at {icao} in the last 24 hours." if airline_code else f"No completed turnarounds recorded at {icao} in the last 24 hours."
            msg = f"🔄 <b>Recent Turnarounds at {icao}</b>\n\n"
            for r in rows:
                msg += f"✈️ <b>Hex:</b> <code>{r['hex_id']}</code> | <b>Turnaround:</b> {int(r['turnaround_mins'])} mins\n🛬 {r['arrival_callsign']} (From: {r['origin'] or 'UNK'}) at {r['arrival_time'].strftime('%H:%M')}\n🛫 {r['departure_callsign']} (To: {r['destination'] or 'UNK'}) at {r['departure_time'].strftime('%H:%M')}\n\n"
            return msg.strip()
    except Exception as e: return f"⚠️ Error fetching turnarounds: {e}"

@app.tool()
async def get_average_turnaround_by_airline(airport_code: str) -> str:
    """Generate a statistical report showing average turnaround times by airline."""
    icao = resolve_to_icao(airport_code)
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("""
                SELECT SUBSTRING(d.callsign FROM '^[A-Z]+') AS airline_code, 
                COUNT(*) AS flight_count, AVG(EXTRACT(EPOCH FROM (d.timestamp - a.timestamp))/60) AS avg_turnaround_mins 
                FROM departures_log d 
                JOIN LATERAL (
                    SELECT timestamp FROM arrivals_log a 
                    WHERE a.hex_id = d.hex_id AND a.airport = d.airport AND a.airport = $1 
                    AND a.timestamp < d.timestamp AND a.timestamp > d.timestamp - INTERVAL '12 hours' 
                    ORDER BY a.timestamp DESC LIMIT 1
                ) a ON true 
                WHERE d.airport = $1 AND d.timestamp >= NOW() - INTERVAL '24 hours' 
                GROUP BY airline_code ORDER BY flight_count DESC
            """, icao)
            
            if not rows: return f"No turnaround data available to calculate averages at {icao} for the last 24 hours."
            msg = f"📊 <b>Average Turnaround Report by Airline at {icao} (Last 24h)</b>\n\n"
            for r in rows:
                acode = r['airline_code'] or 'UNKNOWN'
                aname = AIRLINE_MAP.get(acode, acode)
                display_name = f"{aname} ({acode})" if aname != acode else acode
                msg += f"• <b>{display_name}</b>: {int(r['avg_turnaround_mins'])} mins <i>(based on {r['flight_count']} flights)</i>\n"
            return msg.strip()
    except Exception as e: return f"⚠️ Error calculating average turnarounds: {e}"

@app.tool()
async def get_airport_anomalies(airport_code: str) -> str:
    """Get a report of flight anomalies (go-arounds, diversions, air returns) at a specific airport."""
    icao = resolve_to_icao(airport_code)
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT callsign, event_type, details, anomaly_flag, timestamp FROM flight_events WHERE airport = $1 AND anomaly_flag IS NOT NULL AND timestamp >= NOW() - INTERVAL '24 hours' ORDER BY timestamp DESC", icao)
            if not rows: return f"✅ No anomalies or irregular operations recorded at {icao} in the last 24 hours."
            msg = f"⚠️ <b>Anomaly Report for {icao} (Last 24h)</b>\n\n"
            for r in rows: msg += f"• <b>{r['timestamp'].strftime('%H:%M')}</b> | {r['callsign']} | <b>{r['anomaly_flag']}</b>: {r['details']}\n"
            return msg.strip()
    except Exception as e: return f"⚠️ Error fetching anomalies: {e}"

@app.tool()
async def get_inbound_flights(airport_code: str, origin_airport: Optional[str] = None) -> str:
    """Live inbound flights to airport."""
    icao = resolve_to_icao(airport_code)   
    orig_icao = resolve_to_icao(origin_airport) if origin_airport else None
    
    # 1. 🌟 NEW: Look up the Destination Coordinates ONCE
    dest_lat, dest_lon = None, None
    for ap_icao, ap_data in Config.TARGET_AIRPORTS.items():
        if ap_icao == icao or ap_data.get('iata', '') == icao:
            dest_lat, dest_lon = float(ap_data['lat']), float(ap_data['lon'])
            break
    
    try:
        async with DB_POOL.acquire() as conn:
            # 2. 🌟 NEW: Calculate the historical traffic buffer ONCE for the whole airport
            hist_buffer_mins = 15
            try:
                avg_app_row = await conn.fetchrow("""
                    SELECT AVG(EXTRACT(EPOCH FROM (a.timestamp - e.timestamp)))/60 as avg_mins 
                    FROM arrivals_log a JOIN flight_events e ON a.hex_id = e.hex_id AND e.event_type = 'APPROACHING' AND e.airport = a.airport 
                    WHERE a.airport = $1 AND a.timestamp > e.timestamp AND a.timestamp < e.timestamp + INTERVAL '1 hour'
                """, icao)
                if avg_app_row and avg_app_row['avg_mins']: 
                    hist_buffer_mins = max(5, min(45, int(avg_app_row['avg_mins'])))
            except: pass

            # 3. Fetch the active flights
            query = """
                SELECT f.callsign, f.hexid, f.alt, f.lat, f.lon, f.speed, e.origin 
                FROM flights_in_air f 
                JOIN (
                    SELECT DISTINCT ON (hex_id) hex_id, origin, destination 
                    FROM flight_events WHERE destination IS NOT NULL 
                    ORDER BY hex_id, timestamp DESC
                ) e ON f.hexid = e.hex_id 
                WHERE e.destination = $1
            """
            
            if orig_icao:
                query += " AND e.origin = $2"
                rows = await conn.fetch(query, icao, orig_icao)
            else:
                rows = await conn.fetch(query, icao)
            
            orig_str = f" from {orig_icao}" if orig_icao else ""
            if not rows: return f"No active inbound flights heading to {icao}{orig_str}."
            
            msg = f"🛬 <b>Active Inbounds to {icao}{orig_str}: {len(rows)} aircraft</b>\n"
            
            # 4. 🌟 NEW: Calculate ETAs in memory instantly without any DB or API calls!
            for r in rows: 
                eta_mins = None
                if dest_lat and dest_lon and r['speed'] and float(r['speed']) > 0:
                    dist_nm = calculate_haversine(float(r['lat'] or 0), float(r['lon'] or 0), dest_lat, dest_lon)
                    live_mins = (dist_nm / float(r['speed'])) * 60
                    
                    if dist_nm > 50:
                        eta_mins = int(live_mins * 1.10) + hist_buffer_mins
                    elif dist_nm > 15:
                        buffer_multiplier = (dist_nm - 15) / 35.0
                        eta_mins = int(live_mins) + int(hist_buffer_mins * buffer_multiplier)
                    else:
                        eta_mins = int(live_mins)

                eta_str = f" | ETA: ~{eta_mins}m" if eta_mins else ""
                msg += f"• <b>{r['callsign']}</b> (From: {r['origin'] or 'UNK'}) | Alt: {r['alt']:,}ft{eta_str}\n"
                
            return msg
    except Exception as e: return f"⚠️ Error checking inbounds: {e}"

@app.tool()
async def get_route_status_board(origin: str, destination: str) -> str:
    """Get a complete daily timeline (landed, airborne, departed, scheduled) for flights between two cities."""
    orig_icao = resolve_to_icao(origin)
    dest_icao = resolve_to_icao(destination)
    
    try:
        async with DB_POOL.acquire() as conn:
            msg = f"📊 <b>Daily Route Dashboard: {orig_icao} ➔ {dest_icao}</b>\n\n"
            
            landed = await conn.fetch("""
                SELECT callsign, timestamp, runway 
                FROM arrivals_log 
                WHERE airport = $2 AND origin = $1 AND timestamp >= NOW() - INTERVAL '24 hours'
                ORDER BY timestamp DESC
            """, orig_icao, dest_icao)
            if landed:
                msg += "✅ <b>Already Landed</b>\n"
                for r in landed:
                    rwy_str = f" [RWY {r['runway']}]" if r.get('runway') else ""
                    #msg += f"• {r['timestamp'].strftime('%H:%M')} | <b>{r['callsign']}</b>{rwy_str}\n"
                    msg += f"• {r['timestamp'].strftime('%d %b %H:%M')} | <b>{r['callsign']}</b>{rwy_str}\n"

                msg += "\n"

            airborne = await conn.fetch("""
                SELECT f.callsign, f.lat, f.lon, f.alt, f.speed 
                FROM flights_in_air f 
                JOIN (
                    SELECT DISTINCT ON (hex_id) hex_id, origin, destination 
                    FROM flight_events WHERE destination IS NOT NULL 
                    ORDER BY hex_id, timestamp DESC
                ) e ON f.hexid = e.hex_id 
                WHERE e.origin = $1 AND e.destination = $2
            """, orig_icao, dest_icao)
            if airborne:
                msg += "✈️ <b>Currently Airborne (Live Radar)</b>\n"
                for r in airborne:
                    eta_mins, _, _, _ = await _get_unified_eta_and_dest(conn, r['callsign'], float(r['lat'] or 0), float(r['lon'] or 0), float(r['alt'] or 0), float(r['speed'] or 0))
                    eta_str = f" | ETA: ~{eta_mins}m" if eta_mins else ""
                    msg += f"• <b>{r['callsign']}</b> | Alt: {r['alt']:,}ft{eta_str}\n"
                msg += "\n"

            pending = await conn.fetch("""
                SELECT callsign, timestamp 
                FROM departures_log 
                WHERE airport = $1 AND destination = $2 AND timestamp >= NOW() - INTERVAL '12 hours'
                AND hex_id NOT IN (SELECT hex_id FROM arrivals_log WHERE airport = $2 AND timestamp >= NOW() - INTERVAL '12 hours')
                AND hex_id NOT IN (SELECT hexid FROM flights_in_air)
                ORDER BY timestamp DESC
            """, orig_icao, dest_icao)
            if pending:
                msg += "📡 <b>Departed (Out of Radar Range)</b>\n"
                for r in pending:
                    msg += f"• <b>{r['callsign']}</b> | Departed at {r['timestamp'].strftime('%d %b %H:%M')}\n"
                msg += "\n"

            scheduled = await conn.fetch("""
                SELECT flight_number, callsign, scheduled_time 
                FROM flight_schedules 
                WHERE airport_code = $1 AND direction = 'DEPARTURES' AND route_airport = $2 
                AND actual_time IS NULL 
                AND scheduled_time >= NOW() - INTERVAL '2 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours'
                ORDER BY scheduled_time ASC
            """, orig_icao, dest_icao)
            if scheduled:
                msg += "⏳ <b>Upcoming / Scheduled</b>\n"
                for r in scheduled:
                    cs_str = f" <i>(Live: {r['callsign']})</i>" if r['callsign'] and r['callsign'] != r['flight_number'] else ""
                    msg += f"• {r['scheduled_time'].strftime('%d %b %H:%M')} | <b>{r['flight_number']}</b>{cs_str}\n"
                msg += "\n"

            if not landed and not airborne and not pending and not scheduled:
                msg += "<i>No activity recorded or scheduled for this route today.</i>"

            return msg.strip()
    except Exception as e: return f"⚠️ Error generating route board: {e}"

@app.tool()
async def get_airspace_pulse() -> str:
    """Get global stats on currently tracked airspace."""
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) as c, AVG(speed) as s, AVG(alt) as a FROM flights_in_air")
            return f"🌐 <b>Airspace Pulse</b>\nTracking <b>{row['c']}</b> active flights.\nAvg Speed: {int(row['s'] or 0)}kts | Avg Altitude: {int(row['a'] or 0)}ft."
    except: return "⚠️ Error fetching pulse."

@app.tool()
async def get_system_health() -> str:
    """Raspberry Pi 4 Hardware Stats."""
    import psutil
    cpu = "N/A"
    if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f: cpu = f"{round(int(f.read())/1000,1)}°C"
    return f"🌡️ <b>CPU:</b> {cpu} | 🧠 <b>RAM:</b> {psutil.virtual_memory().percent}% | ⚡ <b>Load:</b> {psutil.cpu_percent()}%"

# ============================================================
# 🌟 NEW: Delay Prediction & NLP Query Tools
# ============================================================

@app.tool()
async def get_delay_prediction(callsign: str = "", origin: str = "", destination: str = "", airport_code: str = "") -> str:
    """Predict delay for a flight, route, or airport. Use when user asks about delays, lateness, or on-time performance."""
    try:
        predictor = dp_module.DelayPredictor(DB_POOL)
        
        # Parse inputs
        cs = callsign.strip().upper() if callsign else None
        orig = origin.strip().upper() if origin else None
        dest = destination.strip().upper() if destination else None
        ap = airport_code.strip().upper() if airport_code else None
        
        # Normalize callsign
        if cs:
            # Try to resolve via airline mapper
            airline_prefix = cs[:3] if len(cs) >= 3 else ""
            digits = "".join(filter(str.isdigit, cs))
            if airline_prefix in airline_mapper.IATA_TO_ICAO and digits:
                cs = f"{airline_mapper.IATA_TO_ICAO[airline_prefix]}{digits}"
        
        result = await predictor.predict_delay(
            callsign=cs,
            origin=orig,
            destination=dest,
            airport_code=ap
        )
        
        delay_mins = result['predicted_delay_minutes']
        confidence = result['confidence']
        status = result['status']
        factors = result['factors']
        
        # Format response
        status_emoji = {"ON_TIME": "✅", "SLIGHT_DELAY": "⚠️", "DELAYED": "🔶", "SIGNIFICANT_DELAY": "🔴"}
        emoji = status_emoji.get(status, "❓")
        
        msg = f"{emoji} <b>Delay Prediction</b>\n\n"
        
        if cs:
            msg += f"<b>Flight:</b> {cs}\n"
        if orig and dest:
            msg += f"<b>Route:</b> {orig} → {dest}\n"
        if ap and not (orig and dest):
            msg += f"<b>Airport:</b> {ap}\n"
        
        msg += f"<b>Predicted Delay:</b> {delay_mins} minutes\n"
        msg += f"<b>Status:</b> {status.replace('_', ' ')}\n"
        msg += f"<b>Confidence:</b> {confidence}\n"
        
        if factors and factors != ['baseline']:
            msg += f"\n<b>Factors:</b>\n"
            for f in factors:
                msg += f"• {f.replace(':', ': ')}\n"
        
        msg += "\n<i>Note: This is a traffic-based estimate. Real delays may vary.</i>"
        
        return msg
    except Exception as e:
        logger.error(f"Delay prediction error: {e}")
        return f"⚠️ Error calculating delay prediction: {str(e)}"

@app.tool()
async def get_airline_delay_stats(airline_code: str = "", limit: int = 10) -> str:
    """Get airline on-time performance and delay rankings. Use when user asks about airline punctuality or which airlines are most delayed."""
    try:
        predictor = dp_module.DelayPredictor(DB_POOL)
        
        # Normalize airline code
        al_code = airline_code.strip().upper() if airline_code else None
        if al_code and len(al_code) == 2 and al_code in airline_mapper.IATA_TO_ICAO:
            al_code = airline_mapper.IATA_TO_ICAO[al_code]
        
        results = await predictor.get_airline_otp(airline=al_code, limit=limit)
        
        if not results:
            return "📊 <b>Airline Performance</b>\n\nNo data available for the specified airline."
        
        if al_code:
            # Single airline query
            r = results[0]
            status_emoji = {"EXCELLENT": "🟢", "GOOD": "🟢", "FAIR": "🟡", "POOR": "🔴"}
            emoji = status_emoji.get(r['status'], "⚪")
            
            msg = f"{emoji} <b>Airline Performance: {r['airline']}</b>\n\n"
            msg += f"<b>Avg Delay:</b> {r['avg_delay_minutes']} minutes\n"
            msg += f"<b>Status:</b> {r['status']}\n"
            msg += f"<b>Sample Size:</b> {r['sample_size']} flights (30 days)\n"
            msg += "\n<i>Note: Based on traffic volume heuristic. Real OTP data coming with B1.</i>"
            return msg
        else:
            # Ranking query
            msg = "📊 <b>Airline Delay Rankings</b> (Worst → Best)\n\n"
            for i, r in enumerate(results, 1):
                status_emoji = {"EXCELLENT": "🟢", "GOOD": "🟢", "FAIR": "🟡", "POOR": "🔴"}
                emoji = status_emoji.get(r['status'], "⚪")
                msg += f"{i}. {emoji} <b>{r['airline']}</b> | {r['avg_delay_minutes']}min avg | {r['status']}\n"
            msg += "\n<i>Based on traffic volume estimates. Lower delay = better performance.</i>"
            return msg
    except Exception as e:
        logger.error(f"Airline delay stats error: {e}")
        return f"⚠️ Error fetching airline stats: {str(e)}"

@app.tool()
async def get_route_delay_stats(origin: str = "", destination: str = "", limit: int = 10) -> str:
    """Get route delay statistics. Use when user asks about delays on a specific route or worst/best routes."""
    try:
        predictor = dp_module.DelayPredictor(DB_POOL)
        
        orig = origin.strip().upper() if origin else None
        dest = destination.strip().upper() if destination else None
        
        results = await predictor.get_route_otp(origin=orig, dest=dest, limit=limit)
        
        if not results:
            return "🗺️ <b>Route Performance</b>\n\nNo data available for the specified route."
        
        if orig and dest:
            # Single route query
            r = results[0]
            msg = f"🗺️ <b>Route Performance: {r['route']}</b>\n\n"
            msg += f"<b>Estimated Delay:</b> {r['avg_delay_minutes']} minutes\n"
            msg += f"<b>Traffic Volume:</b> {r['sample_size']} flights (30 days)\n"
            msg += "\n<i>Note: Based on traffic volume heuristic. Real delay data coming with B1.</i>"
            return msg
        else:
            # Ranking query
            msg = "🗺️ <b>Route Delay Rankings</b> (Worst → Best)\n\n"
            for i, r in enumerate(results, 1):
                msg += f"{i}. <b>{r['route']}</b> | {r['avg_delay_minutes']}min avg | {r['sample_size']} flights\n"
            msg += "\n<i>Based on traffic volume estimates. Lower delay = better performance.</i>"
            return msg
    except Exception as e:
        logger.error(f"Route delay stats error: {e}")
        return f"⚠️ Error fetching route stats: {str(e)}"

if __name__ == "__main__":
    app.run()