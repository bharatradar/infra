import math
import re
import logging
from typing import Tuple, Optional, Dict, Any
from config import Config

logger = logging.getLogger(__name__)

class AirportUtils:
    """Centralized airport code resolution and formatting."""
    
    # Class-level caches shared across instances
    _iata_to_icao_cache: Dict[str, str] = {}
    _icao_to_iata_cache: Dict[str, str] = {}
    _coords_cache: Dict[str, Tuple[float, float]] = {}
    
    def __init__(self):
        # Instance-specific overrides (if needed)
        self.airport_map: Dict[str, str] = {}
        self.airport_coords: Dict[str, Tuple[float, float]] = {}
        self.iata_to_icao_mem: Dict[str, str] = {}
    
    @classmethod
    def _load_iata_icao_maps(cls):
        """Load IATA-ICAO mappings from config and cache them."""
        if not cls._iata_to_icao_cache:
            for icao, data in Config.TARGET_AIRPORTS.items():
                iata = data.get('iata', '')
                if iata:
                    cls._iata_to_icao_cache[iata.upper()] = icao
                    cls._icao_to_iata_cache[icao] = iata
    
    def normalize_code(self, code: str) -> str:
        """Normalize airport codes with caching."""
        if not code:
            return code
        
        code_upper = str(code).strip().upper()
        
        if code_upper in ('UNK', '\\N', 'ALL', ''):
            return code_upper
        
        # Ensure mappings are loaded
        self._load_iata_icao_maps()
        
        # Check if it's already an ICAO code
        if code_upper in self._icao_to_iata_cache:
            return code_upper
        
        # Check IATA to ICAO mapping
        if code_upper in self._iata_to_icao_cache:
            return self._iata_to_icao_cache[code_upper]
        
        # Check instance cache
        if code_upper in self.iata_to_icao_mem:
            return self.iata_to_icao_mem[code_upper]
        
        return code_upper
    
    def format_airport(self, code: str, airport_map: Dict = None) -> str:
        """Format airport code with city info."""
        if not code:
            return code
        
        norm_code = self.normalize_code(code)
        if not norm_code or norm_code in ('UNK', '\\N', 'ALL'):
            return norm_code
        
        use_map = airport_map or self.airport_map
        city = use_map.get(norm_code)
        return f"{city} ({norm_code})" if city else norm_code
    
    def get_coordinates(self, code: str) -> Tuple[Optional[float], Optional[float]]:
        """Get lat/lon with multi-level caching."""
        norm_code = self.normalize_code(code)
        
        # Check config first
        if norm_code in Config.TARGET_AIRPORTS:
            ap = Config.TARGET_AIRPORTS[norm_code]
            try:
                lat, lon = float(ap.get('lat', 0)), float(ap.get('lon', 0))
                # Cache the result
                self._coords_cache[norm_code] = (lat, lon)
                return lat, lon
            except (ValueError, TypeError):
                pass
        
        # Check memory cache
        if norm_code in self._coords_cache:
            return self._coords_cache[norm_code]
        
        # Check instance cache
        if norm_code in self.airport_coords:
            return self.airport_coords[norm_code]
        
        return None, None
    
    @staticmethod
    def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great-circle distance in nautical miles."""
        R = 3440.065  # Earth radius in NM
        
        try:
            dLat = math.radians(lat2 - lat1)
            dLon = math.radians(lon2 - lon1)
            a = (math.sin(dLat/2)**2 + 
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon/2)**2)
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            return R * c
        except Exception as e:
            logger.error(f"Haversine calculation error: {e}")
            return 0.0


class CallsignUtils:
    """Centralized callsign normalization."""
    
    @staticmethod
    def normalize(callsign: str) -> Optional[str]:
        """Normalize callsign format."""
        if not callsign:
            return None
        
        clean = str(callsign).upper().replace(" ", "").strip()
        
        # Match pattern: letters + numbers + optional letters
        match = re.match(r"^([A-Z]+)(\d+)([A-Z]*)$", clean)
        
        if match:
            airline = match.group(1)
            number = match.group(2).lstrip('0') or '0'
            suffix = match.group(3)
            return f"{airline}{number}{suffix}"
        
        return clean if clean else None
    
    @staticmethod
    def is_valid(callsign: str) -> bool:
        """Validate callsign format."""
        if not callsign:
            return False
        
        clean = str(callsign).upper().replace(" ", "").strip()
        # Must be 3+ alphanumeric characters
        return bool(re.match(r"^[A-Z0-9]{3,}$", clean))


class DataConversion:
    """Safe type conversions."""
    
    @staticmethod
    def to_float(val, default: float = 0.0) -> float:
        """Safely convert to float."""
        try:
            if val is None:
                return default
            
            if isinstance(val, str):
                val_clean = val.strip().lower()
                if val_clean in ('ground', 'n/a', '-', ''):
                    return default
                return float(val_clean)
            
            return float(val)
        except (ValueError, AttributeError, TypeError):
            return default
    
    @staticmethod
    def to_int(val, default: int = 0) -> int:
        """Safely convert to int."""
        try:
            if val is None:
                return default
            return int(val)
        except (ValueError, TypeError):
            return default
    
    @staticmethod
    def extract_coords(ac_dict: Dict) -> Tuple[float, float]:
        """Extract and validate coordinates."""
        if not ac_dict:
            return 0.0, 0.0
        
        lat = DataConversion.to_float(ac_dict.get('lat'))
        lon = DataConversion.to_float(ac_dict.get('lon'))
        
        # Swap if needed (some sources invert them)
        if lat > 60.0 and lon < 40.0:
            return lon, lat
        
        return lat, lon
    
    @staticmethod
    def extract_altitude(ac_dict: Dict) -> float:
        """Extract altitude with fallback chain."""
        alt_baro = DataConversion.to_float(ac_dict.get('alt_baro'))
        alt_geom = DataConversion.to_float(ac_dict.get('alt_geom'))
        mcp = DataConversion.to_float(ac_dict.get('nav_altitude_mcp'))
        
        valid_alts = [a for a in [alt_baro, alt_geom] if a > 0]
        
        if valid_alts:
            return max(valid_alts)
        
        return max(mcp, 0.0)
    
    @staticmethod
    def extract_speed(ac_dict: Dict) -> float:
        """Extract ground speed."""
        gs = ac_dict.get('gs')
        if gs is None:
            gs = ac_dict.get('speed')
        return DataConversion.to_float(gs)
    
    @staticmethod
    def extract_vrate(ac_dict: Dict) -> float:
        """Extract vertical rate."""
        vrate = ac_dict.get('baro_rate')
        if vrate is None:
            vrate = ac_dict.get('geom_rate')
        return DataConversion.to_float(vrate)
    
    @staticmethod
    def extract_heading(ac_dict: Dict) -> float:
        """Extract heading."""
        heading = ac_dict.get('track')
        if heading is None:
            heading = ac_dict.get('heading')
        return DataConversion.to_float(heading)


class TimeUtils:
    """Time-related utilities."""
    
    @staticmethod
    def minutes_since(timestamp: float) -> int:
        """Calculate minutes since timestamp."""
        import time
        try:
            return int((time.time() - timestamp) / 60)
        except Exception:
            return 0
    
    @staticmethod
    def eta_from_distance(distance_nm: float, speed_kts: float, buffer_mins: int = 0) -> Optional[int]:
        """Calculate ETA in minutes."""
        if speed_kts <= 0 or distance_nm < 0:
            return None
        
        try:
            flight_mins = int((distance_nm / speed_kts) * 60)
            return flight_mins + buffer_mins
        except Exception:
            return None


import asyncio
import aiohttp
import urllib.request
import re
from datetime import datetime
from typing import Optional
from config import Config

logger = logging.getLogger(__name__)

CALLSIGN_CACHE: dict = {}
FR24_CACHE_TTL_SEC = 3600


async def get_iata_from_icao_fr24(callsign: str, session: aiohttp.ClientSession = None) -> tuple:
    """
    Get IATA flight number from ICAO callsign using FlightRadar24 search.
    Returns: (iata_code, iata_flight, operator) or (None, None, None)
    """
    if not callsign:
        return None, None, None
    
    norm = callsign.strip().upper().replace('-', '').replace(' ', '')
    
    cache_key = f"fr24_iata_{norm}"
    if cache_key in CALLSIGN_CACHE:
        cached = CALLSIGN_CACHE[cache_key]
        if cached.get("expires", 0) > asyncio.get_event_loop().time():
            return cached.get("iata_code"), cached.get("iata_flight"), cached.get("operator")
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        url = f"{Config.FLIGHTRADAR24_SEARCH}?query={callsign}"
        
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                
                results = data.get("results", [])
                iata_code = None
                iata_flight = None
                operator = None
                
                # FR24 returns types: "live", "schedule", "aircraft", "airport", "operator"
                for r in results:
                    rtype = r.get("type", "")
                    detail = r.get("detail", {})
                    
                    # Look for live flight first (has full route info)
                    if rtype == "live":
                        flight = detail.get("flight", "")
                        operator = detail.get("operator", "")
                        if flight:
                            # Extract IATA code from flight number (e.g., "AI1830" -> "AI", "6E123" -> "6E")
                            iata_match = re.match(r"^([A-Z0-9]{2})", flight)
                            iata_code = iata_match.group(1).upper() if iata_match else ""
                            iata_flight = flight.upper()
                            break
                    
                    # Fallback to schedule type
                    elif rtype == "schedule" and not iata_flight:
                        flight = detail.get("flight", "")
                        operator = detail.get("operator", "")
                        if flight:
                            iata_match = re.match(r"^([A-Z0-9]{2})", flight)
                            iata_code = iata_match.group(1).upper() if iata_match else ""
                            iata_flight = flight.upper()
                
                if iata_flight:
                    CALLSIGN_CACHE[cache_key] = {
                        "iata_code": iata_code,
                        "iata_flight": iata_flight,
                        "operator": operator,
                        "expires": asyncio.get_event_loop().time() + FR24_CACHE_TTL_SEC
                    }
                    logger.info(f"🔍 FR24 IATA resolved {callsign} -> {iata_code} {iata_flight}")
                    return iata_code, iata_flight, operator
                        
    except Exception as e:
        logger.warning(f"FR24 IATA lookup failed: {e}")
    finally:
        if close_session and session:
            await session.close()
    
    return None, None, None


async def get_route_from_flightaware(callsign: str, iata_flight: str, session: aiohttp.ClientSession = None) -> tuple:
    """
    Get route (origin, destination) from FlightAware.
    URL format: https://www.flightaware.com/live/flight/IGO5192 or https://www.flightaware.com/live/flight/AIC4MJ
    Returns: (origin_icao, destination_icao) or (None, None)
    """
    if not iata_flight and not callsign:
        return None, None
    
    # Try IATA flight first, then fallback to ICAO callsign
    lookup_keys = []
    if iata_flight:
        lookup_keys.append(iata_flight)
    if callsign:
        lookup_keys.append(callsign.upper())
    
    for lookup in lookup_keys:
        cache_key = f"flightaware_route_{lookup}"
        if cache_key in CALLSIGN_CACHE:
            cached = CALLSIGN_CACHE[cache_key]
            if cached.get("expires", 0) > asyncio.get_event_loop().time():
                return cached.get("origin"), cached.get("destination")
        
        import urllib.request
        import re
        
        try:
            await asyncio.sleep(0.3)
            
            url = f"{Config.FLIGHTAWARE_FLIGHT_URL}{lookup}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            })
            
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode('utf-8', errors='ignore')
                
                origin_match = re.search(r'<meta name="origin" content="([A-Z]{4})"', html)
                dest_match = re.search(r'<meta name="destination" content="([A-Z]{4})"', html)
                
                if origin_match and dest_match:
                    origin = origin_match.group(1)
                    dest = dest_match.group(1)
                    
                    CALLSIGN_CACHE[cache_key] = {
                        "origin": origin,
                        "destination": dest,
                        "iata_flight": lookup,
                        "expires": asyncio.get_event_loop().time() + FR24_CACHE_TTL_SEC
                    }
                    # Also cache with callsign key
                    if lookup != callsign.upper():
                        CALLSIGN_CACHE[f"flightaware_route_{callsign.upper()}"] = CALLSIGN_CACHE[cache_key]
                    
                    logger.info(f"✈️ FlightAware route resolved {callsign} -> {origin} → {dest}")
                    return origin, dest
                    
        except Exception as e:
            logger.warning(f"FlightAware route error for {lookup}: {e}")
            continue
    
    return None, None


async def get_route_from_adsbdb(callsign: str, iata_flight: str, hex_id: str, session: aiohttp.ClientSession = None) -> tuple:
    """
    Get route (origin, destination) from adsbdb API.
    Returns: (origin_icao, destination_icao) or (None, None)
    """
    if not iata_flight and not callsign:
        return None, None
    
    cache_key = f"adsbdb_route_{callsign or hex_id}"
    if cache_key in CALLSIGN_CACHE:
        cached = CALLSIGN_CACHE[cache_key]
        if cached.get("expires", 0) > asyncio.get_event_loop().time():
            return cached.get("origin"), cached.get("destination")
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        lookup = iata_flight if iata_flight else callsign
        
        for url_base in [Config.API_ADSB_DB_AIRCRAFT + hex_id + "?callsign=", Config.API_ADSB_DB_CALLSIGN]:
            if not hex_id and url_base == Config.API_ADSB_DB_AIRCRAFT:
                continue
            try:
                url = f"{url_base}{lookup}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        route_data = data.get("response", {}).get("flightroute", {})
                        
                        o = route_data.get("origin", {}).get("icao_code") or route_data.get("origin", {}).get("iata_code")
                        d = route_data.get("destination", {}).get("icao_code") or route_data.get("destination", {}).get("iata_code")
                        
                        if o and d:
                            CALLSIGN_CACHE[cache_key] = {
                                "origin": o.upper() if len(o) == 4 else None,
                                "destination": d.upper() if len(d) == 4 else None,
                                "iata_flight": iata_flight,
                                "expires": asyncio.get_event_loop().time() + FR24_CACHE_TTL_SEC
                            }
                            logger.info(f"✈️ adsbdb route resolved {callsign} -> {o} → {d}")
                            return o.upper(), d.upper()
            except Exception:
                pass
                        
    except Exception as e:
        logger.warning(f"adsbdb route error: {e}")
    finally:
        if close_session and session:
            await session.close()
    
    return None, None


# NOT USED - legacy FR24 HTML scraping. Replaced by fr24_data.py airline batching
async def get_route_from_fr24(callsign: str, iata_flight: str, session: aiohttp.ClientSession = None) -> tuple:
    """
    Get route (origin, destination) from FlightRadar24 HTML (utility function).
    Returns: (origin_icao, destination_icao) or (None, None)
    """
    if not iata_flight:
        return None, None
    
    cache_key = f"fr24_route_{callsign}"
    if cache_key in CALLSIGN_CACHE:
        cached = CALLSIGN_CACHE[cache_key]
        if cached.get("expires", 0) > asyncio.get_event_loop().time():
            return cached.get("origin"), cached.get("destination")
    
    import urllib.request
    import re
    from datetime import datetime
    
    try:
        await asyncio.sleep(0.5)
        
        today = datetime.utcnow()
        url = f"{Config.FLIGHTRADAR24_FLIGHTS}{iata_flight.lower()}-{today.day:02d}-{today.month:02d}-{today.year}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        })
        
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
            knu_match = re.search(r'<label>FROM</label>.*?\(([A-Z]{3})\)', html, re.DOTALL)
            if knu_match:
                origin = knu_match.group(1)
                to_match = re.search(r'<label>TO</label>.*?\(([A-Z]{3})\)', html[knu_match.start():], re.DOTALL)
                if to_match:
                    dest = to_match.group(1)
                    
                    CALLSIGN_CACHE[cache_key] = {
                        "origin": dest,
                        "destination": dest,
                        "iata_flight": iata_flight,
                        "expires": asyncio.get_event_loop().time() + FR24_CACHE_TTL_SEC
                    }
                    logger.info(f"✈️ FR24 route resolved {callsign} -> {origin} → {dest}")
                    return origin, dest
                    
    except Exception as e:
        logger.warning(f"FR24 route error: {e}")
    
    return None, None