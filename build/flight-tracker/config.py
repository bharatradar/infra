
# config.py
# v5 - Interactive Gatekeeper & Web Push Notifications
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def _env(key, default=None):
    """Get environment variable with optional default."""
    return os.environ.get(key, default)

class Config:
    DEBUG_MODE = True
    APP_NAME = "raga_flight_status"
    
    # 🎛️ MASTER AI TOGGLE (For ZeroClaw Agent)
    USE_LOCAL_LLM = False 
    
    # 🚀 NEW: TELEGRAM BOT ENGINE MODE
    # Options: 'zeroclaw' (Legacy Agent Loop) OR 'fast_router' (High-speed regex + JSON router)
    BOT_ENGINE_MODE = "fast_router"
    # 🌟 NEW: Model Provider Selection
    # Options: 'local_gguf', 'groq', 'cloudflare' , 'llama-server' 
    FAST_ROUTER_PROVIDER = "groq"

    # 🌟 NEW: Fallback Priority Order
    # The system will attempt FAST_ROUTER_PROVIDER first. If it fails, it will 
    # cascade through this list in the exact order specified below.
    FAST_ROUTER_FALLBACK_QUEUE = ["groq", "cloudflare", "llama-server", "local_gguf"]
    
    # 🌟 NEW: Llama-Server Endpoint
    LLAMA_SERVER_URL = _env("LLAMA_SERVER_URL", "http://192.168.100.44:8080/v1/chat/completions")


# 🌟 NEW: Direct GGUF Model Configuration
    #LOCAL_GGUF_PATH = "/home/raghavan/qwen2.5-1.5b-instruct.Q4_K_M.gguf" # Update path for your Pi!
    LOCAL_GGUF_PATH = _env("LOCAL_GGUF_PATH", "/home/ragaradar/raga-radar-v2-q8_0-local.gguf")
    LOCAL_GGUF_CTX = 1024

    FAST_ROUTER_MODEL = "llama-3.1-8b-instant" # Fast LLM for JSON routing
    
    # 🧠 NEW: SEMANTIC ROUTING MODE 
    # 'llm_classifier' -> STRICT NLP: Uses LLM with Nullable Schema (Foolproof for English grammar).
    # 'vector_math' -> PURE MATH: Fast, but brittle with complex sentence structures.
    SEMANTIC_ROUTING_MODE = "llm_classifier" 

    #BOT_ENGINE_MODE = "fast_router"
    #FAST_ROUTER_PROVIDER = "cloudflare" 
    #FAST_ROUTER_MODEL = "@cf/meta/llama-3.1-8b-instruct" # Or "@cf/zai-org/glm-4.7-flash"    


    # 🧠 NEW: SEMANTIC ROUTING STRATEGY
    # If True: Tries lightning-fast Regex patterns first. If it misses, falls back to LLM.
    # If False: Bypasses Regex completely and sends every query straight to the LLM.
    USE_REGEX_ROUTING_FIRST = False


    # 🛑 BOOT BEHAVIOR
    FR24_BLOCKING_STARTUP = False
    
    # ⏱️ SYSTEM & ALERT CONFIG
    ETA_ALERT_CHECK_INTERVAL_SEC = 60
    RADAR_QUEUE_MAXSIZE = 2
    CONCURRENT_PROCESSING_LIMIT = 15

    # 📍 FLIGHT INFERENCE CONFIG
    TAKEOFF_INFERENCE_MAX_DIST_KM = 150  # Max distance for inferring takeoff origin
    CLEAR_AIRBORNE_MIN_ALT_FT = 3000  # Min altitude to consider flight clearly airborne    
    
    # --- API Endpoints ---
    ADSB_EXCHANGE_BINCRAFT_URL = _env("ADSB_EXCHANGE_BINCRAFT_URL", "https://globe.adsbexchange.com/re-api/?binCraft&zstd&box=9.337602,33.583193,60.875230,104.092506")
    LOCAL_AIRCRAFT_DATA_URL = _env("LOCAL_AIRCRAFT_DATA_URL", "http://192.168.1.3/tar1090/data/aircraft.json")
    ADSB_LOL_AIRCRAFT_DATA_URL = _env("ADSB_LOL_AIRCRAFT_DATA_URL", "https://api.adsb.lol/v2/point/21.1458/79.0882/150")
    RE_ADSB_LOL_AIRCRAFT_DATA_URL = _env("RE_ADSB_LOL_AIRCRAFT_DATA_URL", "https://re-api.adsb.lol/?circle=21.1458,79.0882,1000")
    ADSB_ONE_AIRCRAFT_DATA_URL = _env("ADSB_ONE_AIRCRAFT_DATA_URL", "https://api.adsb.one/v2/point/21.1458,79.0882/1000")
    
    # 🌟 K3s Internal Data Sources (NEW)
    K3S_PLANES_DATA_URL = _env("K3S_PLANES_DATA_URL", "http://planes-readsb.bharatradar.svc.cluster.local/data/aircraft.json")
    K3S_REAPI_DATA_URL = _env("K3S_REAPI_DATA_URL", "http://reapi-readsb.bharatradar.svc.cluster.local:30152")
    
    # Data Source Priority Toggle
    USE_K3S_DATA_PRIMARY = _env("USE_K3S_DATA_PRIMARY", "false").lower() == "true"

    PROXY_URL = _env("PROXY_URL", "http://192.168.100.19:8888")
    
    API_ADSB_DB_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft/"
    API_ADSB_DB_CALLSIGN = "https://api.adsbdb.com/v0/callsign/"
    
    FLIGHTRADAR24_SEARCH = "https://www.flightradar24.com/v1/search/web/find"
    FLIGHTRADAR24_FLIGHTS = "https://www.flightradar24.com/data/flights/"
    
    # FlightAware / piaware for route extraction
    FLIGHTAWARE_ROUTE_ENABLED = True
    FLIGHTAWARE_FLIGHT_URL = "https://www.flightaware.com/live/flight/"
    
    # Route resolution priority order (first to last)
    ROUTE_RESOLUTION_ORDER = ["flightaware", "adsbdb"]
    
    # 🗂️ STATIC DATA URLS
    ROUTES_CSV_URL = "https://vrs-standing-data.adsb.lol/routes.csv"
    AIRPORTS_CSV_URL = "https://vrs-standing-data.adsb.lol/airports.csv"
    
    # --- Physical Constraints & Zones ---
    APPROACH_RADIUS_KM = 185
    TERMINAL_AREA_RADIUS_KM = 50   # 🌟 NEW: The "Arriving Shortly" terminal area boundary (50km)
    LANDING_RADIUS_KM = 10
    TAKEOFF_WAKEUP_RADIUS_KM = 20  
    LANDING_ALT_THRESH_FT = 2000
    MAX_TRACKING_ALTITUDE = 40000
    APPROACH_ALT_THRESH_FT = 15000    
    FINAL_APPROACH_RADIUS_KM = 100
    FINAL_APPROACH_ALT_FT = 3500      
    
    ASSUMED_LANDING_TIMEOUT_SEC = 180 
    
    # 🌟 Python Executable Path
    PYTHON_PATH = "/home/ragaradar/miniforge3/envs/bharat-radar_env/bin/python" 
    

    GET_SCHEDULES_FOR = ["TODAY", "TOMORROW"]  # Options: "TODAY", "TOMORROW", "NEXT_7_DAYS"
    GET_SCHEDULES_FROM_AVIONIO = False
    MISSING_AIRPORTS_IN_AVIONIO = {'VEAY': 'AYJ', 'VEDO': 'DGH', 'VIJW': 'DXN', 'VIDX': 'HDO', 'VEHO': 'HGI', 'VIAH': 'HRH', 'VAHS': 'HSR', 'VOKU': 'KJB', 'VANM': 'NMI', 'VARW': 'REW', 'VOSR': 'SDW'}
    # --- File Paths ---
    AIRLINES_FILE = _env("AIRLINES_FILE", "/app/data/airlines.csv")
    AIRPORTS_CSV_FILE = _env("AIRPORTS_CSV_FILE", "/app/data/airports.csv")
    
    # --- Database & Redis ---
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
        "db": int(_env("REDIS_DB", "0")),
        "password": _env("REDIS_PASSWORD", None),
        "decode_responses": True
    }
    
    # --- Redis Keys ---
    REDIS_LIVE_FLIGHTS_KEY = "live_flights"
    REDIS_LIVE_FLIGHTS_META_KEY = "live_flights_meta"
    REDIS_FLIGHTS_TTL = 30  # seconds

    # --- WebSocket Configuration ---
    WS_ENABLED = _env("WS_ENABLED", "true").lower() == "true"
    WS_HOST = _env("WS_HOST", "localhost")
    WS_PORT = int(_env("WS_PORT", "8002"))
    WS_URL = f"ws://{WS_HOST}:{WS_PORT}" if WS_ENABLED else None
    
    # 🌟 NEW: InfluxDB Time-Series Telemetry
    INFLUXDB_URL = _env("INFLUXDB_URL", "http://localhost:8086")
    INFLUXDB_TOKEN = _env("INFLUXDB_TOKEN")
    INFLUXDB_ORG = _env("INFLUXDB_ORG", "Vellur") 
    INFLUXDB_BUCKET = _env("INFLUXDB_BUCKET", "raga_flight_radar_db")
    INFLUXDB_WRITE_INTERVAL_SEC = int(_env("INFLUXDB_WRITE_INTERVAL_SEC", "5"))

    # ==========================================
    # Polling Intervals (All configurable via env)
    # ==========================================
    RADAR_FETCH_INTERVAL_SEC = int(_env("RADAR_FETCH_INTERVAL_SEC", "10"))
    WEBSOCKET_BROADCAST_INTERVAL_SEC = int(_env("WEBSOCKET_BROADCAST_INTERVAL_SEC", "1"))
    GAP_FILLER_INTERVAL_SEC = int(_env("GAP_FILLER_INTERVAL_SEC", "10"))
    ENRICHMENT_FETCH_DELAY_SEC = int(_env("ENRICHMENT_FETCH_DELAY_SEC", "2"))
    JANITOR_INTERVAL_SEC = int(_env("JANITOR_INTERVAL_SEC", "900"))
    FORENSIC_JANITOR_INTERVAL_SEC = int(_env("FORENSIC_JANITOR_INTERVAL_SEC", "900"))
    WATCHDOG_CHECK_INTERVAL_SEC = int(_env("WATCHDOG_CHECK_INTERVAL_SEC", "120"))
    DAILY_ANALYST_INTERVAL_SEC = int(_env("DAILY_ANALYST_INTERVAL_SEC", "900"))
    FEEDER_HEALTH_CHECK_INTERVAL_SEC = int(_env("FEEDER_HEALTH_CHECK_INTERVAL_SEC", "30"))
    STATS_PARSER_INTERVAL_SEC = int(_env("STATS_PARSER_INTERVAL_SEC", "120"))
    BACKGROUND_TASK_STARTUP_DELAY_SEC = int(_env("BACKGROUND_TASK_STARTUP_DELAY_SEC", "5"))
    STATS_PARSER_STARTUP_DELAY_SEC = int(_env("STATS_PARSER_STARTUP_DELAY_SEC", "10"))

    # ==========================================
    # Frontend Polling Intervals (milliseconds)
    # ==========================================
    # Radar display refresh interval
    FRONTEND_RADAR_POLL_INTERVAL_MS = 5000

    # ATC display refresh interval
    FRONTEND_ATC_POLL_INTERVAL_MS = 5000

    # Ops display refresh interval
    FRONTEND_OPS_POLL_INTERVAL_MS = 30000

    # Executive dashboard refresh interval
    FRONTEND_EXEC_POLL_INTERVAL_MS = 60000

    # WebSocket reconnect base delay (milliseconds)
    FRONTEND_WS_RECONNECT_DELAY_MS = 3000

    TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID") 
    
    # --- AI API Keys & Local Endpoints ---
    GROQ_API_KEY = _env("GROQ_API_KEY")

    _openrouter_keys = _env("OPENROUTER_API_KEYS", "")
    OPENROUTER_API_KEYS = [k.strip() for k in _openrouter_keys.split(",") if k.strip()] if _openrouter_keys else []

    GOOGLE_STUDIO_API_KEY = _env("GOOGLE_STUDIO_API_KEY")

    #Cloudflare AI token
    CLOUDFLARE_ACCOUNT_ID = _env("CLOUDFLARE_ACCOUNT_ID")
    CLOUDFLARE_API_TOKEN = _env("CLOUDFLARE_API_TOKEN")

    # Cloudflare keys - list for request-scoped rotation
    # Format: [{"id": "...", "token": "..."}, ...]
    try:
        CLOUDFLARE_KEYS = json.loads(os.environ.get("CLOUDFLARE_KEYS", "[]"))
    except json.JSONDecodeError:
        CLOUDFLARE_KEYS = []

    OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    OLLAMA_API_KEY = _env("OLLAMA_API_KEY", "ollama")

 
    # ==========================================
    # 🌟 NEW: WEB PUSH NOTIFICATIONS (VAPID)
    # ==========================================
    ENABLE_WEB_NOTIFICATIONS = _env("ENABLE_WEB_NOTIFICATIONS", "true").lower() == "true"

    # ⚠️ To generate your real keys, run this in your terminal:
    # 1. pip install pywebpush cryptography
    # 2. vapid --gen

    VAPID_PUBLIC_KEY = _env("VAPID_PUBLIC_KEY")
    VAPID_PRIVATE_KEY = _env("VAPID_PRIVATE_KEY")
    VAPID_CLAIMS = {
        "sub": _env("VAPID_EMAIL", "mailto:admin@example.com") # Push servers require an admin email
    }


    # 🛰️ FEEDER HEALTH MONITOR THRESHOLDS
    FEEDER_HEALTH = {
        "ACTIVE_AFTER_MINUTES": 5,           # Data seen within last 5 min → ACTIVE
        "INACTIVE_AFTER_MINUTES": 10,        # No data for 10 min → INACTIVE
        "CONTRIBUTOR_DOWNGRADE_HOURS": 6,    # No active feeders for 6h → STANDARD
        "CONTRIBUTOR_RESTORE_HOURS": 2,      # Active for 2h straight → CONTRIBUTOR
        "TIER_ACTIVE_HOURS_REQUIRED": 24,    # Need 24h ACTIVE to count toward tier promo
    }

    # 🗂️ DYNAMIC STATIC DATA (Populated from DB at runtime)
    TARGET_AIRPORTS = {}
    RUNWAY_DATA = {}
    IATA_TO_ICAO = {}
    ICAO_TO_IATA = {}

    @classmethod
    def load_from_db(cls):
        """Fetches Airports and Runways from Postgres and rebuilds the dictionaries in RAM."""
        print("Loading Airport and Runway data from PostgreSQL into Config...")
        try:
            conn = psycopg2.connect(**cls.DB_PARAMS)
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # 1. Load Airports
            cur.execute("SELECT * FROM airports;")
            airports = cur.fetchall()

            for apt in airports:
                icao = apt['icao']
                # Rebuild TARGET_AIRPORTS
                cls.TARGET_AIRPORTS[icao] = dict(apt)
                
                # Rebuild IATA/ICAO Lookups
                cls.IATA_TO_ICAO[apt['iata']] = icao
                cls.ICAO_TO_IATA[icao] = apt['iata']
                
                # Initialize RUNWAY_DATA for this airport
                cls.RUNWAY_DATA[icao] = {
                    "mag_var_w": float(apt.get('mag_var_w') or 0.0),
                    "runways": []
                }

            # 2. Load Runways
            cur.execute("SELECT * FROM runways;")
            runways = cur.fetchall()

            for rw in runways:
                icao = rw['airport_icao']
                if icao in cls.RUNWAY_DATA:
                    # Rebuild the paired runway dictionary structure expected by your app
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

# ==========================================
# 🚀 AUTO-LOAD ON STARTUP
# ==========================================
# Because Python caches imports, this will only run EXACTLY ONCE 
# when the system starts and the Config class is first imported.
try:
    Config.load_from_db()
except Exception as e:
    # We silently catch the error here so that db_reset.py doesn't crash 
    # when trying to import Config on a completely empty database.
    print(f"⚠️ Warning: Could not load static DB data on startup (Ignore if running db_reset.py).")


# ==========================================
# Database and Cache Settings
# ==========================================
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
