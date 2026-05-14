import json
import logging
import httpx
import redis.asyncio as redis
from config import Config
from web_app_db import get_redis_client
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_VARS = ",".join([
    "temperature_2m", "relative_humidity_2m", "weather_code",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "pressure_msl", "visibility", "cloud_cover_low"
])

HOURLY_VARS = ",".join([
    "temperature_2m", "precipitation", "weather_code",
    "visibility", "wind_speed_10m", "wind_gusts_10m", "pressure_msl"
])

DAILY_VARS = ",".join([
    "temperature_2m_max", "temperature_2m_min",
    "precipitation_sum", "wind_speed_10m_max"
])

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
}

def describe_wmo(code):
    return WMO_CODES.get(code, "Unknown")

ASYNC_LOCK = None

async def _get_lock():
    global ASYNC_LOCK
    if ASYNC_LOCK is None:
        from asyncio import Lock
        ASYNC_LOCK = Lock()
    return ASYNC_LOCK

async def _fetch_from_openmeteo(lats, lons):
    lat_str = ",".join(f"{lat:.4f}" for lat in lats)
    lon_str = ",".join(f"{lon:.4f}" for lon in lons)
    params = {
        "latitude": lat_str,
        "longitude": lon_str,
        "current": CURRENT_VARS,
        "hourly": HOURLY_VARS,
        "daily": DAILY_VARS,
        "wind_speed_unit": "kn",
        "timezone": "auto",
        "forecast_days": 2,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(OPEN_METEO_FORECAST_URL, params=params)
        resp.raise_for_status()
        return resp.json()

def _parse_response(raw, icao):
    data = raw if isinstance(raw, dict) and "current" in raw else None
    if not data:
        return None
    c = data.get("current", {})
    hourly_raw = data.get("hourly", {})
    daily_raw = data.get("daily", {})
    hourly = []
    if hourly_raw.get("time"):
        for i in range(len(hourly_raw["time"])):
            hourly.append({
                "time": hourly_raw["time"][i],
                "temperature_2m": hourly_raw.get("temperature_2m", [None])[i],
                "weather_code": hourly_raw.get("weather_code", [None])[i],
                "precipitation": hourly_raw.get("precipitation", [None])[i],
                "visibility": hourly_raw.get("visibility", [None])[i],
                "wind_speed_10m": hourly_raw.get("wind_speed_10m", [None])[i],
                "wind_gusts_10m": hourly_raw.get("wind_gusts_10m", [None])[i],
                "pressure_msl": hourly_raw.get("pressure_msl", [None])[i],
            })
    daily = []
    if daily_raw.get("time"):
        for i in range(len(daily_raw["time"])):
            daily.append({
                "date": daily_raw["time"][i],
                "temperature_2m_max": daily_raw.get("temperature_2m_max", [None])[i],
                "temperature_2m_min": daily_raw.get("temperature_2m_min", [None])[i],
                "precipitation_sum": daily_raw.get("precipitation_sum", [None])[i],
                "wind_speed_10m_max": daily_raw.get("wind_speed_10m_max", [None])[i],
            })
    wcode = c.get("weather_code", 0)
    return {
        "icao": icao,
        "current": {
            "temperature_2m": c.get("temperature_2m"),
            "relative_humidity_2m": c.get("relative_humidity_2m"),
            "weather_code": wcode,
            "weather_description": describe_wmo(wcode),
            "wind_speed_10m": c.get("wind_speed_10m"),
            "wind_direction_10m": c.get("wind_direction_10m"),
            "wind_gusts_10m": c.get("wind_gusts_10m"),
            "pressure_msl": c.get("pressure_msl"),
            "visibility": c.get("visibility"),
            "cloud_cover_low": c.get("cloud_cover_low"),
        },
        "hourly": hourly[:12],
        "daily": daily,
        "alerts": _check_thresholds(c),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }

def _check_thresholds(current):
    alerts = []
    vis = current.get("visibility")
    gust = current.get("wind_gusts_10m")
    wcode = current.get("weather_code", 0)
    temp = current.get("temperature_2m")
    if vis is not None:
        if vis < 1000:
            alerts.append("dense_fog")
        elif vis < 3000:
            alerts.append("low_visibility")
    if gust is not None:
        if gust >= 40:
            alerts.append("extreme_wind")
        elif gust >= 30:
            alerts.append("strong_wind")
        elif gust >= 25:
            alerts.append("windy")
    if wcode in (95, 96, 99):
        alerts.append("thunderstorm")
    if wcode in (71, 73, 75, 77, 85, 86):
        alerts.append("snow")
    if temp is not None and temp > 42:
        alerts.append("extreme_heat")
    return alerts

async def _get_airport_coords(icao):
    from config import Config
    icao = icao.upper().strip()
    data = getattr(Config, 'TARGET_AIRPORTS', {}).get(icao)
    if data:
        lat, lon = data.get('lat'), data.get('lon')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    from web_app_db import AIRPORT_COORDS
    if icao in AIRPORT_COORDS:
        return AIRPORT_COORDS[icao]
    try:
        import asyncpg
        conn = await asyncpg.connect(**Config.POSTGRES_PARAMS)
        try:
            row = await conn.fetchrow("SELECT lat, lon FROM airports WHERE icao = $1", icao)
            if row and row['lat'] and row['lon']:
                lat, lon = float(row['lat']), float(row['lon'])
                AIRPORT_COORDS[icao] = (lat, lon)
                return lat, lon
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"Could not get coords for {icao}: {e}")
    return None, None

async def get_airport_weather(icao):
    icao = icao.upper().strip()
    r = await get_redis_client()
    cache_key = f"weather:current:{icao}"
    cached = await r.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass
    coords = await _get_airport_coords(icao)
    if not coords[0] or not coords[1]:
        return {"error": f"Unknown airport: {icao}"}
    lat, lon = coords
    try:
        raw = await _fetch_from_openmeteo([lat], [lon])
        result = _parse_response(raw, icao)
        if result:
            await r.setex(cache_key, 900, json.dumps(result))
        return result
    except Exception as e:
        logger.warning(f"Weather fetch failed for {icao}: {e}")
        return {"error": str(e)}

async def get_weather_by_coords(lat, lon, label="My Location"):
    cache_key = f"weather:current:coord:{lat:.2f}:{lon:.2f}"
    r = await get_redis_client()
    cached = await r.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        raw = await _fetch_from_openmeteo([lat], [lon])
        result = _parse_response(raw, label)
        if result:
            result["icao"] = label
            await r.setex(cache_key, 900, json.dumps(result))
        return result
    except Exception as e:
        logger.warning(f"Weather fetch failed for coords ({lat},{lon}): {e}")
        return {"error": str(e)}

async def get_batch_weather(icaos):
    if not icaos:
        return {"airports": []}
    icaos = [i.upper().strip() for i in icaos]
    r = await get_redis_client()
    results = []
    uncached = []
    uncached_idxs = []
    for idx, icao in enumerate(icaos):
        cached = await r.get(f"weather:current:{icao}")
        if cached:
            try:
                results.append(json.loads(cached))
                continue
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(None)
        uncached.append(icao)
        uncached_idxs.append(idx)
    if uncached:
        lats, lons = [], []
        valid_uncached = []
        valid_idxs = []
        for i, icao in enumerate(uncached):
            coords = await _get_airport_coords(icao)
            if coords:
                lats.append(coords[0])
                lons.append(coords[1])
                valid_uncached.append(icao)
                valid_idxs.append(uncached_idxs[i])
        if lats:
            try:
                raw = await _fetch_from_openmeteo(lats, lons)
                if isinstance(raw, list):
                    for raw_i, vi in enumerate(raw):
                        idx = valid_idxs[raw_i] if raw_i < len(valid_idxs) else None
                        if idx is not None and idx < len(results):
                            parsed = _parse_response(vi, valid_uncached[raw_i] if raw_i < len(valid_uncached) else "UNK")
                            if parsed:
                                results[idx] = parsed
                                await r.setex(f"weather:current:{parsed['icao']}", 900, json.dumps(parsed))
                elif isinstance(raw, dict):
                    parsed = _parse_response(raw, valid_uncached[0] if valid_uncached else "UNK")
                    if parsed:
                        results[valid_idxs[0]] = parsed
                        await r.setex(f"weather:current:{parsed['icao']}", 900, json.dumps(parsed))
            except Exception as e:
                logger.warning(f"Batch weather fetch failed: {e}")
    return {"airports": [r for r in results if r is not None]}

WEATHER_DELAY_FACTORS = [
    ("visibility", lambda v: v is not None and v < 1000, 20),
    ("visibility", lambda v: v is not None and v < 3000, 10),
    ("visibility", lambda v: v is not None and v < 5000, 5),
    ("wind_gusts_10m", lambda g: g is not None and g >= 40, 15),
    ("wind_gusts_10m", lambda g: g is not None and g >= 30, 8),
    ("wind_gusts_10m", lambda g: g is not None and g >= 25, 4),
    ("precipitation", lambda p: p is not None and p >= 10, 10),
    ("precipitation", lambda p: p is not None and p >= 5, 5),
    ("snow", True, 20),
    ("weather_code", lambda w: w in (95, 96, 99), 25),
    ("cloud_cover_low", lambda c: c is not None and c > 80, 8),
    ("cloud_cover_low", lambda c: c is not None and c > 60, 4),
]

async def get_weather_delay_minutes(icao):
    weather = await get_airport_weather(icao)
    if not weather or "error" in weather:
        return 0
    c = weather.get("current", {})
    delay = 0
    for field, condition, amount in WEATHER_DELAY_FACTORS:
        if callable(condition):
            val = c.get(field)
            if condition(val):
                delay = max(delay, amount)
        elif condition and field == "snow":
            if c.get("weather_code") in (71, 73, 75, 77, 85, 86):
                delay = max(delay, amount)
    hourly = weather.get("hourly", [])
    if hourly:
        peak = 0
        for h in hourly[:6]:
            hdelay = 0
            for field, condition, amount in WEATHER_DELAY_FACTORS:
                if callable(condition):
                    val = h.get(field)
                    if condition(val):
                        hdelay = max(hdelay, amount)
                elif condition and field == "snow":
                    if h.get("weather_code") in (71, 73, 75, 77, 85, 86):
                        hdelay = max(hdelay, amount)
            peak = max(peak, hdelay)
        if peak > delay:
            delay = peak
    return delay