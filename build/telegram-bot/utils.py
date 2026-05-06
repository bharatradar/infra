# Utils copied from flight-tracker to enable FR24 route resolution
import asyncio
import logging
import aiohttp
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

FR24_API = "https://www.flightradar24.com/interactService/appv5/"

async def get_iata_from_icao_fr24(callsign: str, session: aiohttp.ClientSession = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve ICAO callsign to IATA flight number via FR24."""
    if not callsign or len(callsign) < 4:
        return None, None, None
    
    ics = callsign.upper().strip()
    
    # Map common ICAO prefixes to IATA
    icao_to_iata = {
        "AIC": "AI", "ASI": "6E", "AXB": "I5", "VTE": "VT", "IGO": "IG", 
        "SPD": "ID", "GAI": "G8", "AIT": "IX", "ANA": "AK", "JAI": "9W",
        "SBI": "SB", "SKY": "S2", "TGS": "TG", "HAL": "H1", "BBA": "DI"
    }
    prefix = ics[:3]
    iata_prefix = icao_to_iata.get(prefix, prefix[:2] if len(prefix) >= 2 else "")
    
    if len(ics) < 4:
        return None, None, None
    
    # Try to extract number: AIC808 -> 808, AIC0808 -> 0808
    num = ics[3:] if len(ics) > 3 else ics[2:]
    if len(num) < 3:
        return None, None, None
    
    iata_flight = f"{iata_prefix}{num}"
    own_session = False
    
    try:
        if not session:
            session = aiohttp.ClientSession()
            own_session = True
        
        params = {"flights": f"{iata_flight.lower()}", "extend": "1"}
        async with session.get(FR24_API, params=params, timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                try:
                    d = await r.json()
                    if d.get("result").get("response"):
                        flight_data = d["result"]["response"][0]
                        if flight_data.get("status") == 1:
                            return iata_prefix[:2] if iata_prefix else "", iata_flight, flight_data.get("operator", "").upper()
                except:
                    pass
    except Exception as e:
        logger.warning(f"FR24 lookup failed for {callsign}: {e}")
    finally:
        if own_session and session:
            await session.close()
    
    return None, None, None


def calculate_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in nautical miles."""
    import math
    R = 3440.065  # Earth radius in NM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))