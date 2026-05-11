# main.py
# v6.13 - Multi-Route Hopper Support & Pre-Flight Landing Guard
import asyncio
import aiohttp
import asyncpg
import redis.asyncio as redis
import orjson
import math
import time
import os
import csv
import logging
import re
import datetime
import sys
import tempfile
import urllib.request
import zstandard as zstandard_lib
import traceback

class ZstdPatch:
    @staticmethod
    def decompress(data):
        dctx = zstandard_lib.ZstdDecompressor()
        return dctx.decompress(data)

sys.modules['zstd'] = ZstdPatch()

if not os.path.exists("binCraft_decoder.py"):
    url = "https://raw.githubusercontent.com/acarsGuy/binCraft-decoder/main/binCraft_decoder.py"
    try:
        with urllib.request.urlopen(url) as response:
            content = response.read().decode('utf-8')
    except Exception as e:
        url = "https://raw.githubusercontent.com/acarsGuy/binCraft-decoder/master/binCraft_decoder.py"
        with urllib.request.urlopen(url) as response:
            content = response.read().decode('utf-8')
            
    with open("binCraft_decoder.py", "w", encoding="utf-8") as f:
        f.write(content)

import binCraft_decoder
from config import Config
from utils import (
    get_iata_from_icao_fr24,
    get_route_from_flightaware,
    get_route_from_adsbdb,
    CALLSIGN_CACHE
)
from db import AsyncDatabaseManager

log_level = logging.DEBUG if getattr(Config, 'DEBUG_MODE', False) else logging.INFO
logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def normalize_callsign(callsign):
    if not callsign: return None
    match = re.match(r"([A-Z]+)(\d+)([A-Z]*)", callsign.upper())
    if match:
        prefix, number, suffix = match.groups()
        return f"{prefix}{int(number)}{suffix}"
    return callsign.upper()

def to_f(val):
    try: return float(val) if val is not None else 0.0
    except (ValueError, TypeError): return 0.0

def extract_field(ac_dict, *fields, default=None):
    """Extract first non-None value from multiple possible field names"""
    for field in fields:
        val = ac_dict.get(field)
        if val is not None:
            return to_f(val) if default is None else to_f(val) or default
    return default if default is not None else 0.0

def extract_lat_lon(ac_dict):
    lat = to_f(ac_dict.get('lat'))
    lon = to_f(ac_dict.get('lon'))
    if lat > 60.0 and lon < 40.0:
        return lon, lat
    return lat, lon

def extract_altitude(data):
    def safe_float(value):
        if isinstance(value, str):
            if value.strip().lower() == 'ground': return 0.0
            try: return float(value.strip())
            except ValueError: return None
        return float(value) if value is not None else None
    
    alt_baro = safe_float(data.get('alt_baro'))
    alt_geom = safe_float(data.get('alt_geom'))
    mcp = safe_float(data.get('nav_altitude_mcp'))
    
    valid_alts = [a for a in (alt_baro, alt_geom) if a is not None]
    
    if valid_alts:
        best_alt = max(valid_alts)
        return max(best_alt, 0.0) 
    else:
        return max(mcp or 0.0, 0.0)

def extract_speed(ac_dict):
    return extract_field(ac_dict, 'gs', 'speed')

def extract_vrate(ac_dict):
    return extract_field(ac_dict, 'baro_rate', 'geom_rate')

def extract_heading(ac_dict):
    return extract_field(ac_dict, 'track', 'heading')

class FlightMonitor:
    def __init__(self, db_pool, redis_client):
        self.tracked_flights = {}
        self.db = AsyncDatabaseManager(db_pool)
        self.redis = redis_client
        self.airline_map = {}
        self.routes_map = {}
        self.airport_icao_to_iata = {}
        self.airport_iata_to_icao = {} 
        self.iata_to_icao = {} 
        self.session = None
        self.runway_data = getattr(Config, 'RUNWAY_DATA', {})
        
        self.radar_queue = asyncio.Queue(maxsize=getattr(Config, 'RADAR_QUEUE_MAXSIZE', 2))
        self.processing_semaphore = asyncio.Semaphore(getattr(Config, 'CONCURRENT_PROCESSING_LIMIT', 100))
        
        # In-memory route cache (fallback when Redis unavailable)
        self.route_memory_cache = {}
        self.route_memory_ttl = 86400  # 24 hours
        
        self.load_airlines()

    async def load_airports_to_redis(self):
        try:
            await self.redis.delete("india_airports")
            for icao_code, ap in Config.TARGET_AIRPORTS.items():
                await self.redis.geoadd("india_airports", (to_f(ap["lon"]), to_f(ap["lat"]), icao_code))
            logger.info(f"✅ Loaded airports into Redis Geospatial Index.")
        except Exception as e:
            logger.error(f"❌ Failed to load airports to Redis Geo-Index: {e}")

    async def get_cached_route(self, callsign):
        try:
            cache_key = f"route:{callsign.upper()}"
            cached = await self.redis.get(cache_key)
            if cached:
                data = orjson.loads(cached)
                return data.get('origin'), data.get('dest')
        except Exception as e:
            logger.warning(f"Route cache read error: {e}")
        return None, None

    async def set_cached_route(self, callsign, origin, dest):
        try:
            cache_key = f"route:{callsign.upper()}"
            cache_data = orjson.dumps({'origin': origin, 'dest': dest, 'cached_at': time.time()})
            await self.redis.setex(cache_key, 86400, cache_data)  # 24hr TTL
            logger.info(f"💾 Cached route for {callsign}: {origin} -> {dest}")
        except Exception as e:
            logger.warning(f"Route cache write error: {e}")
        # Also save to memory cache as backup
        await self.set_memory_route(callsign, origin, dest)

    async def get_memory_route(self, callsign):
        """Get route from in-memory cache (fallback when Redis unavailable)."""
        entry = self.route_memory_cache.get(callsign.upper())
        if entry:
            origin, dest, timestamp = entry
            if time.time() - timestamp < self.route_memory_ttl:
                return origin, dest
            else:
                del self.route_memory_cache[callsign.upper()]
        return None, None

    async def set_memory_route(self, callsign, origin, dest):
        """Set route in in-memory cache."""
        self.route_memory_cache[callsign.upper()] = (origin, dest, time.time())

    async def cleanup_memory_cache(self):
        """Clean up expired entries from memory cache."""
        now = time.time()
        expired = [k for k, (_, _, ts) in self.route_memory_cache.items() if now - ts > self.route_memory_ttl]
        for k in expired:
            del self.route_memory_cache[k]
        if expired:
            logger.info(f"🧹 Cleaned {len(expired)} expired route entries from memory cache")

    async def download_static_data(self):
        os.makedirs("data", exist_ok=True)
        files_to_download = {
            "data/routes.csv": getattr(Config, 'ROUTES_CSV_URL', "https://vrs-standing-data.adsb.lol/routes.csv"),
            "data/airports.csv": getattr(Config, 'AIRPORTS_CSV_URL', "https://vrs-standing-data.adsb.lol/airports.csv")
        }
        for file_path, url in files_to_download.items():
            try:
                async with self.session.get(url, timeout=30) as resp:
                    if resp.status == 200:
                        with open(file_path, 'wb') as f:
                            f.write(await resp.read())
            except Exception as e:
                logger.error(f"❌ Download error for {url}: {e}")

    def load_static_data(self):
        self.airport_icao_to_iata.clear()
        self.airport_iata_to_icao.clear()
        self.routes_map.clear()

        try:
            with open("data/airports.csv", mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    icao = row.get('ICAO', '').strip().upper()
                    iata = row.get('IATA', '').strip().upper()
                    if icao and iata:
                        self.airport_icao_to_iata[icao] = iata
                        self.airport_iata_to_icao[iata] = icao
        except Exception as e:
            logger.error(f"❌ Failed to load local airports.csv: {e}")

        try:
            with open("data/routes.csv", mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    cs = row.get('Callsign', '').strip().upper()
                    route = row.get('AirportCodes', '').strip()
                    if cs and route and '-' in route:
                        # 🌟 MULTI-ROUTE UPGRADE: Store as full array for Sliding Window Logic
                        parts = [p.strip() for p in route.split('-') if p.strip()]
                        if len(parts) >= 2:
                            self.routes_map[cs] = parts
        except Exception as e:
            logger.error(f"❌ Failed to load local routes.csv: {e}")

    async def scheduled_data_updater(self):
        while True:
            try:
                now = datetime.datetime.now()
                t1 = now.replace(hour=9, minute=0, second=0, microsecond=0)
                t2 = now.replace(hour=18, minute=0, second=0, microsecond=0)
                
                if now < t1: next_run = t1
                elif now < t2: next_run = t2
                else: next_run = t1 + datetime.timedelta(days=1)
                    
                sleep_seconds = (next_run - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                
                await self.download_static_data()
                self.load_static_data()
            except Exception as e:
                await asyncio.sleep(60) 

    def get_icao(self, code):
        if not code: return None
        code = code.strip().upper()
        if len(code) == 4 and code.startswith(('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'K', 'L', 'M', 'N', 'O', 'P', 'R', 'S', 'T', 'U', 'V', 'W', 'Y', 'Z')): 
            return code 
        for icao, data in Config.TARGET_AIRPORTS.items():
            if code == icao or code == data.get('iata', ''): return icao
        if code in self.airport_iata_to_icao:
            return self.airport_iata_to_icao[code]
        return code

    def load_airlines(self):
        if os.path.exists(Config.AIRLINES_FILE):
            try:
                with open(Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                    for row in csv.DictReader(f):
                        if str(row.get('Active', 'Y')).strip().upper() != 'N':
                            icao = row.get('ICAO', '').strip()
                            iata = row.get('IATA', '').strip()
                            name = row.get('Name', '').strip()
                            
                            if icao: self.airline_map[icao] = name
                            if iata and icao: self.iata_to_icao[iata] = icao
                                
            except Exception as e: 
                logger.error(f"Failed to load airlines: {e}")

    async def queue_event_redis(self, event_type, flight_data, custom_msg=None):
        payload = {"type": event_type, "data": flight_data, "msg": custom_msg, "time": time.time()}
        channel = f"{Config.APP_NAME}:flight_events"
        await self.redis.publish(channel, orjson.dumps(payload))

    def calculate_distance(self, lat1, lon1, lat2, lon2):
        try:
            lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lon2), float(lon2)
            R = 6371
            dLat, dLon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            a = (math.sin(dLat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon/2)**2)
            return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        except (ValueError, TypeError, ZeroDivisionError):
            return 9999

    def guess_runway_from_heading(self, airport_code, track, tolerance=45):
        icao_code = self.get_icao(airport_code)
        if not icao_code or icao_code not in self.runway_data: return None
        mag_var_w = self.runway_data[icao_code].get('mag_var_w', 0)
        best_rwy = None
        min_diff = 999.0
        
        for rw in self.runway_data[icao_code].get("runways", []):
            t_hdg1 = (rw.get('hdg1', 0) - mag_var_w) % 360
            t_hdg2 = (rw.get('hdg2', 0) - mag_var_w) % 360
            diff1 = min(abs(track - t_hdg1), 360 - abs(track - t_hdg1))
            diff2 = min(abs(track - t_hdg2), 360 - abs(track - t_hdg2))
            
            if diff1 < min_diff and diff1 <= tolerance:
                min_diff = diff1
                best_rwy = rw['name1']
            if diff2 < min_diff and diff2 <= tolerance:
                min_diff = diff2
                best_rwy = rw['name2']
                
        return best_rwy

    def check_runway_position(self, airport_code, p_lat, p_lon, track):
        icao_code = self.get_icao(airport_code)
        if not icao_code or icao_code not in self.runway_data: return False, None 
        
        mag_var_w = self.runway_data[icao_code].get('mag_var_w', 0)
        best_rwy = None
        min_dist = 999999.0
        
        for rw in self.runway_data[icao_code].get("runways", []):
            # 🌟 FIX: Safely retrieve spatial coordinates or skip if missing
            lat1 = rw.get('lat1')
            lon1 = rw.get('lon1')
            lat2 = rw.get('lat2')
            lon2 = rw.get('lon2')
            
            if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
                continue
                
            lat_scale, lon_scale = 111320.0, 111320.0 * math.cos(math.radians(lat1))
            
            bx, by = (lon2 - lon1) * lon_scale, (lat2 - lat1) * lat_scale
            px, py = (p_lon - lon1) * lon_scale, (p_lat - lat1) * lat_scale
            
            l2 = bx**2 + by**2
            if l2 == 0: continue
            
            length_m = math.sqrt(l2)
            
            buffer_t = 6000.0 / length_m if length_m > 0 else 0
            
            t = max(-buffer_t, min(1 + buffer_t, (px * bx + py * by) / l2))
            proj_x, proj_y = t * bx, t * by
            dist_m = math.sqrt((px - proj_x)**2 + (py - proj_y)**2)
            
            dist_from_threshold_m = 0.0
            if t < 0: dist_from_threshold_m = abs(t) * length_m
            elif t > 1: dist_from_threshold_m = (t - 1) * length_m
            
            cone_expansion = dist_from_threshold_m * 0.20 
            # 🌟 FIX: Safe retrieval of width_m, fallback to 45m standard runway width
            width_buffer = (rw.get('width_m', 45.0) / 2) + 200.0 + cone_expansion 
            
            if dist_m <= width_buffer:
                t_hdg1, t_hdg2 = (rw.get('hdg1', 0) - mag_var_w) % 360, (rw.get('hdg2', 0) - mag_var_w) % 360
                diff1 = min(abs(track - t_hdg1), 360 - abs(track - t_hdg1))
                diff2 = min(abs(track - t_hdg2), 360 - abs(track - t_hdg2))
                
                if diff1 <= 30 or diff2 <= 30: 
                    if dist_m < min_dist:
                        min_dist = dist_m
                        best_rwy = rw.get('name1', 'UNK') if diff1 <= diff2 else rw.get('name2', 'UNK')
                        
        return (best_rwy is not None), best_rwy

    async def get_aircraft_data(self):
        try:
            url = getattr(Config, 'ADSB_EXCHANGE_BINCRAFT_URL', "https://globe.adsbexchange.com/re-api/?binCraft&zstd&box=9.337602,33.583193,60.875230,104.092506")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Host": "globe.adsbexchange.com",
                "Referer": "https://globe.adsbexchange.com/",
                "Origin": "https://globe.adsbexchange.com"
            }
            logger.debug(f"📡 Attempting to fetch BinCraft data from {url}")
            async with self.session.get(url, headers=headers, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.read()
                
                def process_bincraft(data_bytes):
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".zst") as tmp:
                        tmp.write(data_bytes)
                        tmp_path = tmp.name
                    try:
                        data = binCraft_decoder.binCraftReader(tmp_path, zstd_compressed=True)
                        return data.get("aircraft", [])
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                            
                aircraft_data = await asyncio.to_thread(process_bincraft, payload)
                if aircraft_data:
                    logger.debug(f"📡 Successfully decoded {len(aircraft_data)} aircraft from BinCraft stream")
                    
                    # NEW: Direct bulk upsert all valid airborne flights to DB
                    await self.sync_valid_flights_to_db(aircraft_data)
                    
                    return aircraft_data
                    
        except Exception as e:
            logger.warning(f"⚠️ Primary BinCraft method failed, falling back to legacy polling: {e}")

        sources = [            
            (Config.ADSB_ONE_AIRCRAFT_DATA_URL, "aircraft"),
            (Config.RE_ADSB_LOL_AIRCRAFT_DATA_URL, "aircraft"),
            (Config.ADSB_LOL_AIRCRAFT_DATA_URL, "ac"),
            (Config.LOCAL_AIRCRAFT_DATA_URL, "aircraft")
        ]
        for url, key in sources:
            try:
                logger.debug(f"📡 Attempting fallback fetch from {url}")
                proxy = Config.PROXY_URL if "re-api" in url else None
                async with self.session.get(url, timeout=5, proxy=proxy) as resp:
                    if resp.status == 200:
                        data = orjson.loads(await resp.read())
                        aircraftData = data.get(key, data.get("ac", data.get("aircraft", [])))
                        logger.debug(f"📡 Fallback source {url} returned {len(aircraftData)} aircraft")
                        
                        # NEW: Direct bulk upsert all valid airborne flights to DB - bypasses complex tracking logic
                        await self.sync_valid_flights_to_db(aircraftData)
                        
                        return aircraftData
            except: continue
        return []

    # 🌟 NEW: Bulk sync all valid airborne flights to DB - provider agnostic
    async def sync_valid_flights_to_db(self, aircraft_list):
        """Bulk upsert all valid airborne flights to flights_in_air table"""
        if not aircraft_list:
            logger.warning("⚠️ SYNC: no aircraft data to sync")
            return
            
        valid_flights = []
        alt_values = []
        filtered_reasons = {"no_hex": 0, "ground": 0, "invalid_alt": 0, "no_position": 0}
        
        for ac in aircraft_list:
            hex_id = ac.get('hex')
            alt_baro = ac.get('alt_baro')
            alt_geom = ac.get('alt_geom')
            alt_values.append(alt_baro)
            
            # Skip if no hex
            if not hex_id:
                filtered_reasons["no_hex"] += 1
                continue
            
            # Determine best altitude value
            alt = None
            
            # Try alt_baro first (barometric altitude is preferred)
            if alt_baro is not None and alt_baro != "ground":
                if isinstance(alt_baro, (int, float)):
                    # Accept small negative values (sensor noise, up to -1000 ft)
                    # but reject extreme negative values that indicate data errors
                    if alt_baro >= -1000:
                        alt = int(alt_baro)
                    else:
                        # Extreme negative - try to use absolute value if it makes sense
                        # (some decoders might have signed/unsigned issues)
                        if abs(alt_baro) <= 50000:  # Max reasonable altitude 50k ft
                            alt = int(abs(alt_baro))
                            logger.debug(f"🔄 Altitude fix for {hex_id}: converted {alt_baro} to {alt}")
            
            # Fallback to alt_geom (GPS altitude) if baro is invalid
            if alt is None and alt_geom is not None and alt_geom != "ground":
                if isinstance(alt_geom, (int, float)) and alt_geom >= -1000:
                    alt = int(alt_geom)
                    logger.debug(f"🔄 Using alt_geom for {hex_id}: {alt}")
            
            # Skip if still no valid altitude
            if alt is None:
                if alt_baro == "ground" or alt_geom == "ground":
                    filtered_reasons["ground"] += 1
                else:
                    filtered_reasons["invalid_alt"] += 1
                continue
            
            # Extract all required fields
            callsign = (ac.get('flight') or '').strip() or hex_id
            lat = to_f(ac.get('lat'))
            lon = to_f(ac.get('lon'))
            
            # Skip if no valid position
            if not lat or not lon:
                filtered_reasons["no_position"] += 1
                continue
            
            speed = int(ac.get('gs', 0) or 0) if ac.get('gs') else 0
            heading = to_f(ac.get('track')) if ac.get('track') else 0.0
            
            # Include empty route columns - will be populated by gap_filler later
            valid_flights.append((hex_id, callsign, lat, lon, alt, speed, heading, None, None, None, None, None, None, None, None, None))
        
        total_filtered = sum(filtered_reasons.values())
        logger.warning(f"⚠️ SYNC: processing {len(aircraft_list)} aircraft, valid={len(valid_flights)}, filtered={total_filtered}, alt_samples={alt_values[:3]}")
        if total_filtered > 0:
            logger.debug(f"🔍 Filter reasons: {filtered_reasons}")
        
        if valid_flights:
            try:
                await self.db.bulk_upsert_flights_in_air(valid_flights)
                logger.warning(f"💾 SYNC: {len(valid_flights)} flights upserted to flights_in_air")
                
                # Cache to Redis for fast API reads
                await self._cache_flights_to_redis(valid_flights)
            except Exception as e:
                logger.error(f"❌ SYNC ERROR: {e}")

    async def _cache_flights_to_redis(self, flights_list):
        """Cache flights to Redis for fast API reads"""
        try:
            import time
            redis_key = Config.REDIS_LIVE_FLIGHTS_KEY
            meta_key = Config.REDIS_LIVE_FLIGHTS_META_KEY
            
            # Clear old data and set new
            await self.redis.delete(redis_key)
            
            pipeline = self.redis.pipeline()
            for fl in flights_list:
                hex_id, callsign, lat, lon, alt, speed, heading, *_ = fl
                flight_data = {
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "speed": speed,
                    "heading": heading,
                    "callsign": callsign,
                    "last_seen": time.time()
                }
                pipeline.hset(redis_key, hex_id, orjson.dumps(flight_data))
            
            # Set meta info
            pipeline.hset(meta_key, "count", len(flights_list))
            pipeline.hset(meta_key, "last_update", time.time())
            pipeline.hset(meta_key, "source", "main_sync")
            
            # Set TTL on the key
            pipeline.expire(redis_key, Config.REDIS_FLIGHTS_TTL)
            pipeline.expire(meta_key, Config.REDIS_FLIGHTS_TTL)
            
            await pipeline.execute()
            logger.info(f"✅ Cached {len(flights_list)} flights to Redis")
        except Exception as e:
            logger.error(f"❌ Redis cache error: {e}")

    # 🌟 NEW FIX: Ground Truth Arbiter with Multi-Route Array Support
    async def fetch_flight_details(self, hex_id, callsign, current_airport=None, context="AIRBORNE"):
        cs = normalize_callsign(callsign)
        if not cs: return None, None
        
        api_orig, api_dest = None, None
        csv_orig, csv_dest = None, None
        db_orig, db_dest = None, None

        # 0. CHECK REDIS CACHE FIRST
        cached_orig, cached_dest = await self.get_cached_route(cs)
        if cached_orig and cached_dest:
            return cached_orig, cached_dest
        
        # 0b. CHECK MEMORY CACHE FALLBACK
        mem_orig, mem_dest = await self.get_memory_route(cs)
        if mem_orig and mem_dest:
            return mem_orig, mem_dest

        # 1. FETCH FROM ALL SOURCES
        # A. Get IATA from FR24 (always primary for IATA mapping)
        try:
            iata_code, iata_flight, operator = await get_iata_from_icao_fr24(cs, self.session)
        except Exception as e:
            logger.warning(f"FR24 IATA lookup failed for {cs}: {e}")
            iata_code, iata_flight, operator = None, None, None

        # B. Try route sources in priority order
        api_orig, api_dest = None, None
        
        if iata_flight:
            for source in Config.ROUTE_RESOLUTION_ORDER:
                try:
                    if source == "flightaware" and Config.FLIGHTAWARE_ROUTE_ENABLED:
                        fa_orig, fa_dest = await get_route_from_flightaware(cs, iata_flight, self.session)
                        if fa_orig and fa_dest:
                            api_orig, api_dest = self.get_icao(fa_orig), self.get_icao(fa_dest)
                            if api_orig and api_dest:
                                await self.set_cached_route(cs, api_orig, api_dest)
                                logger.info(f"✈️ FlightAware resolved {cs} ({iata_flight}): {api_orig} -> {api_dest}")
                                break
                    
                    elif source == "adsbdb":
                        ad_orig, ad_dest = await get_route_from_adsbdb(cs, iata_flight, hex_id, self.session)
                        if ad_orig and ad_dest:
                            api_orig, api_dest = self.get_icao(ad_orig), self.get_icao(ad_dest)
                            if api_orig and api_dest:
                                await self.set_cached_route(cs, api_orig, api_dest)
                                logger.info(f"✈️ adsbdb resolved {cs} ({iata_flight}): {api_orig} -> {api_dest}")
                                break
                except Exception as e:
                    logger.warning(f"Route resolution failed for {cs} ({source}): {e}")
                    continue
        
        # Try FlightAware directly with ICAO callsign if IATA lookup failed
        if not api_orig and not api_dest and cs and Config.FLIGHTAWARE_ROUTE_ENABLED:
            try:
                fa_orig, fa_dest = await get_route_from_flightaware(cs, None, self.session)
                if fa_orig and fa_dest:
                    api_orig, api_dest = self.get_icao(fa_orig), self.get_icao(fa_dest)
                    if api_orig and api_dest:
                        await self.set_cached_route(cs, api_orig, api_dest)
                        logger.info(f"✈️ FlightAware ICAO resolved {cs}: {api_orig} -> {api_dest}")
            except Exception as e:
                logger.warning(f"FlightAware ICAO lookup failed for {cs}: {e}")

        # C. Fallback: adsbdb with ICAO callsign (if no IATA found)
        if not api_orig and not api_dest and cs:
            for url_base in [Config.API_ADSB_DB_AIRCRAFT + hex_id + "?callsign=", Config.API_ADSB_DB_CALLSIGN]:
                try:
                    async with self.session.get(f"{url_base}{cs}", timeout=8) as resp:
                        if resp.status == 200:
                            data = (orjson.loads(await resp.read())).get("response", {}).get("flightroute", {})
                            o = data.get("origin", {}).get("icao_code") or data.get("origin", {}).get("iata_code")
                            d = data.get("destination", {}).get("icao_code") or data.get("destination", {}).get("iata_code")
                            if o and d: 
                                api_orig, api_dest = self.get_icao(o), self.get_icao(d)
                                # Cache successful API result
                                if api_orig and api_dest:
                                    await self.set_cached_route(cs, api_orig, api_dest)
                                break
                except: pass

        # C. Local CSV
        if cs in self.routes_map:
            route_array = self.routes_map[cs]
            c_o, c_d = None, None
            
            if current_airport:
                idx = -1
                for i, ap in enumerate(route_array):
                    if self.get_icao(ap) == current_airport:
                        idx = i
                        break
                
                # Slide the window based on context
                if idx != -1:
                    if context == "DEPARTURE" and idx < len(route_array) - 1:
                        c_o, c_d = route_array[idx], route_array[idx + 1]
                    elif context == "ARRIVAL" and idx > 0:
                        c_o, c_d = route_array[idx - 1], route_array[idx]

            # If no context matched perfectly, use array outer bounds
            if not c_o or not c_d:
                c_o, c_d = route_array[0], route_array[-1]

            csv_orig, csv_dest = self.get_icao(c_o), self.get_icao(c_d)
            # Cache CSV result if valid
            if csv_orig and csv_dest and not (api_orig or api_dest):
                await self.set_cached_route(cs, csv_orig, csv_dest)

        # C. Local DB Schedule
        db_orig, db_dest = await self.db.get_route_from_schedule(cs, current_airport)
        if db_orig: db_orig = self.get_icao(db_orig)
        if db_dest: db_dest = self.get_icao(db_dest)
            # Cache DB result if valid and nothing cached yet
        if db_orig and db_dest and not (api_orig or api_dest or csv_orig):
            await self.set_cached_route(cs, db_orig, db_dest)

        # Prevent same-airport routes (VOHS-VOHS bug)
        if api_orig == api_dest: api_orig, api_dest = None, None
        if csv_orig == csv_dest: csv_orig, csv_dest = None, None
        if db_orig == db_dest: db_orig, db_dest = None, None

        # 2. PHYSICAL GROUND TRUTH RESOLUTION
        if current_airport:
            if context == "ARRIVAL":
                # User Logic 1: Check if CSV matches landing airport
                if csv_dest == current_airport:
                    if db_orig != csv_orig or db_dest != csv_dest:
                        logger.info(f"📂 GROUND TRUTH OVERRIDE: API wrong/missing. Forcing {cs} schedule to CSV ({csv_orig}->{csv_dest})")
                        await self.db.update_schedule_with_csv(cs, hex_id, csv_orig, csv_dest)
                    return csv_orig, csv_dest
                # User Logic 2: Check if API matches landing airport
                if api_dest == current_airport:
                    return api_orig, api_dest
                # User Logic 3: Check DB as final fallback
                if db_dest == current_airport:
                    return db_orig, db_dest

            elif context == "DEPARTURE":
                # Check Origin matches physical airport
                if csv_orig == current_airport:
                    if db_orig != csv_orig or db_dest != csv_dest:
                        logger.info(f"📂 GROUND TRUTH OVERRIDE: API wrong/missing. Forcing {cs} schedule to CSV ({csv_orig}->{csv_dest})")
                        await self.db.update_schedule_with_csv(cs, hex_id, csv_orig, csv_dest)
                    return csv_orig, csv_dest
                if api_orig == current_airport:
                    return api_orig, api_dest
                if db_orig == current_airport:
                    return db_orig, db_dest

        # 3. NO PHYSICAL MATCH OR AIRBORNE (Return best available starting with API)
        if api_orig and api_dest: return api_orig, api_dest
        if csv_orig and csv_dest: return csv_orig, csv_dest
        if db_orig and db_dest: return db_orig, db_dest

        return None, None

    async def gap_filler_worker(self):
        while True:
            try:
                active_planes = await self.db.get_planes_missing_enrichment()
                for row in active_planes:
                    hex_id, raw_cs = row['hexid'], row['callsign']
                    orig, dest = await self.fetch_flight_details(hex_id, raw_cs)
                    if orig or dest:
                        await self.db.log_event(hex_id, raw_cs, "ENRICHMENT", f"Pre-fetch: {orig}->{dest}", origin=orig, destination=dest)
                        try:
                            await self.db.update_enriched_route(hex_id, raw_cs, orig, dest)
                            logger.info(f"✅ update_enriched_route succeeded for {raw_cs}")
                        except Exception as e:
                            logger.error(f"❌ update_enriched_route failed for {raw_cs}: {e}")
                        
                        # Get airport coordinates and update flights_in_air
                        if orig and dest:
                            logger.info(f"📝 orig and dest both present for {raw_cs}: {orig} -> {dest}")
                            origin_lat, origin_lon = None, None
                            dest_lat, dest_lon = None, None
                            origin_iata, dest_iata = None, None
                            callsign_iata = None
                            
                            # Get origin airport data from Config.TARGET_AIRPORTS
                            if orig in Config.TARGET_AIRPORTS:
                                ap_data = Config.TARGET_AIRPORTS[orig]
                                origin_lat = float(ap_data.get('lat', 0)) if ap_data.get('lat') else None
                                origin_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                                origin_iata = ap_data.get('iata')
                            # Get destination airport data from Config.TARGET_AIRPORTS
                            if dest in Config.TARGET_AIRPORTS:
                                ap_data = Config.TARGET_AIRPORTS[dest]
                                dest_lat = float(ap_data.get('lat', 0)) if ap_data.get('lat') else None
                                dest_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                                dest_iata = ap_data.get('iata')
                            
                            # Update flights_in_air with route data
                            logger.info(f"📝 About to update flights_in_air for {raw_cs} ({hex_id}): {orig} -> {dest}")
                            await self.db.update_flight_in_air_route(
                                hex_id, raw_cs, orig, dest, 
                                origin_iata, dest_iata,
                                origin_lat, origin_lon, dest_lat, dest_lon,
                                callsign_iata
                            )
                        
                        if orig:
                            await self.db.link_actual_flight_to_schedule(orig, 'DEPARTURES', raw_cs, hex_id, time.time(), route_airport=dest)

                    await asyncio.sleep(getattr(Config, 'ENRICHMENT_FETCH_DELAY_SEC', 2))

                inc_arr = await self.db.get_incomplete_arrivals()
                for row in inc_arr:
                    hex_id, raw_cs = row['hex_id'], row['callsign']
                    org, _ = await self.fetch_flight_details(hex_id, raw_cs)
                    if org: await self.db.update_arrival_broadcast(row['id'], hex_id, org)
                    await asyncio.sleep(getattr(Config, 'ENRICHMENT_FETCH_DELAY_SEC', 2))

                await self.db.cleanup_stale_ground_ops(hours=4)

            except Exception as e: logger.error(f"Gap Filler/TTL Error: {e}")
            await asyncio.sleep(getattr(Config, 'GAP_FILLER_INTERVAL_SEC', 10))

    async def route_enrichment_worker(self):
        """Background worker to populate route data in flights_in_air table"""
        logger.info("🗺️ Route Enrichment Worker Started")
        while True:
            try:
                async with self.db.pool.acquire() as conn:
                    # Find flights missing route data
                    rows = await conn.fetch("""
                        SELECT hexid, callsign FROM flights_in_air 
                        WHERE origin_icao IS NULL 
                        LIMIT 10
                    """)
                    
                    for row in rows:
                        hex_id, raw_cs = row['hexid'], row['callsign']
                        try:
                            orig, dest = await self.fetch_flight_details(hex_id, raw_cs)
                            if orig and dest:
                                origin_lat, origin_lon = None, None
                                dest_lat, dest_lon = None, None
                                origin_iata, dest_iata = None, None
                                
                                if orig in Config.TARGET_AIRPORTS:
                                    ap_data = Config.TARGET_AIRPORTS[orig]
                                    origin_lat = float(ap_data.get('lat', 0)) if ap_data.get('lat') else None
                                    origin_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                                    origin_iata = ap_data.get('iata')
                                
                                if dest in Config.TARGET_AIRPORTS:
                                    ap_data = Config.TARGET_AIRPORTS[dest]
                                    dest_lat = float(ap_data.get('lat', 0)) if ap_data.get('lat') else None
                                    dest_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                                    dest_iata = ap_data.get('iata')
                                
                                await self.db.update_flight_in_air_route(
                                    hex_id, raw_cs, orig, dest,
                                    origin_iata, dest_iata,
                                    origin_lat, origin_lon, dest_lat, dest_lon,
                                    None  # callsign_iata
                                )
                                logger.info(f"✅ Route enriched for {raw_cs}: {orig} -> {dest}")
                        except Exception as e:
                            logger.warning(f"Route enrichment failed for {raw_cs}: {e}")
                        
                        await asyncio.sleep(1)  # Rate limit
                        
            except Exception as e:
                logger.error(f"Route Enrichment Error: {e}")
            
            await asyncio.sleep(30)  # Run every 30 seconds

    async def janitor_worker(self):
        logger.info(f"🧹 Background Janitor Service Active (Runs every {getattr(Config, 'JANITOR_INTERVAL_SEC', 900)}s)")
        while True:
            await asyncio.sleep(getattr(Config, 'JANITOR_INTERVAL_SEC', 900))
            try:
                # Clean up route cache memory
                await self.cleanup_memory_cache()
                
                async with self.db.pool.acquire() as conn:
                    await conn.execute("DELETE FROM arrivals_log WHERE anomaly_flag = 'TELEMETRY_BOUNCE' AND timestamp >= NOW() - INTERVAL '2 hours'")
                    await conn.execute("DELETE FROM flight_events WHERE anomaly_flag = 'TELEMETRY_BOUNCE' AND timestamp >= NOW() - INTERVAL '2 hours'")
                
                if getattr(Config, 'DEBUG_MODE', False):
                    logger.debug("🧹 Janitor Routine: Cleared recent telemetry bounces.")
            except Exception as e:
                logger.error(f"❌ Janitor Worker Error: {e}")

    async def websocket_broadcaster(self):
        logger.info(f"📡 WebSocket Broadcaster Active (Broadcasts every {getattr(Config, 'WEBSOCKET_BROADCAST_INTERVAL_SEC', 1)}s)")
        while True:
            await asyncio.sleep(getattr(Config, 'WEBSOCKET_BROADCAST_INTERVAL_SEC', 1))
            try:
                if not self.tracked_flights: continue
                
                # Build flight snapshot from tracked flights
                flights = []
                for hex_id, flight in self.tracked_flights.items():
                    if flight.get('lat') and flight.get('lon'):
                        flights.append({
                            'hexid': hex_id,
                            'callsign': flight.get('callsign', ''),
                            'lat': flight.get('lat'),
                            'lon': flight.get('lon'),
                            'alt': flight.get('altitude', 0),
                            'speed': flight.get('ground_speed', 0),
                            'heading': flight.get('heading', 0),
                            'origin': flight.get('origin', ''),
                            'destination': flight.get('destination', '')
                        })
                
                if flights:
                    payload = {'type': 'flight_snapshot', 'flights': flights, 'count': len(flights)}
                    channel = f"{Config.APP_NAME}:flight_events"
                    await self.redis.publish(channel, orjson.dumps(payload))
            except Exception as e:
                logger.error(f"❌ WS Broadcaster Error: {e}")

    async def process_new_candidates(self, current_aircraft):
        tasks = [self._process_single_candidate(ac) for ac in current_aircraft]
        if tasks:
            await asyncio.gather(*tasks)

    async def _process_single_candidate(self, ac):
        async with self.processing_semaphore:
            hex_id = ac.get("hex")
            callsign = ac.get("flight", "").strip() or hex_id
            
            c_lat, c_lon = extract_lat_lon(ac)
            if not hex_id or not callsign or c_lat == 0.0: return
            
            alt = extract_altitude(ac)
            speed = extract_speed(ac)
            vrate = extract_vrate(ac)
            heading = extract_heading(ac)

            airline_prefix = callsign[:3].upper() if len(callsign) >= 3 else ""
            if alt > Config.MAX_TRACKING_ALTITUDE or hex_id in self.tracked_flights or (airline_prefix and airline_prefix not in self.airline_map):
                return

            if await self.db.is_on_ground(hex_id):
                g_info = await self.db.get_ground_info(hex_id)
                if g_info and g_info.get('current_callsign') and g_info.get('current_callsign') != callsign:
                    # 🌟 FIX 1: FLICKER GUARD
                    if callsign != hex_id and len(callsign) > 3 and re.match(r"^[A-Z0-9]+$", callsign):
                        logger.info(f"🔄 GROUND DB IDENTITY WIPE: Hex {hex_id} woke up as {callsign} (was {g_info.get('current_callsign')}). Clearing old identity.")
                        await self.db.clear_ground_op(hex_id)
                    else:
                        callsign = g_info.get('current_callsign') # Restore real callsign
                        self.tracked_flights[hex_id] = {"hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), "status": "grounded", "climb_streak": 0, "alt_history": [alt]}
                        return
                else:
                    self.tracked_flights[hex_id] = {"hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), "status": "grounded", "climb_streak": 0, "alt_history": [alt]}
                    return 

            closest_ap, closest_dist = None, 9999
            try:
                nearby = await self.redis.geosearch(
                    "india_airports", 
                    longitude=c_lon, latitude=c_lat, 
                    radius=150, unit="km", withdist=True, sort="ASC"
                )
                if nearby:
                    closest_ap = nearby[0][0]
                    if isinstance(closest_ap, bytes): closest_ap = closest_ap.decode('utf-8')
                    closest_dist = nearby[0][1]
            except Exception as e:
                pass

            wakeup_radius = getattr(Config, 'TAKEOFF_WAKEUP_RADIUS_KM', 20)
            ap_elev = to_f(Config.TARGET_AIRPORTS.get(closest_ap, {}).get("elev", 0)) if closest_ap else 0
            
            if closest_ap and closest_dist < wakeup_radius and alt < (ap_elev + 3000) and (speed < 150 or vrate > 0):
                await self.db.log_wake_up(hex_id, callsign, closest_ap)
                
                orig, dest = await self.fetch_flight_details(hex_id, callsign, current_airport=closest_ap, context="DEPARTURE")
                orig = self.get_icao(orig)
                dest = self.get_icao(dest)
                
                if dest == closest_ap:
                    logger.info(f"🛬 LANDING ROLLOUT DETECTED: {callsign} is arriving at {closest_ap}. Blocking Pre-Flight.")
                    try:
                        await self.db.log_arrival(hex_id, callsign, closest_ap, orig, runway="UNK", anomaly_flag=None)
                        await self.db.link_actual_flight_to_schedule(closest_ap, 'ARRIVALS', callsign, hex_id, time.time(), route_airport=orig)
                        await self.db.register_landing_ops(hex_id, callsign, closest_ap, orig)
                        await self.db.log_event(hex_id, callsign, "LANDED", f"Landing rollout detected at {closest_ap}", airport=closest_ap, origin=orig, destination=dest, runway="UNK", anomaly_flag=None)
                        await self.db.remove_flight_from_air(hex_id)
                    except Exception as e:
                        logger.error(f"❌ DB ERROR ON LANDING ROLLOUT: {e}")
                    self.tracked_flights[hex_id] = {
                        "hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon,
                        "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(),
                        "status": "grounded", "climb_streak": 0, "alt_history": [alt]
                    }
                    return
                
                if orig == closest_ap or not orig:
                    logger.info(f"🛫 PRE-FLIGHT: Flight {callsign} is active at the gate at {closest_ap}")
                    await self.db.log_event(hex_id, callsign, "PRE_FLIGHT", "Aircraft active at gate", airport=closest_ap, origin=orig, destination=dest)
                    await self.db.update_schedule_status(closest_ap, 'DEPARTURES', callsign, 'PRE_FLIGHT', hex_id=hex_id, route_airport=dest)
                    
                    flight_payload = {
                        "hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, 
                        "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), 
                        "status": "pre_flight", "climb_streak": 0, "alt_history": [alt],
                        "origin": orig, "destination": dest
                    }
                    self.tracked_flights[hex_id] = flight_payload
                    
                    dest_str = f" to {dest}" if dest else ""
                    unsched_str = " (Unscheduled)" if not orig else ""
                    alert = f"🛫 <b>PRE-FLIGHT:</b> Flight {callsign}{unsched_str} is active at the gate at {closest_ap}, preparing for departure{dest_str}."
                    await self.queue_event_redis("pre_flight", flight_payload, custom_msg=alert)
                else:
                    self.tracked_flights[hex_id] = {
                        "hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, 
                        "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), 
                        "status": "grounded", "climb_streak": 0, "alt_history": [alt]
                    }                    
                return 

            if vrate < -200 or (alt < getattr(Config, 'APPROACH_ALT_THRESH_FT', 20000) and speed < 250 and vrate <= 0):
                if closest_ap and closest_dist < Config.APPROACH_RADIUS_KM: 
                    orig, dest = await self.fetch_flight_details(hex_id, callsign)
                    
                    if not orig or not dest:
                        ho, hd = await self.db.get_historical_route(hex_id, callsign)
                        orig, dest = orig or ho, dest or hd
                    
                    orig = self.get_icao(orig)
                    dest_code = self.get_icao(dest or closest_ap)

                    if dest_code and dest_code != closest_ap: pass 
                    elif orig and orig == closest_ap: pass 
                    else:
                        eta_msg = ""
                        if speed > 0:
                            try:
                                dest_lat, dest_lon = None, None
                                for icao_code, ap_data in Config.TARGET_AIRPORTS.items():
                                    if icao_code == dest_code:
                                        dest_lat, dest_lon = ap_data['lat'], ap_data['lon']
                                        break
                                
                                if dest_lat and dest_lon:
                                    dist_km = self.calculate_distance(c_lat, c_lon, dest_lat, dest_lon)
                                    live_mins = int((dist_km / (speed * 1.852)) * 60) 
                                    hist_buffer_mins = await self.db.get_avg_approach_time(dest_code) or 15
                                    
                                    total_eta_mins = (live_mins + int(hist_buffer_mins * (alt/10000))) if alt < 10000 else (int(live_mins * 1.10) + hist_buffer_mins)
                                    hrs, mins = total_eta_mins // 60, total_eta_mins % 60
                                    eta_msg = f" Expected landing in ~{hrs}h {mins}m." if hrs > 0 else f" Expected landing in ~{mins}m."
                            except Exception as e: pass

                        self.tracked_flights[hex_id] = {
                            "hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, 
                            "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), "status": "approaching",
                            "origin": orig, "destination": dest_code, "climb_streak": 0, "alt_history": [alt]
                        }
                        
                        await self.db.log_event(hex_id, callsign, "APPROACHING", f"Approaching {closest_ap}", airport=closest_ap, origin=orig, destination=dest_code)
                        await self.db.upsert_flight_in_air(hex_id, callsign, c_lat, c_lon, alt, speed, heading)
                        
                        orig_str = f" (From: {orig})" if orig else ""
                        custom_alert = f"🛬 <b>APPROACHING:</b> Flight {callsign}{orig_str} is approaching {dest_code}.{eta_msg}"
                        await self.queue_event_redis("approaching", self.tracked_flights[hex_id], custom_msg=custom_alert)
                        return
            
            #if alt > 3000 and speed > 100:
                #has_dep = await self.db.has_recent_departure(hex_id, hours=1)
            if alt > 3000 and speed > 100 and vrate > 500:
                has_dep = await self.db.has_recent_departure(hex_id, hours=0.5)
                if not has_dep:
                    g_info = await self.db.get_ground_info(hex_id)
                    inferred_origin = None
                    dest_code = None
                    
                    if g_info and g_info.get('airport'):
                        inferred_origin = self.get_icao(g_info.get('airport'))
                        _, dest_code = await self.fetch_flight_details(hex_id, callsign, current_airport=inferred_origin, context="DEPARTURE")
                    else:
                        orig, dest = await self.fetch_flight_details(hex_id, callsign)
                        if not orig:
                            orig, dest = await self.db.get_historical_route(hex_id, callsign)
                        inferred_origin = self.get_icao(orig)
                        dest_code = self.get_icao(dest)

                    # Prevent same-airport routes (VOHS-VOHS bug)
                    if dest_code == inferred_origin:
                        logger.warning(f"🛡️ SAME-AIRPORT FILTER: {callsign} departure dest=origin={dest_code}, clearing dest")
                        dest_code = None
                    if not inferred_origin and dest_code:
                        logger.warning(f"🛡️ NO ORIGIN FILTER: {callsign} has no origin, clearing dest")
                        dest_code = None

                    if inferred_origin:
                        origin_valid = False
                        orig_lat, orig_lon = None, None
                        ap_elev = 0.0
                        
                        # 1. Tier 1: O(1) Dictionary Lookup for Airport Data
                        ap = Config.TARGET_AIRPORTS.get(inferred_origin)
                        if ap:
                            orig_lat, orig_lon = to_f(ap.get('lat')), to_f(ap.get('lon'))
                            ap_elev = to_f(ap.get('elev', 0.0))
                        
                        # 2. Tier 2: Try data/airports.csv fallback
                        if orig_lat is None or orig_lon is None:
                            try:
                                with open("data/airports.csv", mode='r', encoding='utf-8-sig') as f:
                                    for row in csv.DictReader(f):
                                        if row.get('ICAO', '').strip().upper() == inferred_origin or row.get('IATA', '').strip().upper() == inferred_origin:
                                            orig_lat = to_f(row.get('Latitude'))
                                            orig_lon = to_f(row.get('Longitude'))
                                            ap_elev = to_f(row.get('Altitude', 0.0))
                                            break
                            except Exception:
                                pass

                        # --- VERTICAL MATH ---
                        alt_gained = max(0.0, alt - ap_elev)
                        effective_climb_rate = max(500.0, float(vrate)) if vrate else 2000.0
                        vertical_mins_ago = alt_gained / effective_climb_rate

                        # 3. Tier 3: Approximation Fallback (Reverse Kinematics)
                        if orig_lat is None or orig_lon is None:
                            dist_nm = max(100.0, float(speed)) * (vertical_mins_ago / 60.0)
                            dist_km = dist_nm / 0.539957
                            
                            rev_heading = (heading - 180) % 360
                            lat_offset = (dist_km * math.cos(math.radians(rev_heading))) / 111.32
                            lon_offset = (dist_km * math.sin(math.radians(rev_heading))) / (111.32 * math.cos(math.radians(c_lat)))
                            
                            orig_lat = c_lat + lat_offset
                            orig_lon = c_lon + lon_offset

                        # Distance Sanity Check 
                        #dist_km = self.calculate_distance(c_lat, c_lon, orig_lat, orig_lon)                        
                        #if dist_km < 350.0:  
                        #    origin_valid = True

                        # Distance Sanity Check 
                        dist_km = self.calculate_distance(c_lat, c_lon, orig_lat, orig_lon)
                        
                        if dist_km < getattr(Config, 'TAKEOFF_INFERENCE_MAX_DIST_KM', 150.0):
                            origin_valid = True
                        else:
                            # 1. Calculate what the "Look Down" fallback sees below the plane
                            closest_ap_fallback = None
                            closest_dist_fallback = 100.0 
                            fallback_lat, fallback_lon, fallback_elev = None, None, 0.0
                            
                            for fallback_icao, fallback_data in Config.TARGET_AIRPORTS.items():
                                f_dist = self.calculate_distance(c_lat, c_lon, to_f(fallback_data.get('lat')), to_f(fallback_data.get('lon')))
                                if f_dist < closest_dist_fallback:
                                    closest_dist_fallback = f_dist
                                    closest_ap_fallback = fallback_icao
                                    fallback_lat = to_f(fallback_data.get('lat'))
                                    fallback_lon = to_f(fallback_data.get('lon'))
                                    fallback_elev = to_f(fallback_data.get('elev', 0.0))

                            # ==========================================
                            # 🧪 A/B TEST: SHADOW TRACKER
                            # ==========================================
                            approach_a_would_trigger = (closest_ap_fallback is not None)
                            approach_b_allows = (alt < 18000 and vrate > 1000)
                            
                            if approach_a_would_trigger and not approach_b_allows:
                                logger.info(f"🧪 A/B TEST DIVERGENCE [{callsign}]: Approach A would have hallucinated a takeoff from {closest_ap_fallback} (just {closest_dist_fallback:.1f}km away). Approach B safely aborted because Alt={alt}ft and vRate={vrate}fpm.")
                            # ==========================================

                            # 🌟 APPROACH B: The High-Altitude Cruising Guard (Controls the DB)
                            if approach_b_allows and closest_ap_fallback:
                                logger.warning(f"🛡️ SANITY GUARD: API claimed {callsign} started at {inferred_origin} ({int(dist_km)}km away). Rejecting bad data.")
                                
                                inferred_origin = closest_ap_fallback
                                orig_lat, orig_lon, ap_elev = fallback_lat, fallback_lon, fallback_elev
                                origin_valid = True
                                logger.info(f"📍 RADAR OVERRIDE: Look-Down fallback snapped {callsign} to true physical origin: {inferred_origin}")
                                
                            elif not approach_b_allows:
                                logger.warning(f"🛑 REJECTED FALLBACK: {callsign} at {alt}ft (vRate: {vrate}fpm) is too high/slow to safely infer a local takeoff. Ignoring.")



                        if origin_valid:
                            # --- HORIZONTAL MATH ---
                            dist_nm = dist_km * 0.539957
                            effective_speed_kts = max(100.0, float(speed))
                            horizontal_mins_ago = (dist_nm / effective_speed_kts) * 60.0
                            
                            # --- THE 3D BLEND ---
                            blended_mins_ago = (vertical_mins_ago + horizontal_mins_ago) / 2.0
                            final_mins_ago = min(blended_mins_ago, 30.0)
                            inferred_time = time.time() - (final_mins_ago * 60)
                            
                            # ==========================================
                            # 📊 ATOMIC KINEMATIC TELEMETRY DUMP
                            # ==========================================
                            # Build the entire message in memory first so threads cannot interrupt it
                            log_msg = (
                                f"🚀 KINEMATIC REPORT [{callsign} | {hex_id}]:\n"
                                f"   ├─ Live: Alt:{alt}ft | Spd:{speed}kts | vRate:{vrate}fpm | Hdg:{heading:.1f}\n"
                                f"   ├─ Origin ({inferred_origin}): Dist:{dist_nm:.1f}NM | Elev:{ap_elev}ft\n"
                                f"   └─ Math: Vert={vertical_mins_ago:.2f}m | Horiz={horizontal_mins_ago:.2f}m ➔ Applied: {final_mins_ago:.2f}m"
                            )
                            
                            try:
                                async with self.db.pool.acquire() as conn:
                                    sched_row = await conn.fetchrow("""
                                        SELECT scheduled_time FROM flight_schedules 
                                        WHERE (callsign = $1 OR flight_number = $1) 
                                          AND airport_code = $2 AND direction = 'DEPARTURES' 
                                          AND scheduled_time >= NOW() - INTERVAL '12 hours' 
                                        ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
                                    """, callsign, inferred_origin)
                                    
                                    if sched_row and sched_row['scheduled_time']:
                                        sched_ts = sched_row['scheduled_time'].timestamp()
                                        taxi_delay_mins = (inferred_time - sched_ts) / 60.0
                                        log_msg += f"\n   └─ ⏱️ SCHEDULE DELTA: Inferred Wheels-Up was {taxi_delay_mins:.1f} mins after Gate Departure."
                            except Exception:
                                pass
                                
                            # Print it all at once!
                            logger.info(log_msg)
                            # ==========================================
                            # ==========================================
                            
                            logger.warning(f"👻 GHOST FLIGHT RECTIFIED: {callsign} woke up mid-air at {alt}ft. Inferring takeoff from {inferred_origin} ~{int(final_mins_ago)} mins ago.")
                            
                            await self.db.log_departure(hex_id, callsign, inferred_origin, dest_code, runway="UNK", anomaly_flag="INFERRED_TAKEOFF", manual_timestamp=inferred_time)
                            await self.db.log_event(hex_id, callsign, "INFERRED_TAKEOFF", f"Radar gap filled. Back-projected {int(final_mins_ago)}m", airport=inferred_origin, destination=dest_code, anomaly_flag="INFERRED_TAKEOFF", manual_timestamp=inferred_time)
                            await self.db.link_actual_flight_to_schedule(inferred_origin, 'DEPARTURES', callsign, hex_id, inferred_time, route_airport=dest_code)
                            await self.db.clear_ground_op(hex_id)

            self.tracked_flights[hex_id] = {"hex": hex_id, "callsign": callsign, "lat": c_lat, "lon": c_lon, "alt_baro": alt, "last_seen": time.time(), "first_seen": time.time(), "status": "airborne", "climb_streak": 0, "alt_history": [alt]}

    async def update_tracked_flights(self, current_aircraft):
        current_time = time.time()
        live_map = {ac.get("hex"): ac for ac in current_aircraft if ac.get("hex")}
        
        tasks = [self._update_single_flight(hex_id, live_map.get(hex_id), current_time) for hex_id in list(self.tracked_flights.keys())]
        
        if tasks:
            results = await asyncio.gather(*tasks)
            bulk_upsert_list = [r for r in results if r is not None]
            if bulk_upsert_list:
                await self.db.bulk_upsert_flights_in_air(bulk_upsert_list)
            
        await self.db.cleanup_stale_flights()                    

    async def _update_single_flight(self, hex_id, live, current_time):
        upsert_tuple = None
        async with self.processing_semaphore:
            tracked_flight = self.tracked_flights.get(hex_id)
            if not tracked_flight: return None
            
            if live:
                c_callsign = live.get("flight", "").strip() or hex_id
                
                if c_callsign and c_callsign != hex_id and tracked_flight.get('callsign') and tracked_flight['callsign'] != c_callsign:
                    # 🌟 CALLSIGN SANITY GUARD: Reject radio garbage like @@@@@@@@ or invalid chars
                    if re.match(r"^[A-Z0-9]+$", c_callsign):
                        if tracked_flight['status'] in ['landed', 'grounded', 'pre_flight']:
                            logger.info(f"🔄 TURNAROUND MEMORY WIPE: Hex {hex_id} changed from {tracked_flight['callsign']} to {c_callsign} at gate. Forcing fresh start.")
                            await self.db.clear_ground_op(hex_id)
                            del self.tracked_flights[hex_id]
                            return None  
                        else:
                            logger.info(f"🔄 IN-AIR IDENTITY CHANGE: Hex {hex_id} changed from {tracked_flight['callsign']} to {c_callsign}")
                            tracked_flight['callsign'] = c_callsign
                    else:
                        # Ignore the corrupted callsign and keep the healthy one
                        c_callsign = tracked_flight['callsign']
                tracked_flight['last_seen'] = current_time
                
                c_lat, c_lon = extract_lat_lon(live)
                c_alt = extract_altitude(live)
                c_speed = extract_speed(live)
                c_vrate = extract_vrate(live)
                c_heading = extract_heading(live)

                tracked_flight['lat'], tracked_flight['lon'], tracked_flight['alt_baro'] = c_lat, c_lon, c_alt

                alt_hist = tracked_flight.get('alt_history', [])
                alt_hist.append(c_alt)
                if len(alt_hist) > 12: alt_hist.pop(0) 
                tracked_flight['alt_history'] = alt_hist

                last_influx = tracked_flight.get('last_influx_time', 0)
                if current_time - last_influx >= getattr(Config, 'INFLUXDB_WRITE_INTERVAL_SEC', 30):
                    if c_lat != 0.0 and c_lon != 0.0:
                        await self.db.log_telemetry(hex_id, tracked_flight['callsign'], c_lat, c_lon, c_alt, c_speed, c_heading)
                        tracked_flight['last_influx_time'] = current_time

                g_info = await self.db.get_ground_info(hex_id)
                is_on_ground_db = g_info is not None

                if is_on_ground_db:
                    g_dict = dict(g_info)
                    ap_icao = self.get_icao(g_dict.get('airport'))
                    ap_elev = 0.0
                    for icao_code, ap_data in Config.TARGET_AIRPORTS.items():
                        if icao_code == ap_icao: ap_elev = to_f(ap_data.get('elev'))

                    raw_landed_at = g_dict.get('landed_at')
                    landed_at_val = float(raw_landed_at) if raw_landed_at is not None else current_time
                    time_since_landing = current_time - landed_at_val
                    inbound_cs = g_dict.get('inbound_callsign')

                    # ==========================================
                    # 🧹 AGGRESSIVE GROUND_OPS CLEAR
                    # Auto-clear ground_ops when clearly airborne
                    # Fixes: altitude showing as 0 for flights that
                    # took off but still in ground_ops table
                    # ==========================================
                    alt_hist = tracked_flight.get('alt_history', [])
                    alt_trend = (max(alt_hist) - min(alt_hist)) if len(alt_hist) >= 2 else 0
                    
                    # Check if clearly airborne - cruising flights have vrate=0, so check altitude+speed primarily
                    is_clearly_airborne = (
                        c_alt > 5000 and    # Well above any airport
                        c_speed > 200    # Reasonable cruise speed
                    )
                    
                    # Fallback: high altitude trend even at lower speed
                    if not is_clearly_airborne and len(alt_hist) >= 3:
                        is_clearly_airborne = (c_alt > 10000 or alt_trend >= 500)
                    
                    if is_clearly_airborne:
                        logger.warning(f"🧹 AIRBORNE CLEAR: {tracked_flight['callsign']} at {c_alt}ft, speed={c_speed}. Clearing ground_ops.")
                        await self.db.clear_ground_op(hex_id)
                        tracked_flight['status'] = 'airborne'
                    
                    # Existing janitor (keep as-is):
                    if c_alt > (ap_elev + 3000.0) and c_speed > 150.0:
                        if inbound_cs is not None and time_since_landing < 300:
                            logger.warning(f"🧹 JANITOR: Auto-correcting fake landing for {tracked_flight['callsign']}. Cruising at {c_alt}ft.")
                            await self.db.clear_ground_op(hex_id)
                            try:
                                async with self.db.pool.acquire() as conn:
                                    await conn.execute("DELETE FROM arrivals_log WHERE hex_id = $1 AND timestamp >= NOW() - INTERVAL '30 minutes'", hex_id)
                                    await conn.execute("DELETE FROM flight_events WHERE hex_id = $1 AND event_type = 'LANDED' AND timestamp >= NOW() - INTERVAL '30 minutes'", hex_id)
                            except Exception as db_err:
                                logger.error(f"❌ Janitor DB Error: {db_err}")
                            
                            tracked_flight['status'] = 'airborne'
                            is_on_ground_db = False 
                        
                        else:
                            logger.debug(f"🛫🕵️ GHOST TAKEOFF DETECTED: {tracked_flight['callsign']} missed takeoff from {ap_icao} but spotted in air at {c_alt}ft. Logging departure and updating schedule.")
                            await self.db.clear_ground_op(hex_id)
                            
                            try:
                                outbound_cs = tracked_flight['callsign']
                                _, final_dest = await self.fetch_flight_details(hex_id, outbound_cs, current_airport=ap_icao, context="DEPARTURE")
                                if not final_dest:
                                    _, hd = await self.db.get_historical_route(hex_id, outbound_cs)
                                    final_dest = hd
                                final_dest = self.get_icao(final_dest)

                                if final_dest == ap_icao:
                                    final_dest = None

                                await self.db.log_departure(hex_id, outbound_cs, ap_icao, final_dest, runway="UNK", anomaly_flag="INFERRED_TAKEOFF") 
                                await self.db.link_actual_flight_to_schedule(ap_icao, 'DEPARTURES', outbound_cs, hex_id, current_time, route_airport=final_dest)
                                
                                orig_icao = self.get_icao(g_dict.get('origin'))
                                if orig_icao == ap_icao:
                                    await self.db.log_event(hex_id, outbound_cs, "CYCLE_COMPLETE", f"Air Return Departure. Radar Gap Rectified.", airport=ap_icao, origin=ap_icao, destination=final_dest, runway="UNK", anomaly_flag="INFERRED_TAKEOFF")
                                else:
                                    inbound_str = f"Arrived as {inbound_cs}, " if inbound_cs else ""
                                    await self.db.log_event(hex_id, outbound_cs, "CYCLE_COMPLETE", f"{inbound_str}Departed {outbound_cs} (Gap Rectified)", airport=ap_icao, origin=ap_icao, destination=final_dest, runway="UNK", anomaly_flag="INFERRED_TAKEOFF")

                                dest_str = f" (Heading to {final_dest})" if final_dest else ""
                                await self.queue_event_redis("departing", tracked_flight, custom_msg=f"🛫 <b>TAKEOFF:</b> Flight {outbound_cs} departed {ap_icao}{dest_str} (Radar Gap Rectified).")
                                
                                tracked_flight['status'], tracked_flight['departed_ap'], tracked_flight['departed_elev'] = 'airborne', ap_icao, ap_elev
                            except Exception as db_err: logger.error(f"❌ DB ERROR ON GHOST TAKEOFF: {db_err}")
                            
                            is_on_ground_db = False
                
                # 🌟 FIX: THE PRE-FLIGHT GUARD
                # Ensures that a plane in pre_flight cannot fall into the airborne landing trap
                if not is_on_ground_db and tracked_flight['status'] not in ['landed', 'grounded', 'pre_flight']:
                    # Get route data from tracked_flight if available
                    orig = tracked_flight.get('origin')
                    dest = tracked_flight.get('destination')
                    orig_iata, dest_iata = None, None
                    orig_lat, orig_lon = None, None
                    dest_lat, dest_lon = None, None
                    
                    if orig and dest:
                        # Look up airport data from Config.TARGET_AIRPORTS
                        if orig in Config.TARGET_AIRPORTS:
                            ap_data = Config.TARGET_AIRPORTS[orig]
                            orig_iata = ap_data.get('iata')
                            orig_lat = float(ap_data.get('lat', 0)) if ap_data.get('lat') else None
                            orig_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                        if dest in Config.TARGET_AIRPORTS:
                            ap_data = Config.TARGET_AIRPORTS[dest]
                            dest_iata = ap_data.get('iata')
                            dest_lat = float(ap_data.get('lat', 0)) if ap_data.get('lon') else None
                            dest_lon = float(ap_data.get('lon', 0)) if ap_data.get('lon') else None
                    
                    # callsign_iata not currently populated - route_memory_cache stores (origin, dest, timestamp)
                    callsign_iata = None
                    
                    upsert_tuple = (hex_id, tracked_flight['callsign'], c_lat, c_lon, c_alt, c_speed, c_heading, orig, dest, orig_iata, dest_iata, orig_lat, orig_lon, dest_lat, dest_lon, callsign_iata)
                
                if is_on_ground_db:
                    over_asphalt, active_rwy = self.check_runway_position(ap_icao, c_lat, c_lon, c_heading)
                    is_taking_off = False
                    
                    alt_trend_up = len(alt_hist) >= 2 and (c_alt - min(alt_hist)) >= 150.0
                    
                    if c_speed > 90.0 and c_alt > (ap_elev + 150.0) and alt_trend_up:
                        is_taking_off = True

                    if is_taking_off:
                        raw_landed_at = g_dict.get('landed_at')
                        landed_at_val = float(raw_landed_at) if raw_landed_at is not None else current_time
                        time_on_ground = current_time - landed_at_val
                        
                        inbound_cs = g_dict.get('inbound_callsign')
                        
                        if not active_rwy:
                            active_rwy = self.guess_runway_from_heading(ap_icao, c_heading)
                            
                        active_rwy_str = active_rwy if active_rwy else "UNK"
                        rwy_str = f" on Runway {active_rwy_str}" if active_rwy_str != "UNK" else ""

                        if inbound_cs and time_on_ground < 300:  
                            logger.warning(f"🔄 TOUCH & GO INTERCEPTED: {tracked_flight['callsign']} at {ap_icao}")
                            await self.db.clear_ground_op(hex_id)
                            
                            try:
                                async with self.db.pool.acquire() as conn:
                                    await conn.execute("UPDATE arrivals_log SET anomaly_flag = 'TELEMETRY_BOUNCE' WHERE hex_id = $1 AND airport = $2 AND timestamp >= NOW() - INTERVAL '30 minutes'", hex_id, ap_icao)
                                    await conn.execute("UPDATE flight_events SET anomaly_flag = 'TELEMETRY_BOUNCE' WHERE event_type = 'LANDED' AND hex_id = $1 AND airport = $2 AND timestamp >= NOW() - INTERVAL '30 minutes'", hex_id, ap_icao)
                            except Exception as db_err:
                                logger.error(f"❌ DB ERROR marking bounce: {db_err}")

                            await self.db.log_event(hex_id, tracked_flight['callsign'], "TOUCH_AND_GO", f"Ground time {int(time_on_ground/60)}m", airport=ap_icao, runway=active_rwy_str, anomaly_flag="TRAINING_PATTERN")
                            tracked_flight['status'], tracked_flight['departed_ap'] = 'airborne', None
                            
                        else:
                            logger.info(f"🚀 TAKEOFF TRIGGERED: {tracked_flight['callsign']} leaving {ap_icao}{rwy_str}")
                            try:
                                outbound_cs = tracked_flight['callsign'] 
                                _, final_dest = await self.fetch_flight_details(hex_id, outbound_cs, current_airport=ap_icao, context="DEPARTURE")

                                if not final_dest:
                                    _, hd = await self.db.get_historical_route(hex_id, outbound_cs)
                                    final_dest = hd
                                    
                                final_dest = self.get_icao(final_dest)

                                if final_dest == ap_icao:
                                    logger.warning(f"🛡️ ROUTE SANITY GUARD: Prevented {outbound_cs} departing to its own origin ({ap_icao}). Forced UNK.")
                                    final_dest = None

                                await self.db.log_departure(hex_id, outbound_cs, ap_icao, final_dest, runway=active_rwy_str) 
                                await self.db.link_actual_flight_to_schedule(ap_icao, 'DEPARTURES', outbound_cs, hex_id, current_time, route_airport=final_dest)
                                
                                orig_icao = self.get_icao(g_dict.get('origin'))
                                if orig_icao == ap_icao:
                                    logger.info(f"🔄 Air Return Departure detected for {outbound_cs}.")
                                else:
                                    inbound_str = f"Arrived as {inbound_cs}, " if inbound_cs else ""
                                    await self.db.log_event(hex_id, outbound_cs, "CYCLE_COMPLETE", f"{inbound_str}Departed {outbound_cs}{rwy_str}", airport=ap_icao, origin=ap_icao, destination=final_dest, runway=active_rwy_str)
                                
                                await self.db.clear_ground_op(hex_id)
                                dest_str = f" (Heading to {final_dest})" if final_dest else ""
                                await self.queue_event_redis("departing", tracked_flight, custom_msg=f"🛫 <b>TAKEOFF:</b> Flight {outbound_cs} departed {ap_icao}{rwy_str}{dest_str}.")
                                
                                tracked_flight['status'], tracked_flight['departed_ap'], tracked_flight['departed_elev'] = 'departing', ap_icao, ap_elev
                            except Exception as db_err: logger.error(f"❌ DB ERROR ON TAKEOFF: {db_err}")

                # 🌟 FIX: Ensuring airborne block catches lost ground state properly
                elif not is_on_ground_db and tracked_flight['status'] not in ['landed', 'grounded']:
                    
                    if tracked_flight['status'] == 'departing':
                        dep_ap = tracked_flight.get('departed_ap')
                        dep_elev = tracked_flight.get('departed_elev', 0.0)
                        dist_from_dep = 999.0
                        if dep_ap:
                            for icao_code, ap_data in Config.TARGET_AIRPORTS.items():
                                if icao_code == dep_ap:
                                    dist_from_dep = self.calculate_distance(c_lat, c_lon, to_f(ap_data.get('lat')), to_f(ap_data.get('lon')))
                                    break
                        if c_alt > (dep_elev + 3000.0) or dist_from_dep > 15.0:
                            tracked_flight['status'] = 'airborne'
                        return upsert_tuple
                        
                    if tracked_flight['status'] == 'pre_flight':
                        # Catch planes that lost ground DB but are now genuinely flying
                        if c_alt > 3000.0 and c_speed > 150.0:
                            tracked_flight['status'] = 'airborne'
                        return upsert_tuple
                    
                    if tracked_flight['status'] == 'airborne' and (c_vrate < -200 or (c_alt < getattr(Config, 'APPROACH_ALT_THRESH_FT', 20000) and c_speed < 250 and c_vrate <= 0)):
                        closest_ap, closest_dist = None, 9999
                        try:
                            nearby = await self.redis.geosearch("india_airports", longitude=c_lon, latitude=c_lat, radius=150, unit="km", withdist=True, sort="ASC")
                            if nearby:
                                closest_ap = nearby[0][0]
                                if isinstance(closest_ap, bytes): closest_ap = closest_ap.decode('utf-8')
                                closest_dist = nearby[0][1]
                        except: pass

                        if closest_ap and closest_dist < Config.APPROACH_RADIUS_KM:
                            orig, dest = await self.fetch_flight_details(hex_id, tracked_flight['callsign'])
                            
                            if not orig or not dest:
                                ho, hd = await self.db.get_historical_route(hex_id, tracked_flight['callsign'])
                                orig, dest = orig or ho, dest or hd
                            
                            orig = self.get_icao(orig)
                            dest_code = self.get_icao(dest or closest_ap)

                            if dest_code and dest_code != closest_ap: pass 
                            elif orig and orig == closest_ap: pass 
                            else:
                                eta_msg = ""
                                if c_speed > 0:
                                    try:
                                        dest_lat, dest_lon = None, None
                                        for icao_code, ap_data in Config.TARGET_AIRPORTS.items():
                                            if icao_code == dest_code:
                                                dest_lat, dest_lon = ap_data['lat'], ap_data['lon']
                                                break
                                        
                                        if dest_lat and dest_lon:
                                            dist_km = self.calculate_distance(c_lat, c_lon, dest_lat, dest_lon)
                                            live_mins = int((dist_km / (c_speed * 1.852)) * 60) 
                                            hist_buffer_mins = await self.db.get_avg_approach_time(dest_code) or 15
                                            
                                            total_eta_mins = (live_mins + int(hist_buffer_mins * (c_alt/10000))) if c_alt < 10000 else (int(live_mins * 1.10) + hist_buffer_mins)
                                            hrs, mins = total_eta_mins // 60, total_eta_mins % 60
                                            eta_msg = f" Expected landing in ~{hrs}h {mins}m." if hrs > 0 else f" Expected landing in ~{mins}m."
                                    except Exception as e: pass

                                self.tracked_flights[hex_id] = {
                                    "hex": hex_id, "callsign": tracked_flight['callsign'], "lat": c_lat, "lon": c_lon, 
                                    "alt_baro": c_alt, "last_seen": time.time(), "first_seen": time.time(), "status": "approaching",
                                    "origin": orig, "destination": dest_code, "climb_streak": 0, "alt_history": alt_hist
                                }
                                
                                await self.db.log_event(hex_id, tracked_flight['callsign'], "APPROACHING", f"Approaching {closest_ap}", airport=closest_ap, origin=orig, destination=dest_code)
                                await self.db.upsert_flight_in_air(hex_id, tracked_flight['callsign'], c_lat, c_lon, c_alt, c_speed, c_heading)
                                
                                orig_str = f" (From: {orig})" if orig else ""
                                custom_alert = f"🛬 <b>APPROACHING:</b> Flight {tracked_flight['callsign']}{orig_str} is approaching {dest_code}.{eta_msg}"
                                await self.queue_event_redis("approaching", self.tracked_flights[hex_id], custom_msg=custom_alert)
                                return upsert_tuple 

                    nearby_airports = []
                    try:
                        nearby_airports = await self.redis.geosearch(
                            "india_airports", 
                            longitude=c_lon, latitude=c_lat, 
                            radius=150, unit="km", withdist=True, sort="ASC"
                        )
                    except: pass

                    for ap_item in nearby_airports:
                        icao_target = ap_item[0]
                        if isinstance(icao_target, bytes): icao_target = icao_target.decode('utf-8')
                        dist = ap_item[1]
                        ap = Config.TARGET_AIRPORTS.get(icao_target)
                        if not ap: continue
                        
                        alt_diff = c_alt - to_f(ap['elev'])
                        is_airport_boundary = dist < 5.0
                        
                        terminal_radius = getattr(Config, 'TERMINAL_AREA_RADIUS_KM', 50)
                        if tracked_flight['status'] == 'approaching' and dist <= terminal_radius:

                            if c_alt < 15000 and c_vrate <= 0:                                
                                tracked_flight['status'] = 'arriving'
                                dest_code = tracked_flight.get('destination') or icao_target
                                orig_code = tracked_flight.get('origin')
                                logger.info(f"📍 ARRIVING SHORTLY: {tracked_flight['callsign']} entering terminal area at {dest_code} | Alt: {c_alt} | Speed: {c_speed} | Heading: {c_heading} | vrate: {c_vrate}")
                                await self.db.log_event(hex_id, tracked_flight['callsign'], "ARRIVING", f"Entering terminal area (<{terminal_radius}km)", airport=dest_code, origin=orig_code, destination=dest_code)
                                await self.db.update_schedule_status(dest_code, 'ARRIVALS', tracked_flight['callsign'], 'ARRIVING_SHORTLY', hex_id=hex_id, route_airport=orig_code)
                                
                                eta_msg = ""
                                if c_speed > 0:
                                    try:
                                        live_mins = int((dist / (c_speed * 1.852)) * 60)
                                        hist_buffer_mins = await self.db.get_avg_approach_time(dest_code) or 10
                                        total_eta_mins = live_mins + max(2, int(hist_buffer_mins * 0.3))
                                        eta_msg = f" ETA ~{total_eta_mins}m."
                                    except: pass
                                
                                alert = f"📍 <b>ARRIVING SHORTLY:</b> Flight {tracked_flight['callsign']} is entering the terminal area at {dest_code}.{eta_msg}"
                                await self.queue_event_redis("arriving", tracked_flight, custom_msg=alert)

                        if tracked_flight['status'] in ['approaching', 'arriving'] and dist < getattr(Config, 'FINAL_APPROACH_RADIUS_KM', 40):
                            if alt_diff < getattr(Config, 'FINAL_APPROACH_ALT_FT', 4500) and c_vrate < -100:
                                aligned_rwy = self.guess_runway_from_heading(icao_target, c_heading, tolerance=10)
                                if aligned_rwy:
                                    logger.info(f"Aligned to Runway - {tracked_flight['callsign']} at {icao_target} | Alt: {c_alt} | Speed: {c_speed} | Heading: {c_heading} | vrate: {c_vrate}")
                                    tracked_flight['status'] = 'preparing_to_land'
                                    logger.info(f"🎯 ALIGNED FOR LANDING: {tracked_flight['callsign']} establishing on Runway {aligned_rwy} at {icao_target}")
                                    
                                    orig, dest = tracked_flight.get('origin'), tracked_flight.get('destination')
                                    if not orig or not dest:
                                        ho, hd = await self.db.get_historical_route(hex_id, tracked_flight['callsign'])
                                        orig, dest = orig or ho, dest or hd
                                    orig, dest = self.get_icao(orig), self.get_icao(dest)
                                    tracked_flight['origin'], tracked_flight['destination'] = orig, dest
                                    
                                    await self.db.log_event(hex_id, tracked_flight['callsign'], "FINAL_APPROACH", f"Aligned for Runway {aligned_rwy}", airport=icao_target, origin=orig, destination=dest, runway=aligned_rwy)
                                    alert = f"🎯 <b>FINAL APPROACH:</b> Flight {tracked_flight['callsign']} is aligned with Runway {aligned_rwy} at {icao_target}."
                                    await self.queue_event_redis("final_approach", tracked_flight, custom_msg=alert)

                        if dist < to_f(Config.LANDING_RADIUS_KM):
                            over_asphalt, active_rwy = self.check_runway_position(icao_target, c_lat, c_lon, c_heading)
                            
                            valid_landing_zone = over_asphalt or is_airport_boundary
                            
                            orig, dest = tracked_flight.get('origin'), tracked_flight.get('destination')
                            if not orig or not dest:
                                ho, hd = await self.db.get_historical_route(hex_id, tracked_flight['callsign'])
                                orig, dest = orig or ho, dest or hd
                                
                            orig = self.get_icao(orig)
                            dest = self.get_icao(dest)

                            # Prevent same-airport routes (VOHS-VOHS bug)
                            if orig == dest:
                                logger.warning(f"🛡️ SAME-AIRPORT FILTER: {tracked_flight['callsign']} has origin=dest={orig}, forcing re-fetch")
                                orig, dest = None, None

                            anomaly = None
                            
                            live_orig, live_dest = await self.fetch_flight_details(hex_id, tracked_flight['callsign'], current_airport=icao_target, context="ARRIVAL")
                            
                            if orig and orig == icao_target: 
                                if live_orig and live_orig != icao_target:
                                    logger.info(f"🛡️ AIR RETURN AVERTED: Cache hallucinated origin {orig}, but Ground Truth confirms {tracked_flight['callsign']} originated from {live_orig}.")
                                    orig = live_orig 
                                else:
                                    anomaly = "AIR_RETURN"
                                    
                            elif dest and dest != icao_target: 
                                if live_dest and live_dest == icao_target:
                                    logger.info(f"🛡️ DIVERSION AVERTED: Cache hallucinated dest {dest}, but Ground Truth confirms {tracked_flight['callsign']} belongs at {icao_target}.")
                                    dest = icao_target 
                                else:
                                    anomaly = "DIVERSION"
                                    
                            tracked_flight['origin'], tracked_flight['destination'] = orig, dest
                            
                            if valid_landing_zone and alt_diff < 500 and c_speed > 100 and c_vrate < 0:
                                tracked_flight['tng_candidate'] = active_rwy if active_rwy else self.guess_runway_from_heading(icao_target, c_heading)
                                tracked_flight['tng_airport'] = icao_target
                                tracked_flight['tng_ap_elev'] = to_f(ap['elev'])

                            dead_zone_catch = valid_landing_zone and (alt_diff < 2000.0) and (c_speed < 170.0) and (c_vrate < -150)
                            strict_landing = (alt_diff < 400.0 and c_speed < 100.0 and c_vrate <= 0) or (c_alt == 0.0 and c_speed < 170.0) or (alt_diff < 100.0 and c_speed < 80.0)
                            rollout_catch = valid_landing_zone and (alt_diff < 200.0) and (c_speed < 170.0)

                            if strict_landing or dead_zone_catch or rollout_catch:
                                if c_speed > 180.0:
                                    continue
                                if c_speed > 40.0 and not valid_landing_zone: 
                                    continue 

                                time_tracked = current_time - tracked_flight.get('first_seen', current_time)
                                is_valid_arrival = time_tracked > 60.0 or c_vrate < -300.0 or tracked_flight['status'] in ['approaching', 'arriving', 'preparing_to_land'] or over_asphalt
                                # added as the vrate while landing is not more than 1400 mostly but just keeping some buffer
                                is_valid_arrival = is_valid_arrival and c_vrate < 5000
                                if is_valid_arrival:
                                    if not active_rwy:
                                        active_rwy = self.guess_runway_from_heading(icao_target, c_heading)
                                        
                                    rwy_str = f" on Runway {active_rwy}" if active_rwy else ""
                                    logger.info(f"🛬 ARRIVAL TRIGGERED: {tracked_flight['callsign']} at {icao_target}{rwy_str} | Anomaly: {anomaly} | Altitude: {c_alt} | Speed: {c_speed} | Heading: {c_heading} | vRate: {c_vrate}")
                                    try:
                                        await self.db.log_arrival(hex_id, tracked_flight['callsign'], icao_target, orig, runway=active_rwy, anomaly_flag=anomaly) 
                                        
                                        await self.db.link_actual_flight_to_schedule(icao_target, 'ARRIVALS', tracked_flight['callsign'], hex_id, current_time, route_airport=orig)
                                        
                                        await self.db.register_landing_ops(hex_id, tracked_flight['callsign'], icao_target, orig)
                                        await self.db.log_event(hex_id, tracked_flight['callsign'], "LANDED", f"Landed at {icao_target}{rwy_str}", airport=icao_target, origin=orig, destination=dest, runway=active_rwy, anomaly_flag=anomaly)
                                        await self.db.remove_flight_from_air(hex_id)
                                        
                                        if anomaly == "AIR_RETURN": alert = f"🚨 <b>AIR RETURN:</b> Flight {tracked_flight['callsign']} has returned to {icao_target}!"
                                        elif anomaly == "DIVERSION": alert = f"⚠️ <b>DIVERSION:</b> Flight {tracked_flight['callsign']} diverted to {icao_target}!"
                                        else: alert = f"✅ <b>LANDED:</b> Flight {tracked_flight['callsign']} arrived at {icao_target}{rwy_str}."
                                            
                                        await self.queue_event_redis("landed", tracked_flight, custom_msg=alert)
                                        tracked_flight['status'], tracked_flight['tng_candidate'] = 'landed', None 
                                    except Exception as e: logger.error(f"❌ DB ERROR ON ARRIVAL: {e}")
                                else:
                                    await self.db.log_wake_up(hex_id, tracked_flight['callsign'], icao_target)
                                    tracked_flight['status'] = 'grounded'
                                return upsert_tuple 
            else:
                timeout_sec = getattr(Config, 'ASSUMED_LANDING_TIMEOUT_SEC', 180)
                if current_time - tracked_flight['last_seen'] > timeout_sec: 
                    if tracked_flight['status'] in ['preparing_to_land', 'arriving', 'approaching'] and to_f(tracked_flight.get('alt_baro', 99999)) < getattr(Config, 'FINAL_APPROACH_ALT_FT', 4500) + 1000:
                        dest_ap = tracked_flight.get('destination')
                        if not dest_ap:
                            f_lat, f_lon = tracked_flight.get('lat', 0), tracked_flight.get('lon', 0)
                            try:
                                nearby = await self.redis.geosearch("india_airports", longitude=f_lon, latitude=f_lat, radius=150, unit="km", withdist=True, sort="ASC")
                                if nearby:
                                    dest_ap = nearby[0][0]
                                    if isinstance(dest_ap, bytes): dest_ap = dest_ap.decode('utf-8')
                            except: pass
                        
                        if dest_ap:
                            logger.info(f"🛬 ASSUMED ARRIVAL TRIGGERED: {tracked_flight['callsign']} lost signal near {dest_ap}")
                            try:
                                await self.db.log_arrival(hex_id, tracked_flight['callsign'], dest_ap, tracked_flight.get('origin'), runway="UNK", anomaly_flag="ASSUMED_LANDING") 
                                
                                await self.db.link_actual_flight_to_schedule(dest_ap, 'ARRIVALS', tracked_flight['callsign'], hex_id, current_time, route_airport=tracked_flight.get('origin'))
                                
                                await self.db.register_landing_ops(hex_id, tracked_flight['callsign'], dest_ap, tracked_flight.get('origin'))
                                await self.db.log_event(hex_id, tracked_flight['callsign'], "LANDED", f"Assumed safe landing at {dest_ap}", airport=dest_ap, origin=tracked_flight.get('origin'), destination=dest_ap, runway="UNK", anomaly_flag="ASSUMED_LANDING")
                                alert = f"✅ <b>ASSUMED LANDED:</b> Flight {tracked_flight['callsign']} lost signal near the ground at {dest_ap}. Assuming safe arrival."
                                await self.queue_event_redis("landed", tracked_flight, custom_msg=alert)
                            except Exception as e: logger.error(f"❌ DB ERROR ON ASSUMED ARRIVAL: {e}")

                    if getattr(Config, 'DEBUG_MODE', False): logger.debug(f"🗑️ Dropping stale flight from memory and DB: {hex_id}")
                    try:
                        await self.db.remove_flight_from_air(hex_id)
                    except Exception: pass
                    
                    del self.tracked_flights[hex_id]
                    return None

        return upsert_tuple

    async def radar_producer(self):
        fetch_interval = getattr(Config, 'RADAR_FETCH_INTERVAL_SEC', 10)
        logger.info(f"📡 Radar Producer Started: Fetching live data every {fetch_interval}s")
        while True:
            logger.debug("📡 Fetching live aircraft data...")
            start_t = time.time()
            try:
                d = await self.get_aircraft_data()
                if d:
                    if self.radar_queue.full():
                        try:
                            self.radar_queue.get_nowait()
                            self.radar_queue.task_done()
                            logger.warning("⚠️ Radar processing lagging! Dropped oldest queue frame to maintain real-time accuracy.")
                        except asyncio.QueueEmpty: pass
                    await self.radar_queue.put(d)
            except Exception as e:
                logger.error(f"Producer Error: {e}")
            
            elapsed = time.time() - start_t
            sleep_time = max(0, fetch_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def radar_consumer(self):
        logger.info("⚙️ Radar Consumer Started: Processing radar frames")
        while True:
            try:
                d = await self.radar_queue.get()
                await self.process_new_candidates(d)
                await self.update_tracked_flights(d)
                self.radar_queue.task_done()
            except Exception as e:
                tb_lines = traceback.format_exc().splitlines()
                error_line = tb_lines[-1] if tb_lines else "Unknown line"
                
                logger.error(f"Consumer Error at line: {error_line}")
                logger.error(f"Full traceback: {traceback.format_exc()}")

    async def run(self):
        logger.info("🚀 Enterprise Async Monitor Started")
        
        await self.db.reset_system_state(self.redis)
        await self.load_airports_to_redis()
        
        async with aiohttp.ClientSession() as session:
            self.session = session
            
            await self.download_static_data()
            self.load_static_data()
            
            tasks = [
                asyncio.create_task(self.scheduled_data_updater()),
                asyncio.create_task(self.gap_filler_worker()),
                asyncio.create_task(self.route_enrichment_worker()),
                asyncio.create_task(self.janitor_worker()),
                asyncio.create_task(self.radar_producer()),
                asyncio.create_task(self.radar_consumer()),
            ]
            if getattr(Config, 'WEBSOCKET_ENABLED', True):
                tasks.append(asyncio.create_task(self.websocket_broadcaster()))
            
            logger.info("✅ All Radar tasks spawned successfully.")
            await asyncio.gather(*tasks)

async def main():
    pool = await asyncpg.create_pool(**Config.DB_PARAMS)
    redis_client = redis.Redis(**Config.REDIS_PARAMS)
    monitor = FlightMonitor(pool, redis_client)
    await monitor.run()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass