# config.py
# AI Agents - K3s-optimized config
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

def _env(key, default=None):
    return os.environ.get(key, default)

class Config:
    DEBUG_MODE = True
    APP_NAME = "raga_ai_agents"
    
    USE_LOCAL_LLM = False 
    BOT_ENGINE_MODE = "fast_router"
    FAST_ROUTER_PROVIDER = "groq"
    FAST_ROUTER_FALLBACK_QUEUE = ["groq", "cloudflare", "llama-server", "local_gguf"]
    LLAMA_SERVER_URL = _env("LLAMA_SERVER_URL", "http://192.168.100.44:8080/v1/chat/completions")

    LOCAL_GGUF_PATH = _env("LOCAL_GGUF_PATH", "/home/ragaradar/raga-radar-v2-q8_0-local.gguf")
    LOCAL_GGUF_CTX = 1024
    FAST_ROUTER_MODEL = "llama-3.1-8b-instant"
    SEMANTIC_ROUTING_MODE = "llm_classifier"

    ETA_ALERT_CHECK_INTERVAL_SEC = 60
    RADAR_QUEUE_MAXSIZE = 2
    CONCURRENT_PROCESSING_LIMIT = 15
    TAKEOFF_INFERENCE_MAX_DIST_KM = 150
    CLEAR_AIRBORNE_MIN_ALT_FT = 3000

    ADSB_EXCHANGE_BINCRAFT_URL = "https://globe.adsbexchange.com/re-api/?binCraft&zstd&box=9.337602,33.583193,60.875230,104.092506"
    LOCAL_AIRCRAFT_DATA_URL = "http://192.168.1.3/tar1090/data/aircraft.json"
    ADSB_LOL_AIRCRAFT_DATA_URL = "https://api.adsb.lol/v2/point/21.1458/79.0882/150"
    RE_ADSB_LOL_AIRCRAFT_DATA_URL = "https://re-api.adsb.lol/?circle=21.1458,79.0882,1000"
    ADSB_ONE_AIRCRAFT_DATA_URL = "https://api.adsb.one/v2/point/21.1458/79.0882/1000"

    PROXY_URL = _env("PROXY_URL", "http://192.168.100.19:8888")
    API_ADSB_DB_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft/"
    API_ADSB_DB_CALLSIGN = "https://api.adsbdb.com/v0/callsign/"
    FLIGHTRADAR24_SEARCH = "https://www.flightradar24.com/v1/search/web/find"
    FLIGHTRADAR24_FLIGHTS = "https://www.flightradar24.com/data/flights/"
    FLIGHTAWARE_ROUTE_ENABLED = True
    FLIGHTAWARE_FLIGHT_URL = "https://www.flightaware.com/live/flight/"
    ROUTE_RESOLUTION_ORDER = ["flightaware", "adsbdb"]
    ROUTES_CSV_URL = "https://vrs-standing-data.adsb.lol/routes.csv"
    AIRPORTS_CSV_URL = "https://vrs-standing-data.adsb.lol/airports.csv"

    APPROACH_RADIUS_KM = 185
    TERMINAL_AREA_RADIUS_KM = 50
    LANDING_RADIUS_KM = 10
    TAKEOFF_WAKEUP_RADIUS_KM = 20
    LANDING_ALT_THRESH_FT = 2000
    MAX_TRACKING_ALTITUDE = 40000
    APPROACH_ALT_THRESH_FT = 15000
    FINAL_APPROACH_RADIUS_KM = 100
    FINAL_APPROACH_ALT_FT = 3500
    ASSUMED_LANDING_TIMEOUT_SEC = 180

    PYTHON_PATH = "python"
    GET_SCHEDULES_FOR = ["TODAY", "TOMORROW"]
    GET_SCHEDULES_FROM_AVIONIO = False
    MISSING_AIRPORTS_IN_AVIONIO = {'VEAY': 'AYJ', 'VEDO': 'DGH', 'VIJW': 'DXN', 'VIDX': 'HDO', 'VEHO': 'HGI', 'VIAH': 'HRH', 'VAHS': 'HSR', 'VOKU': 'KJB', 'VANM': 'NMI', 'VARW': 'REW', 'VOSR': 'SDW'}

    DB_PARAMS = {
        "database": _env("DB_NAME", "flight_db"),
        "user": _env("DB_USER", "flight_db_user"),
        "password": _env("DB_PASSWORD", "flight_db_password"),
        "host": _env("DB_HOST", "localhost"),
        "port": _env("DB_PORT", "5432")
    }
    REDIS_PARAMS = {
        "host": _env("REDIS_HOST", "127.0.0.1"),
        "port": int(_env("REDIS_PORT", "6379")),
        "password": _env("REDIS_PASSWORD", None),
        "db": int(_env("REDIS_DB", "0")),
        "decode_responses": True
    }

    REDIS_LIVE_FLIGHTS_KEY = "live_flights"
    REDIS_LIVE_FLIGHTS_META_KEY = "live_flights_meta"
    REDIS_FLIGHTS_TTL = 30

    WS_ENABLED = _env("WS_ENABLED", "true").lower() == "true"
    WS_HOST = _env("WS_HOST", "localhost")
    WS_PORT = int(_env("WS_PORT", "8002"))
    WS_URL = f"ws://{WS_HOST}:{WS_PORT}" if WS_ENABLED else None

    INFLUXDB_URL = _env("INFLUXDB_URL", "http://localhost:8086")
    INFLUXDB_TOKEN = _env("INFLUXDB_TOKEN")
    INFLUXDB_ORG = _env("INFLUXDB_ORG", "Vellur")
    INFLUXDB_BUCKET = "raga_flight_radar_db"
    INFLUXDB_WRITE_INTERVAL_SEC = 5

    RADAR_FETCH_INTERVAL_SEC = 10
    WEBSOCKET_BROADCAST_INTERVAL_SEC = 1
    GAP_FILLER_INTERVAL_SEC = 10
    ENRICHMENT_FETCH_DELAY_SEC = 2
    JANITOR_INTERVAL_SEC = 900
    FORENSIC_JANITOR_INTERVAL_SEC = 900
    WATCHDOG_CHECK_INTERVAL_SEC = 120
    DAILY_ANALYST_INTERVAL_SEC = 900
    FEEDER_HEALTH_CHECK_INTERVAL_SEC = 30
    STATS_PARSER_INTERVAL_SEC = 120
    BACKGROUND_TASK_STARTUP_DELAY_SEC = 5
    STATS_PARSER_STARTUP_DELAY_SEC = 10
    FRONTEND_RADAR_POLL_INTERVAL_MS = 5000
    FRONTEND_ATC_POLL_INTERVAL_MS = 5000
    FRONTEND_OPS_POLL_INTERVAL_MS = 30000
    FRONTEND_EXEC_POLL_INTERVAL_MS = 60000
    FRONTEND_WS_RECONNECT_DELAY_MS = 3000

    TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
    GROQ_API_KEY = _env("GROQ_API_KEY")

    _openrouter_keys = _env("OPENROUTER_API_KEYS", "")
    OPENROUTER_API_KEYS = [k.strip() for k in _openrouter_keys.split(",") if k.strip()] if _openrouter_keys else []
    GOOGLE_STUDIO_API_KEY = _env("GOOGLE_STUDIO_API_KEY")
    CLOUDFLARE_ACCOUNT_ID = _env("CLOUDFLARE_ACCOUNT_ID")
    CLOUDFLARE_API_TOKEN = _env("CLOUDFLARE_API_TOKEN")

    try:
        CLOUDFLARE_KEYS = json.loads(os.environ.get("CLOUDFLARE_KEYS", "[]"))
    except json.JSONDecodeError:
        CLOUDFLARE_KEYS = []

    OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    OLLAMA_API_KEY = _env("OLLAMA_API_KEY", "ollama")

    ENABLE_WEB_NOTIFICATIONS = _env("ENABLE_WEB_NOTIFICATIONS", "true").lower() == "true"
    VAPID_PUBLIC_KEY = _env("VAPID_PUBLIC_KEY", "BCpydN873EY24vGAR1huUpKxXSjoBsatt0k0jPtvS7q3lVqaeC9DytkaGvURofJw0ubR0M39uhk8U3_NfrHaYyc")
    VAPID_PRIVATE_KEY = _env("VAPID_PRIVATE_KEY", "X0P_PuBAcMnS3IgI7RiYDou0R0w8t8WWmnh3rfg_gEo")
    VAPID_CLAIMS = {
        "sub": _env("VAPID_EMAIL", "mailto:raghavan@vellur.in")
    }

    FEEDER_HEALTH = {
        "ACTIVE_AFTER_MINUTES": 5,
        "INACTIVE_AFTER_MINUTES": 10,
        "CONTRIBUTOR_DOWNGRADE_HOURS": 6,
        "CONTRIBUTOR_RESTORE_HOURS": 2,
        "TIER_ACTIVE_HOURS_REQUIRED": 24,
    }

    TARGET_AIRPORTS = {}
    RUNWAY_DATA = {}
    IATA_TO_ICAO = {}
    ICAO_TO_IATA = {}

    @classmethod
    def load_from_db(cls):
        print("Loading Airport and Runway data from PostgreSQL into Config...")
        try:
            conn = psycopg2.connect(**cls.DB_PARAMS)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM airports;")
            airports = cur.fetchall()
            for apt in airports:
                icao = apt['icao']
                cls.TARGET_AIRPORTS[icao] = dict(apt)
                cls.IATA_TO_ICAO[apt['iata']] = icao
                cls.ICAO_TO_IATA[icao] = apt['iata']
                cls.RUNWAY_DATA[icao] = {
                    "mag_var_w": float(apt.get('mag_var_w') or 0.0),
                    "runways": []
                }
            cur.execute("SELECT * FROM runways;")
            runways = cur.fetchall()
            for rw in runways:
                icao = rw['airport_icao']
                if icao in cls.RUNWAY_DATA:
                    cls.RUNWAY_DATA[icao]['runways'].append({
                        "id": rw["runway_id"],
                        "name1": rw["name1"],
                        "hdg1": float(rw["hdg1"]) if rw["hdg1"] else None,
                        "lat1": rw["lat1"],
                        "lon1": rw["lon1"],
                        "name2": rw["name2"],
                        "hdg2": float(rw["hdg2"]) if rw["hdg2"] else None,
                        "lat2": rw["lat2"],
                        "lon2": rw["lon2"],
                        "width_m": rw["width_m"]
                    })
            cur.close()
            conn.close()
            print(f"✅ Successfully loaded {len(cls.TARGET_AIRPORTS)} airports into RAM.")
        except Exception as e:
            print(f"❌ CRITICAL ERROR: Could not load static data from DB: {e}")

try:
    Config.load_from_db()
except Exception as e:
    print(f"⚠️ Warning: Could not load static DB data on startup.")

POSTGRES_DB = os.getenv("POSTGRES_DB", "flight_db")
POSTGRES_USER = os.getenv("POSTGRES_USER", "flight_db_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres_password")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")

DB_PARAMS = {
    "database": POSTGRES_DB,
    "user": POSTGRES_USER,
    "password": POSTGRES_PASSWORD,
    "host": POSTGRES_HOST,
    "port": POSTGRES_PORT,
    "client_encoding": "UTF8"
}

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))