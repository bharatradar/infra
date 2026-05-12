import logging
import re
import csv
import time
import os
import asyncio
from config import Config
from utils import CALLSIGN_CACHE

logger = logging.getLogger(__name__)

AIRPORT_IATA_MAP = {}
AIRPORT_COORDS = {}
AIRLINE_IATA_MAP = {}
SEEN_AIRCRAFT_INFO = set()


def extract_bounds():
    url = getattr(Config, 'ADSB_EXCHANGE_BINCRAFT_URL', '')
    match = re.search(r'box=([\d.]+),([\d.]+),([\d.]+),([\d.]+)', url)
    if match:
        min_lat, max_lat, min_lon, max_lon = match.groups()
        return f"{max_lat},{min_lat},{max_lon},{min_lon}"
    return None


def load_airport_maps():
    path = Config.AIRPORTS_CSV_FILE
    if not os.path.exists(path):
        logger.warning(f"airports.csv not found at {path}")
        return
    with open(path, mode='r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            iata = row.get('IATA', '').strip().upper()
            icao = row.get('ICAO', '').strip().upper()
            if iata and icao:
                AIRPORT_IATA_MAP[iata] = icao
                if icao not in AIRPORT_COORDS:
                    try:
                        AIRPORT_COORDS[icao] = {
                            'lat': float(row.get('lat', 0)),
                            'lon': float(row.get('lon', 0))
                        }
                    except (ValueError, TypeError):
                        pass
    for icao, data in Config.TARGET_AIRPORTS.items():
        icao_upper = icao.upper()
        if data.get('iata'):
            AIRPORT_IATA_MAP[data['iata'].upper()] = icao_upper
        if icao_upper not in AIRPORT_COORDS and data.get('lat'):
            AIRPORT_COORDS[icao_upper] = {
                'lat': float(data['lat']),
                'lon': float(data['lon'])
            }
    logger.info(f"Loaded {len(AIRPORT_IATA_MAP)} airport IATA->ICAO mappings")


def load_airline_iata_map():
    path = Config.AIRLINES_FILE
    if not os.path.exists(path):
        logger.warning(f"airlines.csv not found at {path}")
        return
    with open(path, mode='r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if str(row.get('Active', 'Y')).strip().upper() != 'N':
                icao = row.get('ICAO', '').strip().upper()
                iata = row.get('IATA', '').strip().upper()
                if icao and iata:
                    AIRLINE_IATA_MAP[icao] = iata
    logger.info(f"Loaded {len(AIRLINE_IATA_MAP)} airline ICAO->IATA mappings")


def get_airline_iata(airline_icao):
    return AIRLINE_IATA_MAP.get(airline_icao.upper())


def resolve_airport(iata_code):
    iata = iata_code.upper() if iata_code else ''
    if not iata:
        return None, None, None
    icao = AIRPORT_IATA_MAP.get(iata)
    coords = AIRPORT_COORDS.get(icao, {}) if icao else {}
    return icao, coords.get('lat'), coords.get('lon')


def parse_fr24_aircraft(key, arr):
    return {
        'hex_id': arr[0],
        'lat': arr[1],
        'lon': arr[2],
        'heading': arr[3],
        'alt': arr[4],
        'speed': arr[5],
        'ac_type': arr[8] if len(arr) > 8 else None,
        'registration': arr[9] if len(arr) > 9 else None,
        'origin_iata': arr[11] if len(arr) > 11 else None,
        'dest_iata': arr[12] if len(arr) > 12 else None,
        'flight_number_iata': arr[13] if len(arr) > 13 else None,
        'callsign_icao': arr[16] if len(arr) > 16 else None,
        'airline_icao': arr[18] if len(arr) > 18 else None,
    }


async def fetch_airline_data(session, airline_code):
    url = f"{Config.FR24_DATA_URL}?airline={airline_code}&air=1"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Origin': 'https://www.flightradar24.com',
        'Referer': 'https://www.flightradar24.com/',
    }
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = []
                for key, value in data.items():
                    if key in ('full_count', 'version', 'stats'):
                        continue
                    if isinstance(value, list) and len(value) >= 14:
                        results.append(parse_fr24_aircraft(key, value))
                logger.debug(f"FR24 {airline_code}: {len(results)} aircraft")
                return results
            else:
                logger.warning(f"FR24 {airline_code}: HTTP {resp.status}")
                return []
    except Exception as e:
        logger.warning(f"FR24 {airline_code} fetch failed: {e}")
        return []


def resolve_callsign_iata(callsign):
    if not callsign or len(callsign) < 3:
        return None
    cache_key = f"fr24_iata_{callsign.strip().upper()}"
    cached = CALLSIGN_CACHE.get(cache_key)
    if cached and cached.get('iata_code'):
        return cached['iata_code']
    return None
