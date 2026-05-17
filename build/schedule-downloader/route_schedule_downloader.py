import asyncio
import aiohttp
import asyncpg
import time
import os
import csv
import logging
import traceback
import re
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from db import AsyncDatabaseManager
import importlib 
import config    

# 🌟 STRICT TIMEZONE: Server's UTC time won't ruin the day-math
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def get_iata_to_icao_map():
    """Builds the airline code map needed to deduce missing callsigns."""
    iata_to_icao = {}
    if os.path.exists(config.Config.AIRLINES_FILE):
        try:
            with open(config.Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    if str(row.get('Active', 'Y')).strip().upper() != 'N':
                        icao = row.get('ICAO', '').strip()
                        iata = row.get('IATA', '').strip()
                        if iata and icao:
                            iata_to_icao[iata] = icao
            logger.info(f"✅ Loaded {len(iata_to_icao)} IATA->ICAO mappings for Callsign deduction")
        except Exception as e: 
            logger.error(f"Failed to load airlines: {e}")
    return iata_to_icao

async def get_airport_iata_to_icao_map(session: aiohttp.ClientSession):
    """Builds an airport mapping from IATA to ICAO using the airports.csv file."""
    airport_map = {}
    for icao, data in config.Config.TARGET_AIRPORTS.items():
        if data.get('iata'):
            airport_map[data['iata'].upper()] = icao.upper()

    file_path = "data/airports.csv"
    try:
        if not os.path.exists(file_path):
            os.makedirs("data", exist_ok=True)
            url = getattr(config.Config, 'AIRPORTS_CSV_URL', "https://vrs-standing-data.adsb.lol/airports.csv")
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
        logger.info(f"✅ Loaded {len(airport_map)} Airport IATA->ICAO mappings")
    except Exception as e:
        logger.error(f"Failed to load airports.csv: {e}")
    return airport_map

def _normalize_number(number):
    return "".join(number.upper().split())

async def download_from_flightsfrom(db: AsyncDatabaseManager, session: aiohttp.ClientSession, airport_iata_to_icao: dict, days_to_run: list = None):
    logger.info("📅 Fetching Schedules from FlightsFrom (PRIMARY)...")

    if days_to_run is None:
        days_to_run = getattr(config.Config, 'GET_SCHEDULES_FOR', ['TODAY', 'TOMORROW'])

    now_ist = datetime.now(IST)
    offsets = []
    if 'TODAY' in days_to_run: offsets.append(0)
    if 'TOMORROW' in days_to_run: offsets.append(1)

    for day_offset in offsets:
        target_date = now_ist + timedelta(days=day_offset)
        date_str = target_date.strftime('%Y-%m-%d')
        day_key = f'day{target_date.isoweekday()}'

        midday_epoch = int(target_date.replace(hour=12, minute=0, second=0, microsecond=0).timestamp())

        for icao_key, data in config.Config.TARGET_AIRPORTS.items():
            iata_code = data.get('iata', '').upper()
            if not iata_code:
                continue

            url = f'https://www.flightsfrom.com/api/airport/{iata_code}'
            airport_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Referer': f'https://www.flightsfrom.com/{iata_code}',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Origin': 'https://www.flightsfrom.com',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            }

            for entity_type in ('departures', 'arrivals'):
                params = {
                    'from': iata_code,
                    'entityType': entity_type,
                    'take': '1000',
                    'sorting': 'departure-time',
                    'sortingDirection': 'asc',
                    'selectedDate': date_str,
                    'dateMethod': 'day',
                    'dateFrom': date_str,
                    'dateTo': date_str,
                }

                try:
                    async with session.get(url, params=params, headers=airport_headers, timeout=60) as resp:
                        if resp.status != 200:
                            logger.warning(f'⚠️ FlightsFrom HTTP {resp.status} for {iata_code} {entity_type}')
                            continue

                        payload = await resp.json()
                        routes = payload.get('response', {}).get('routes', {})
                        direction = entity_type.upper()
                        route_count = 0

                        route_items = routes.values() if isinstance(routes, dict) else routes
                        for route in route_items:
                            if route.get(day_key) != 'yes':
                                continue

                            route_iata = route.get('iata_to' if entity_type == 'departures' else 'iata_from', '')
                            if not route_iata:
                                continue

                            route_icao = airport_iata_to_icao.get(route_iata.upper(), route_iata)

                            for ar in route.get('airlineroutes', []):
                                carrier = ar.get('carrier', '').upper()
                                icao_code = ar.get('airline', {}).get('ICAO', '').upper()
                                if not carrier and not icao_code:
                                    continue

                                safe_route = route_icao.upper() if route_icao else ''
                                async with db.pool.acquire() as conn:
                                    try:
                                        await conn.execute("""
                                            INSERT INTO flight_schedules
                                            (airport_code, direction, flight_number, callsign, route_airport, scheduled_time, created_from, updated_from)
                                            VALUES ($1, $2, $3, $4, $5, TO_TIMESTAMP($6), 'FLIGHTSFROM', 'FLIGHTSFROM')
                                            ON CONFLICT (airport_code, direction, flight_number, route_airport, scheduled_time)
                                            DO UPDATE SET callsign = EXCLUDED.callsign, updated_from = 'FLIGHTSFROM'
                                        """, icao_key.upper(), direction, carrier, icao_code, safe_route, midday_epoch)
                                        route_count += 1
                                    except Exception as ins_err:
                                        logger.error(f'⚠️ FlightsFrom upsert failed for {carrier}/{icao_code}: {ins_err}')

                        logger.info(f'[{icao_key.upper()} {iata_code.upper()} {direction}] FlightsFrom routes: {route_count}')

                except asyncio.TimeoutError:
                    logger.error(f'⏳ FlightsFrom Timeout for {iata_code} {entity_type}')
                except Exception as req_err:
                    logger.error(f'❌ FlightsFrom Error for {iata_code}: {repr(req_err)}')

                await asyncio.sleep(1)

            await asyncio.sleep(1)

    logger.info('✅ FlightsFrom Schedule download complete.')


async def download_schedules(db: AsyncDatabaseManager, session: aiohttp.ClientSession, iata_to_icao: dict, airport_iata_to_icao: dict):
    logger.info("📅 Fetching Schedules...")
    
    get_from_flightsfrom = getattr(config.Config, 'GET_SCHEDULES_FROM_FLIGHTSFROM', False)
    get_from_avionio = getattr(config.Config, 'GET_SCHEDULES_FROM_AVIONIO', False)
    missing_avionio = getattr(config.Config, 'MISSING_AIRPORTS_IN_AVIONIO', {})
    days_to_run = getattr(config.Config, 'GET_SCHEDULES_FOR', ["TODAY", "TOMORROW"])
    
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
            
            # 🌟 STRICT EPOCH BOUNDING: Exactly bounds Today and Tomorrow in IST
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
                                    
            for icao_key, data in config.Config.TARGET_AIRPORTS.items():
                iata_code = data.get('iata', '').upper()
                if not iata_code: continue
                target_code = iata_code.lower()
                
                use_avionio = False
                if get_from_avionio and icao_key not in missing_avionio:
                    use_avionio = True
                    
                if use_avionio:
                    # ==========================================
                    # 🌟 AVIONIO SCRAPING LOGIC
                    # ==========================================
                    avionio_data = {"arrivals": [], "departures": []}
                    base_url = f"https://www.avionio.com/widget/en/{iata_code.upper()}/"
                    
                    for mode in ['arrivals', 'departures']:
                        page = 0
                        
                        while True:
                            url = f"{base_url}{mode}?ts={target_ts_ms}&page={page}"
                            try:
                                async with session.get(url, headers=headers_avionio, timeout=30) as resp:
                                    if resp.status != 200:
                                        logger.warning(f"⚠️ Avionio returned HTTP {resp.status} for {iata_code} {mode} page {page}")
                                        break
                                    html = await resp.text()
                                    
                                    soup = BeautifulSoup(html, "html.parser")
                                    table = soup.find("table")
                                    
                                    if not table:
                                        break
                                        
                                    rows = table.find_all("tr")
                                    
                                    # 🌟 THE FIX: Track physical HTML rows to prevent false-breaks
                                    valid_html_rows = 0
                                    flights_added = 0
                                    should_break = False
                                    
                                    for row in rows:
                                        cols = row.find_all(["td", "th"])
                                        if len(cols) < 7 or "Time" in cols[0].text:
                                            continue
                                            
                                        valid_html_rows += 1  # We saw a real flight row!
                                        
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

                                    # 🌟 THE FIX: Only kill the scraper if the page had NO valid HTML rows at all
                                    if should_break or valid_html_rows == 0 or "Next flights" not in html:
                                        break
                                        
                                    page += 1
                                    await asyncio.sleep(0.5) 
                            except Exception as req_err:
                                logger.error(f"❌ Avionio Request Error: {repr(req_err)}")
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
                                safe_flt_num = _normalize_number(flt_num)
                                
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
                                        logger.error(f"⚠️ Upsert failed for {safe_flt_num}: {ins_err}")
                            except Exception as e:
                                logger.error(f"⚠️ Parsing Exception for Avionio flight: {repr(e)}")
                                
                else:
                    # ==========================================
                    # 🌟 FR24 FALLBACK LOGIC
                    # ==========================================
                    for mode in ['arrivals', 'departures']:  
                        page = 1
                        total_pages = 1                   
                        while page <= total_pages:
                            url = f"https://api.flightradar24.com/common/v1/airport.json?code={target_code}&plugin[]=&plugin-setting[schedule][mode]={mode}&plugin-setting[schedule][timestamp]={start_epoch}&page={page}&limit=100"
                            
                            try:
                                async with session.get(url, headers=headers_fr24, timeout=300) as resp:
                                    if resp.status == 200:
                                        payload = await resp.json()
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
                                                    safe_flt_num = _normalize_number(flt_num)
                                                    safe_hex_id = hex_id.upper() if hex_id else None
                                                    safe_route_ap = route_ap.upper() if route_ap else ""

                                                    if len(safe_route_ap) == 3:
                                                        safe_route_ap = airport_iata_to_icao.get(safe_route_ap, safe_route_ap)

                                                    async with db.pool.acquire() as conn:
                                                        try:
                                                            await conn.execute("""
                                                                INSERT INTO flight_schedules 
                                                                (airport_code, direction, flight_number, callsign, hex_id, route_airport, scheduled_time, created_from, updated_from) 
                                                                VALUES ($1, $2, $3, $4, $5, $6, TO_TIMESTAMP($7), 'FLIGHT_RADAR', 'FLIGHT_RADAR')
                                                                ON CONFLICT (airport_code, direction, flight_number, route_airport, scheduled_time) 
                                                                DO UPDATE SET hex_id = EXCLUDED.hex_id, updated_from = 'FLIGHT_RADAR'
                                                            """, safe_airport, safe_mode, safe_flt_num, safe_callsign, safe_hex_id, safe_route_ap, sched_ts)
                                                        except Exception as ins_err:
                                                            logger.error(f"⚠️ Upsert failed for {safe_flt_num}: {ins_err}")
                                                    
                                            except Exception as e: 
                                                logger.error(f"⚠️ Parsing Exception for flight: {repr(e)}")
                                    else:
                                        logger.warning(f"⚠️ FR24 API returned HTTP {resp.status} for {iata_code.upper()} (Page {page})")
                                        
                            except asyncio.TimeoutError:
                                logger.error(f"⏳ Timeout Error (300s) while fetching {iata_code.upper()} {mode} (Page {page}). Skipping page...")
                            except Exception as req_err:
                                logger.error(f"❌ Request Error: {repr(req_err)}")
                            page += 1
                            await asyncio.sleep(3) 
            await asyncio.sleep(3) 
        logger.info("✅ Schedule Matrix successfully updated.")
        
    except Exception as e: 
        logger.error(f"❌ Fatal Error: {traceback.format_exc()}")

async def execute_download(iata_to_icao: dict, days_to_run: list = None):
    """Creates fresh connections, does the work, and closes them."""
    importlib.reload(config)
    
    pool = await asyncpg.create_pool(**config.Config.DB_PARAMS)
    db = AsyncDatabaseManager(pool)

    async with aiohttp.ClientSession() as session:
        airport_iata_to_icao = await get_airport_iata_to_icao_map(session)
        get_from_flightsfrom = getattr(config.Config, 'GET_SCHEDULES_FROM_FLIGHTSFROM', False)
        if get_from_flightsfrom:
            await download_from_flightsfrom(db, session, airport_iata_to_icao, days_to_run)
        else:
            await download_schedules(db, session, iata_to_icao, airport_iata_to_icao)
    await asyncio.sleep(5)
    await pool.close()

async def compute_next_run_time(db: AsyncDatabaseManager) -> datetime:
    """Compute next run: 1.5h before the earliest 'last flight' among all airports today (IST)."""
    now_ist = datetime.now(IST)
    now_naive = now_ist.replace(tzinfo=None)
    today_midnight_naive = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_midnight_naive = today_midnight_naive + timedelta(days=1)
    next_6am = now_naive.replace(hour=6, minute=0, second=0, microsecond=0)
    fallback = next_6am if now_naive < next_6am else next_6am + timedelta(days=1)

    try:
        async with db.pool.acquire() as conn:
            noon_naive = today_midnight_naive.replace(hour=12)
            row = await conn.fetchrow("""
                SELECT MIN(airport_last_flight) AS earliest_last_flight
                FROM (
                    SELECT airport_code, MAX(scheduled_time) AS airport_last_flight
                    FROM flight_schedules
                    WHERE scheduled_time >= $1 AND scheduled_time < $2
                    GROUP BY airport_code
                    HAVING MAX(scheduled_time) >= $3
                ) sub
            """, today_midnight_naive, tomorrow_midnight_naive, noon_naive)
    except Exception as e:
        logger.error(f"compute_next_run_time query failed: {e}")
        return fallback

    if row and row['earliest_last_flight']:
        next_time = row['earliest_last_flight'] - timedelta(hours=1, minutes=30)
        if next_time > now_naive:
            logger.info(f"Next run scheduled at {next_time} (1.5h before earliest last flight)")
            return next_time

    logger.info(f"Using fallback next_run: {fallback}")
    return fallback

async def main():
    importlib.reload(config)
    iata_to_icao = await get_iata_to_icao_map()

    config.Config.GET_SCHEDULES_FOR = ['TODAY', 'TOMORROW']
    logger.info("📅 Fetching schedules: TODAY, TOMORROW")
    await execute_download(iata_to_icao, ['TODAY', 'TOMORROW'])

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass