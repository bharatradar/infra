#!/usr/bin/env python3
"""
BharatRadar Schedule Downloader
Downloads flight schedules from FlightRadar24 and stores in PostgreSQL.
Runs as K3s CronJob - reads schedule_time from download_config table.

Usage:
    python route_schedule_downloader.py [--manual]
"""

import asyncio
import aiohttp
import asyncpg
import urllib.request
import ssl
import os
import sys
import csv
import logging
import traceback
import re
import argparse
import json
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s IST - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database config from environment
DB_PARAMS = {
    "database": os.environ.get("DB_NAME", "flight_db"),
    "user": os.environ.get("DB_USER", "flight_db_user"),
    "password": os.environ.get("DB_PASSWORD", "raga@098"),
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
}

GET_SCHEDULES_FROM_AVIONIO = os.environ.get("GET_SCHEDULES_FROM_AVIONIO", "false").lower() == "true"
GET_SCHEDULES_FOR = os.environ.get("GET_SCHEDULES_FOR", "TODAY,TOMORROW").split(",")
AIRLINES_FILE = os.environ.get("AIRLINES_FILE", "/opt/bharatradar/flight_radar/data/airlines.csv")
MISSING_AIRPORTS_IN_AVIONIO = set()
TARGET_AIRPORTS = {}


class AsyncDatabaseManager:
    """Minimal wrapper for asyncpg pool."""
    def __init__(self, pool):
        self.pool = pool


async def get_db_pool():
    """Create PostgreSQL connection pool."""
    return await asyncpg.create_pool(**DB_PARAMS, min_size=1, max_size=5)


async def load_config_from_db(pool):
    """Load TARGET_AIRPORTS from database."""
    global TARGET_AIRPORTS, MISSING_AIRPORTS_IN_AVIONIO
    
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT icao, iata, name, city, lat, lon, download_schedules
            FROM airports 
            WHERE download_schedules = TRUE AND lat IS NOT NULL AND lon IS NOT NULL
        """)
        
        for row in rows:
            TARGET_AIRPORTS[row['icao']] = {
                'iata': row['iata'] or '',
                'name': row['name'],
                'city': row['city'] or '',
                'lat': row['lat'],
                'lon': row['lon'],
            }
        
        logger.info(f"Loaded {len(TARGET_AIRPORTS)} airports from database")
        
        # Load missing airports config
        rows = await conn.fetch("SELECT icao FROM airports WHERE icao IN ('VEAY', 'VEDO', 'VIJW', 'VIDX', 'VEHO', 'VIAH', 'VAHS', 'VOKU', 'VANM', 'VARW', 'VOSR')")
        MISSING_AIRPORTS_IN_AVIONIO = {r['icao'] for r in rows}


async def get_download_config(pool) -> Optional[dict]:
    """Get download configuration from database."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT schedule_time, scheduler_enabled, enabled, last_run, last_status 
            FROM download_config 
            ORDER BY id DESC 
            LIMIT 1
        """)
        
        if row:
            return {
                'schedule_time': row['schedule_time'],
                'scheduler_enabled': row['scheduler_enabled'],
                'enabled': row['enabled'],
                'last_run': row['last_run'],
                'last_status': row['last_status'],
            }
        return None


async def update_download_status(pool, status: str):
    """Update last_run timestamp and status."""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE download_config 
            SET last_run = NOW(), last_status = $1, updated_at = NOW()
            WHERE id = (SELECT id FROM download_config ORDER BY id DESC LIMIT 1)
        """, status)


async def get_iata_to_icao_map():
    """Builds the airline code map needed to deduce missing callsigns."""
    iata_to_icao = {}
    if os.path.exists(AIRLINES_FILE):
        try:
            with open(AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    if str(row.get('Active', 'Y')).strip().upper() != 'N':
                        icao = row.get('ICAO', '').strip()
                        iata = row.get('IATA', '').strip()
                        if iata and icao:
                            iata_to_icao[iata] = icao
            logger.info(f"Loaded {len(iata_to_icao)} IATA->ICAO mappings for Callsign deduction")
        except Exception as e: 
            logger.error(f"Failed to load airlines: {e}")
    return iata_to_icao


async def get_airport_iata_to_icao_map(session: aiohttp.ClientSession):
    """Builds an airport mapping from IATA to ICAO using the airports.csv file."""
    airport_map = {}
    for icao, data in TARGET_AIRPORTS.items():
        if data.get('iata'):
            airport_map[data['iata'].upper()] = icao.upper()

    file_path = "/opt/bharatradar/flight_radar/data/airports.csv"
    try:
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            url = "https://vrs-standing-data.adsb.lol/airports.csv"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    with open(file_path, 'wb') as f:
                        f.write(await resp.read())
        if os.path.exists(file_path):
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    iata = row.get('IATA', '').strip().upper()
                    icao = row.get('ICAO', '').strip().upper()
                    if iata and icao:
                        airport_map[iata] = icao
        logger.info(f"Loaded {len(airport_map)} Airport IATA->ICAO mappings")
    except Exception as e:
        logger.error(f"Failed to load airports.csv: {e}")
    return airport_map


# ============================================================
# THE ORIGINAL download_schedules FUNCTION - UNCHANGED
# ============================================================
async def download_schedules(db: AsyncDatabaseManager, session: aiohttp.ClientSession, iata_to_icao: dict, airport_iata_to_icao: dict):
    logger.info("Fetching Schedules...")
    
    get_from_avionio = GET_SCHEDULES_FROM_AVIONIO
    missing_avionio = MISSING_AIRPORTS_IN_AVIONIO
    days_to_run = GET_SCHEDULES_FOR
    
    offsets = []
    if "TODAY" in days_to_run: offsets.append(0)
    if "TOMORROW" in days_to_run: offsets.append(1)
    
    headers_fr24 = {
        'accept': 'application/json, text/plain, */*',
        'origin': 'https://www.flightradar24.com',
        'referer': 'https://www.flightradar24.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    headers_avionio = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        for day_offset in offsets: 
            
            # STRICT EPOCH BOUNDING: Exactly bounds Today and Tomorrow in IST
            now_ist = datetime.now(IST)
            tomorrow_midnight_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_after_midnight_ist = (tomorrow_midnight_ist + timedelta(days=1))
            
            if day_offset == 0:
                # TODAY: Start from EXACTLY NOW, end at 23:59:59 tonight
                start_epoch = int(now_ist.timestamp())
                end_epoch = int(tomorrow_midnight_ist.timestamp())
                target_year_str = str(now_ist.year)
            else:
                # TOMORROW: Start exactly at 00:00:00 tomorrow, end at 23:59:59 tomorrow
                start_epoch = int(tomorrow_midnight_ist.timestamp())
                end_epoch = int(day_after_midnight_ist.timestamp())
                target_year_str = str(tomorrow_midnight_ist.year)
            
            target_ts_ms = start_epoch * 1000
                                    
            for icao_key, data in TARGET_AIRPORTS.items():
                iata_code = data.get('iata', '').upper()
                if not iata_code: continue
                target_code = iata_code.lower()
                
                use_avionio = False
                if get_from_avionio and icao_key not in missing_avionio:
                    use_avionio = True
                    
                if use_avionio:
                    # AVIONIO SCRAPING LOGIC
                    avionio_data = {"arrivals": [], "departures": []}
                    base_url = f"https://www.avionio.com/widget/en/{iata_code.upper()}/"
                    
                    for mode in ['arrivals', 'departures']:
                        page = 0
                        
                        while True:
                            url = f"{base_url}{mode}?ts={target_ts_ms}&page={page}"
                            try:
                                async with session.get(url, headers=headers_avionio, timeout=30) as resp:
                                    if resp.status != 200:
                                        logger.warning(f"Avionio returned HTTP {resp.status} for {iata_code} {mode} page {page}")
                                        break
                                    html = await resp.text()
                                    
                                    soup = BeautifulSoup(html, "html.parser")
                                    table = soup.find("table")
                                    
                                    if not table:
                                        break
                                            
                                    rows = table.find_all("tr")
                                    
                                    valid_html_rows = 0
                                    flights_added = 0
                                    should_break = False
                                    
                                    for row in rows:
                                        cols = row.find_all(["td", "th"])
                                        if len(cols) < 7 or "Time" in cols[0].text:
                                            continue
                                            
                                        valid_html_rows += 1
                                        
                                        time_val = cols[0].get_text(strip=True)
                                        date_val = cols[1].get_text(strip=True)
                                        route_iata_val = cols[2].get_text(strip=True)
                                        flight_val = cols[4].get_text(strip=True)
                                        
                                        if not time_val or "flights" in time_val.lower():
                                            continue
                                            
                                        try:
                                            dt_str = f"{date_val} {target_year_str} {time_val}"
                                            dt_obj = datetime.strptime(dt_str, "%d %b %Y %H:%M").replace(tzinfo=IST)
                                            row_epoch = int(dt_obj.timestamp())
                                        except ValueError:
                                            continue
                                            
                                        # Drop past flights, but KEEP looping (do not break)
                                        if row_epoch < start_epoch:
                                            continue
                                            
                                        # If the flight belongs to the NEXT day, break completely!
                                        if row_epoch >= end_epoch:
                                            should_break = True
                                            break
                                            
                                        avionio_data[mode].append({
                                            "time": time_val,
                                            "date": date_val,
                                            "iata": route_iata_val,
                                            "flight": flight_val,
                                            "epoch": row_epoch 
                                        })
                                        flights_added += 1

                                    if should_break or valid_html_rows == 0 or "Next flights" not in html:
                                        break
                                            
                                    page += 1
                                    await asyncio.sleep(0.5) 
                            except Exception as req_err:
                                logger.error(f"Avionio Request Error: {repr(req_err)}")
                                break
                                
                    for mode in ['arrivals', 'departures']:
                        flights = avionio_data[mode]
                        logger.info(f"[{icao_key.upper()} {iata_code.upper()} {mode.upper()}] Avionio Schedules downloaded: {len(flights)}")
                        
                        for f in flights:
                            try:
                                flt_num = f['flight']
                                if not flt_num: continue
                                
                                callsign = None
                                digits = "".join(filter(str.isdigit, flt_num))
                                iata_prefix = flt_num[:2].upper()
                                if iata_prefix in iata_to_icao:
                                    callsign = f"{iata_to_icao[iata_prefix]}{digits}"
                                else:
                                    callsign = flt_num
                                
                                safe_callsign = callsign.upper() if callsign else ""
                                safe_hex_id = None
                                
                                route_ap = f['iata']
                                safe_route_ap = route_ap.upper() if route_ap else ""
                                if len(safe_route_ap) == 3:
                                    safe_route_ap = airport_iata_to_icao.get(safe_route_ap, safe_route_ap)

                                sched_ts = f['epoch']
                                
                                # Double-check bounding box before insertion
                                if sched_ts < start_epoch or sched_ts >= end_epoch:
                                    continue
                                
                                safe_airport = icao_key.upper()
                                safe_mode = mode.upper()
                                safe_flt_num = flt_num.upper()
                                
                                async with db.pool.acquire() as conn:
                                    try:
                                        await conn.execute("""
                                            INSERT INTO flight_schedules 
                                            (airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time, created_from, updated_from) 
                                            VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7), 'AVIONIO', 'AVIONIO')
                                            ON CONFLICT (airport_code, direction, flight_number, route_airport, scheduled_time) 
                                            DO UPDATE SET hex_id = EXCLUDED.hex_id, updated_from = 'AVIONIO'
                                        """, safe_airport, safe_mode, safe_flt_num, safe_callsign, safe_hex_id, safe_route_ap, sched_ts)
                                    except Exception as ins_err:
                                        logger.error(f"Upsert failed for {safe_flt_num}: {ins_err}")
                            except Exception as e:
                                logger.error(f"Parsing Exception for Avionio flight: {repr(e)}")
                                
                else:
                    # FR24 FALLBACK LOGIC - Uses urllib with custom SSL context
                    ssl_ctx = ssl.create_default_context()
                    ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=1')
                    
                    for mode in ['arrivals', 'departures']:
                        page = 1
                        total_pages = 1
                        while page <= total_pages:
                            # THE CORRECT API ENDPOINT - DO NOT CHANGE
                            url = f"https://api.flightradar24.com/common/v1/airport.json?code={target_code}&plugin[]=&plugin-setting[schedule][mode]={mode}&plugin-setting[schedule][timestamp]={start_epoch}&page={page}&limit=100"
                            
                            try:
                                # Use urllib with custom SSL context to bypass Cloudflare
                                def fetch_fr24():
                                    req = urllib.request.Request(url, headers=headers_fr24)
                                    resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=30)
                                    return resp.status, resp.read().decode()
                                
                                status_code, body = await asyncio.get_event_loop().run_in_executor(None, fetch_fr24)
                                
                                if status_code == 200:
                                    payload = json.loads(body)
                                    res_data = payload.get('result') or {}
                                    resp_data = res_data.get('response') or {}
                                    airport_data = resp_data.get('airport') or {}
                                    plugin_data = airport_data.get('pluginData') or {}
                                    schedule_info = plugin_data.get('schedule') or {}
                                    schedule_data = schedule_info.get(mode) or {}
                                    total_pages = schedule_data.get('page', {}).get('total', 1)
                                    flights = schedule_data.get('data', [])
                                    logger.info(f"[{icao_key.upper()} {iata_code.upper()} {mode.upper()}] FR24 Schedules downloaded: {len(flights)}")
                                    for f in flights:
                                        try:
                                            flight_info = f.get('flight') or {}
                                            ident = flight_info.get('identification') or {}
                                            flt_number_obj = ident.get('number') or {}
                                            flt_num = flt_number_obj.get('default')
                                            
                                            callsign = ident.get('callsign')

                                            if callsign and isinstance(callsign, str):
                                                callsign = callsign.strip()
                                                if re.search(r'^[\x5C]*N\d+', callsign) or (len(callsign) >= 2 and ord(callsign[0]) == 92 and callsign[1] == 'N'):
                                                    callsign = None

                                            if not callsign and flt_num:
                                                digits = "".join(filter(str.isdigit, flt_num))
                                                iata_prefix = flt_num[:2].upper()
                                                if iata_prefix in iata_to_icao:
                                                    callsign = f"{iata_to_icao[iata_prefix]}{digits}"
                                                else:
                                                    callsign = flt_num

                                            safe_callsign = callsign.upper() if callsign else ""
                                            aircraft_obj = flight_info.get('aircraft') or {}
                                            hex_id = aircraft_obj.get('hex')

                                            time_info = flight_info.get('time') or {}
                                            sched_info = time_info.get('scheduled') or {}
                                            ap_info = flight_info.get('airport') or {}

                                            if mode == 'departures':
                                                sched_ts = sched_info.get('departure')
                                                route_ap = (ap_info.get('destination') or {}).get('code', {}).get('iata')
                                            else:
                                                sched_ts = sched_info.get('arrival')
                                                route_ap = (ap_info.get('origin') or {}).get('code', {}).get('iata')
                                                
                                            # Strictly drop any flight outside of our precise IST bounding box
                                            if not sched_ts or sched_ts < start_epoch or sched_ts >= end_epoch:
                                                continue
                                                
                                            if flt_num and sched_ts:
                                                safe_airport = icao_key.upper()
                                                safe_mode = mode.upper()
                                                safe_flt_num = "".join(flt_num.upper().split())
                                                safe_hex_id = hex_id.upper() if hex_id else None
                                                safe_route_ap = route_ap.upper() if route_ap else ""

                                                if len(safe_route_ap) == 3:
                                                    safe_route_ap = airport_iata_to_icao.get(safe_route_ap, safe_route_ap)

                                                async with db.pool.acquire() as conn:
                                                    try:
                                                        await conn.execute("""
                                                            INSERT INTO flight_schedules
                                                            (airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time, status)
                                                            VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7), 'SCHEDULED')
                                                            ON CONFLICT DO NOTHING
                                                        """, safe_airport, safe_mode, safe_flt_num, safe_callsign, safe_hex_id, safe_route_ap, sched_ts)
                                                    except Exception as ins_err:
                                                        logger.error(f"Upsert failed for {safe_flt_num}: {ins_err}")
                                                
                                        except Exception as e:
                                            logger.error(f"Parsing Exception for flight: {repr(e)}")
                                else:
                                    logger.warning(f"FR24 API returned HTTP {status_code} for {iata_code.upper()} (Page {page})")
                                    
                            except asyncio.TimeoutError:
                                logger.error(f"Timeout Error while fetching {iata_code.upper()} {mode} (Page {page}). Skipping page...")
                            except Exception as req_err:
                                logger.error(f"Request Error: {repr(req_err)}")
                            page += 1
                            await asyncio.sleep(3) 
            await asyncio.sleep(3) 
        logger.info("Schedule Matrix successfully updated.")
        
    except Exception as e: 
        logger.error(f"Fatal Error: {traceback.format_exc()}")


async def run_downloader(manual: bool = False):
    """Main downloader execution - runs once and exits."""
    logger.info("=" * 50)
    logger.info("BharatRadar Schedule Downloader Starting")
    logger.info("=" * 50)
    
    try:
        pool = await get_db_pool()
        db = AsyncDatabaseManager(pool)
        
        config = await get_download_config(pool)
        
        if not config:
            logger.warning("No download_config found, using defaults")
            scheduler_enabled = False
            enabled = True
        else:
            scheduler_enabled = config.get('scheduler_enabled', False)
            enabled = config.get('enabled', True)
        
        logger.info(f"Config: scheduler_enabled={scheduler_enabled}, enabled={enabled}")
        
        # Check scheduler_enabled for scheduled runs
        if not manual and not scheduler_enabled:
            logger.info("Scheduler disabled, skipping scheduled run")
            await pool.close()
            return
        
        # Check enabled for manual runs
        if manual and not enabled:
            logger.info("Downloader disabled, skipping manual run")
            await pool.close()
            return
        
        await load_config_from_db(pool)
        
        iata_to_icao = await get_iata_to_icao_map()
        
        async with aiohttp.ClientSession() as session:
            airport_iata_to_icao = await get_airport_iata_to_icao_map(session)
            await download_schedules(db, session, iata_to_icao, airport_iata_to_icao)
        
        await asyncio.sleep(5)
        await pool.close()
        
        status = "SUCCESS"
        await update_download_status(pool, status)
        logger.info(f"Download complete: {status}")
        
    except Exception as e:
        logger.error(f"Error: {e}")
        try:
            pool = await get_db_pool()
            await update_download_status(pool, f"FAILED: {str(e)[:100]}")
            await pool.close()
        except:
            pass
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='BharatRadar Schedule Downloader')
    parser.add_argument('--manual', action='store_true', 
                    help='Run manually (ignore enabled flag)')
    args = parser.parse_args()
    
    asyncio.run(run_downloader(manual=args.manual))


if __name__ == "__main__":
    main()