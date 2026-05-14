# web_app.py
# v5 - Enterprise AI Data Lineage + Web Chat + Web Push Support (Refactored)
import asyncio
import secrets
import hashlib
import aiohttp
from fastapi import FastAPI, Request, Query, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
import asyncpg
from contextlib import asynccontextmanager
from config import Config
from typing import Optional
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
import json
import logging
import sys
import urllib.request
import urllib.error
from math import radians, sin, cos, sqrt, atan2
import core_ai_pipeline
import bot_router_mcp_client
import web_app_db
from datetime import datetime
import gzip
import csv
import os
import tempfile

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# 🔐 AUTHENTICATION CONFIGURATION
# ---------------------------------------------------------------------
AUTH_COOKIE_NAME = "bharat_radar_session"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

# Routes that don't require authentication
PUBLIC_ROUTES = [
    "/login",
    "/logout",
    "/auth/",
    "/auth/",
    "/login",
    "/logout",
    "/static/",
    "/favicon.ico",
    # Root-path paths (FastAPI handles root_path internally)
    "/dashboard",
    "/login",
    "/auth/google",
    "/auth/callback",
    # Doubled paths (nginx proxy adds /command_center prefix)
    "/login",
    "/auth/google",
    "/auth/callback",
    # Dashboard data endpoints (no auth for public dashboard display)
    "/api/aircraft/",
    "/api/atc/",
    "/api/ops/",
    "/api/exec/",
    "/api/delay/",
    "/api/drilldown/",
    "/api/telemetry/",
    "/api/config",
    "/api/filters",
    "/static/",
    "/favicon.ico",
    "/api/feeders/",
    # API docs (public access)
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/openapi.json",
]

def get_session(request: Request):
    """Get session from cookie"""
    return request.cookies.get(AUTH_COOKIE_NAME)

def is_session_valid(request: Request) -> bool:
    """Check if session cookie is valid (exists and not expired)"""
    cookie_value = request.cookies.get(AUTH_COOKIE_NAME)
    if not cookie_value:
        return False
    try:
        parts = cookie_value.rsplit('_', 1)
        if len(parts) != 2:
            return False
        timestamp = float(parts[1])
        age = datetime.now().timestamp() - timestamp
        return age < AUTH_COOKIE_MAX_AGE
    except (ValueError, IndexError):
        return False

def require_auth(request: Request):
    """Check auth - return JSONResponse if not authenticated"""
    if not is_session_valid(request):
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return None

# ---------------------------------------------------------------------
# 🌟 NEW: IMPORTS FOR DIRECT GGUF TESTING
# ---------------------------------------------------------------------
try:
    from resolver import resolve_airport, resolve_callsign
except ImportError:
    logger.warning("resolver.py not found. Resolve functions will be bypassed.")
    def resolve_airport(val): return val
    def resolve_callsign(val): return val

try:
    from llama_cpp import Llama
except ImportError:
    Llama = None

db_pool = None
influx_client = None
influx_query_api = None

# ---------------------------------------------------------------------
# 🛩️ TAR1090 AIRCRAFT DATABASE (hex -> type mapping)
# ---------------------------------------------------------------------
AIRCRAFT_DB = {}  # { "hex": { "reg": "N123AB", "type": "B738", "flags": "00", "desc": "BOEING 737-800" } }
AIRCRAFT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "aircraft.csv.gz")

def _enrich_flights(flights):
    """Add ac_type, reg, desc fields from AIRCRAFT_DB to each flight dict in-place."""
    if not AIRCRAFT_DB or not flights:
        return flights
    for ac in flights:
        hex_code = (ac.get('hexid') or ac.get('hex') or '').strip().upper()
        if hex_code and hex_code in AIRCRAFT_DB:
            entry = AIRCRAFT_DB[hex_code]
            if entry.get('type'):
                ac['ac_type'] = entry['type']
            if entry.get('reg'):
                ac['reg'] = entry['reg']
            if entry.get('desc'):
                ac['desc'] = entry['desc']
    return flights

async def load_aircraft_db():
    """Download and parse tar1090 aircraft.csv.gz into memory."""
    global AIRCRAFT_DB
    try:
        # Ensure data dir exists
        os.makedirs(os.path.dirname(AIRCRAFT_DB_PATH), exist_ok=True)
        
        # Download if not exists or older than 24h
        needs_download = True
        if os.path.exists(AIRCRAFT_DB_PATH):
            age_hours = (datetime.now().timestamp() - os.path.getmtime(AIRCRAFT_DB_PATH)) / 3600
            needs_download = age_hours > 24
        
        if needs_download:
            url = "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
            logger.info(f"⬇️ Downloading aircraft database from {url}...")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    with open(AIRCRAFT_DB_PATH, "wb") as f:
                        f.write(resp.read())
                logger.info("✅ Aircraft database downloaded successfully")
            except Exception as e:
                logger.warning(f"⚠️ Failed to download aircraft DB: {e}")
                if not os.path.exists(AIRCRAFT_DB_PATH):
                    logger.warning("No cached aircraft DB available")
                    return
        
        # Parse the CSV
        if os.path.exists(AIRCRAFT_DB_PATH):
            logger.info("📖 Parsing aircraft database...")
            parsed = {}
            with gzip.open(AIRCRAFT_DB_PATH, "rt", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f, delimiter=";")
                for row in reader:
                    if len(row) >= 3:
                        hex_code = row[0].strip().upper()
                        reg = row[1].strip() if len(row) > 1 else ""
                        ac_type = row[2].strip().upper() if len(row) > 2 else ""
                        flags = row[3].strip() if len(row) > 3 else ""
                        desc = row[4].strip() if len(row) > 4 else ""
                        parsed[hex_code] = {
                            "reg": reg,
                            "type": ac_type,
                            "flags": flags,
                            "desc": desc
                        }
            AIRCRAFT_DB = parsed
            logger.info(f"✅ Aircraft database loaded: {len(AIRCRAFT_DB)} entries")
    except Exception as e:
        logger.error(f"❌ Failed to load aircraft DB: {e}")

# ---------------------------------------------------------------------
# 🌟 DIRECT GGUF MODEL INITIALIZATION
# ---------------------------------------------------------------------
logger.info("Initializing Raga Aviation GGUF Model for Direct API Testing...")
test_llm = None
if Llama:
    try:
        test_llm = Llama(
            model_path=Config.LOCAL_GGUF_PATH, 
            n_ctx=1024,           
            n_batch=512,
            n_gpu_layers=0,       
            n_threads=3,
            chat_format="gemma",  
            verbose=False
        )
        logger.info("✅ Direct GGUF Model loaded successfully.")
    except Exception as e:
        logger.error(f"⚠️ Failed to load direct GGUF model: {str(e)}")

TEST_SYSTEM_PROMPT = (
    "You are a flight routing AI. You must ONLY output JSON. "
    "### CRITICAL INSTRUCTION ###\n"
    "1. Do NOT convert, guess, or invent airport codes.\n"
    "2. If the user mentions a city name (e.g., 'Jaipur', 'Goa', 'Pune'), you MUST "
    "put the exact word they typed into the JSON. \n"
    "3. Example: If user says 'Jaipur', output '{\"airport_code\": \"Jaipur\"}'. "
    "DO NOT output 'JAI', 'JAHI', or 'JAU'.\n"
    "### END CRITICAL INSTRUCTION ###\n\n"
    "Allowed functions: set_flight_alert, get_unified_airport_timetable, get_flight_status, "
    "get_inbound_flights, get_route_status_board, get_airport_turnarounds, "
    "get_average_turnaround_by_airline, get_airport_anomalies, get_airframe_history, "
    "get_inbound_aircraft_status, predict_flight_assignment, get_airport_traffic, "
    "get_airspace_pulse, get_system_health, unsupported."
)

async def _feeder_health_monitor_task():
    """Periodically check receivers.json, update feeder health status and user contributor tiers."""
    await asyncio.sleep(getattr(Config, 'BACKGROUND_TASK_STARTUP_DELAY_SEC', 5))
    _last_hour_tick = 0
    while True:
        try:
            await asyncio.sleep(getattr(Config, 'FEEDER_HEALTH_CHECK_INTERVAL_SEC', 30))
            if db_pool is None:
                continue

            ht = Config.FEEDER_HEALTH
            now = asyncio.get_event_loop().time()

            # Fetch receivers.json via HTTP from hub-readsb service
            timeout = aiohttp.ClientTimeout(total=getattr(Config, 'FEEDER_HEALTH_CHECK_TIMEOUT_SEC', 10))
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(Config.READSB_RECEIVERS_URL) as resp:
                        if resp.status != 200:
                            logger.warning(f"receivers.json HTTP error: {resp.status}")
                            continue
                        data = await resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch receivers.json: {e}")
                continue

            receivers = data.get('receivers', [])
            receiver_ids = set()
            for r in receivers:
                if isinstance(r, (list, tuple)) and len(r) > 0:
                    receiver_ids.add(str(r[0]))

            async with db_pool.acquire() as conn:
                # --- Phase A: Feeder-level health ---
                all_feeders = await conn.fetch(
                    "SELECT id, station_uuid, status, last_seen_at, total_active_hours FROM feeders WHERE station_uuid IS NOT NULL"
                )

                for feeder in all_feeders:
                    fid = feeder['id']
                    uuid = feeder['station_uuid']
                    # receivers.json stores first 3 segments of UUID (e.g., 67620ee8-58c4-407d)
                    truncated = '-'.join(uuid.split('-')[:3]) if uuid else ''
                    is_online = truncated in receiver_ids
                    status = feeder['status']
                    last_seen = feeder['last_seen_at']

                    if is_online:
                        if status == 'PENDING':
                            await conn.execute(
                                "UPDATE feeders SET status = 'ACTIVATED', last_seen_at = NOW(), updated_at = NOW() WHERE id = $1",
                                fid
                            )
                            logger.info(f"Feeder {fid} ACTIVATED (first data)")
                        elif status in ('ACTIVATED', 'INACTIVE'):
                            await conn.execute(
                                "UPDATE feeders SET status = 'ACTIVE', last_seen_at = NOW(), updated_at = NOW() WHERE id = $1",
                                fid
                            )
                            logger.info(f"Feeder {fid} ACTIVE (data resumed)")
                        elif status == 'ACTIVE':
                            # Still active, just update last_seen_at
                            await conn.execute(
                                "UPDATE feeders SET last_seen_at = NOW() WHERE id = $1",
                                fid
                            )
                    else:
                        # Not online
                        if status == 'ACTIVE' and last_seen:
                            minutes_since = (conn.fetchval(
                                "SELECT EXTRACT(EPOCH FROM (NOW() - $1)) / 60", last_seen
                            ) or 0)
                            if minutes_since > ht['INACTIVE_AFTER_MINUTES']:
                                await conn.execute(
                                    "UPDATE feeders SET status = 'INACTIVE', updated_at = NOW() WHERE id = $1",
                                    fid
                                )
                                logger.info(f"Feeder {fid} INACTIVE ({minutes_since:.0f}m since last data)")

                # --- Phase B: Increment total_active_hours (once per hour) ---
                hour_now = int(now // 3600)
                if hour_now > _last_hour_tick:
                    _last_hour_tick = hour_now
                    await conn.execute(
                        "UPDATE feeders SET total_active_hours = total_active_hours + 1 WHERE status = 'ACTIVE'"
                    )

                # --- Phase C: User contributor status ---
                users = await conn.fetch(
                    """SELECT u.email, u.contributor_status, u.contributor_changed_at,
                              COUNT(f.id) FILTER (WHERE f.status = 'ACTIVE') as active_count,
                              MIN(f.last_seen_at) FILTER (WHERE f.status = 'ACTIVE') as oldest_active
                       FROM api_users u
                       LEFT JOIN feeders f ON LOWER(f.user_email) = LOWER(u.email)
                       GROUP BY u.email, u.contributor_status, u.contributor_changed_at"""
                )

                for user in users:
                    email = user['email']
                    curr = user['contributor_status'] or 'STANDARD'
                    active_count = user['active_count'] or 0
                    changed_at = user['contributor_changed_at']

                    if active_count > 0:
                        # Has active feeders
                        if curr == 'STANDARD':
                            # Check if continuously active for restore threshold
                            if changed_at:
                                hours_since_change = await conn.fetchval(
                                    "SELECT EXTRACT(EPOCH FROM (NOW() - $1)) / 3600", changed_at
                                ) or 0
                            else:
                                hours_since_change = 999  # First time, promote immediately if active
                            if hours_since_change >= ht['CONTRIBUTOR_RESTORE_HOURS']:
                                await conn.execute(
                                    """UPDATE api_users
                                       SET contributor_status = 'CONTRIBUTOR',
                                           contributor_since = COALESCE(contributor_since, NOW()),
                                           contributor_changed_at = NOW()
                                       WHERE LOWER(email) = LOWER($1)""",
                                    email
                                )
                                logger.info(f"User {email} promoted to CONTRIBUTOR")
                    else:
                        # No active feeders
                        if curr == 'CONTRIBUTOR' and changed_at:
                            hours_down = await conn.fetchval(
                                "SELECT EXTRACT(EPOCH FROM (NOW() - $1)) / 3600", changed_at
                            ) or 0
                            if hours_down >= ht['CONTRIBUTOR_DOWNGRADE_HOURS']:
                                await conn.execute(
                                    """UPDATE api_users
                                       SET contributor_status = 'STANDARD',
                                           contributor_changed_at = NOW()
                                       WHERE LOWER(email) = LOWER($1)""",
                                    email
                                )
                                logger.info(f"User {email} demoted to STANDARD")

        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Health monitor error: {e}")

async def _feeder_stats_parser_task():
    """Periodically parse tar1090 data and write daily stats to feeder_daily_stats."""
    await asyncio.sleep(getattr(Config, 'STATS_PARSER_STARTUP_DELAY_SEC', 10))
    while True:
        try:
            await asyncio.sleep(getattr(Config, 'STATS_PARSER_INTERVAL_SEC', 120))
            if db_pool is None:
                continue

            async with db_pool.acquire() as conn:
                feeders = await conn.fetch(
                    "SELECT id, station_uuid, lat, lon FROM feeders WHERE station_uuid IS NOT NULL"
                )
                today = await conn.fetchval("SELECT CURRENT_DATE")

                for feeder in feeders:
                    fid = feeder['id']
                    uuid = feeder['station_uuid']
                    station_lat = feeder['lat']
                    station_lon = feeder['lon']
                    feeder_aircraft = []

                    try:
                        url = f"http://planes-readsb:80/data/aircraft.json?all"
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req, timeout=5) as response:
                            data = json.loads(response.read().decode('utf-8'))
                            all_aircraft = data.get('aircraft', [])
                            feeder_aircraft = [ac for ac in all_aircraft if uuid.startswith(ac.get('rId', ''))]
                    except Exception as e:
                        logger.warning(f"Stats parser: readsb API error for feeder {fid}: {e}")
                        continue

                    if not feeder_aircraft:
                        continue

                    unique_hexes = set(ac.get('hex', '') for ac in feeder_aircraft)
                    total_messages = sum(ac.get('messages', 0) for ac in feeder_aircraft)
                    positions_count = sum(1 for ac in feeder_aircraft if ac.get('lat') and ac.get('lon'))

                    max_range_km = 0
                    if station_lat and station_lon:
                        for ac in feeder_aircraft:
                            ac_lat = ac.get('lat')
                            ac_lon = ac.get('lon')
                            if ac_lat and ac_lon:
                                from math import radians, sin, cos, sqrt, atan2
                                R = 6371
                                lat1, lon1 = radians(station_lat), radians(station_lon)
                                lat2, lon2 = radians(ac_lat), radians(ac_lon)
                                dlat = lat2 - lat1
                                dlon = lon2 - lon1
                                a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                                c = 2 * atan2(sqrt(a), sqrt(1-a))
                                distance = R * c
                                max_range_km = max(max_range_km, distance)

                    # Upsert daily stats
                    await conn.execute("""
                        INSERT INTO feeder_daily_stats
                            (feeder_id, stat_date, messages_count, aircraft_count, positions_count, max_range_km)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (feeder_id, stat_date)
                        DO UPDATE SET
                            messages_count = feeder_daily_stats.messages_count + EXCLUDED.messages_count,
                            aircraft_count = GREATEST(feeder_daily_stats.aircraft_count, EXCLUDED.aircraft_count),
                            positions_count = feeder_daily_stats.positions_count + EXCLUDED.positions_count,
                            max_range_km = GREATEST(feeder_daily_stats.max_range_km, EXCLUDED.max_range_km)
                    """, fid, today, total_messages, len(unique_hexes), positions_count, int(max_range_km))

                    logger.info(f"Stats parser: feeder {fid} — {len(unique_hexes)} aircraft, {total_messages} msgs, {int(max_range_km)} km max")

        except Exception as e:
            logger.warning(f"Stats parser error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, influx_client, influx_query_api
    logger.info("🚀 Starting Enterprise Web API and connecting to Databases...")
    db_pool = await asyncpg.create_pool(**Config.DB_PARAMS)
    
    # Delegate memory caching & migration to the new web_app_db file
    await web_app_db.init_web_app_db(db_pool)
    
    await bot_router_mcp_client.init_client_state()
    bot_router_mcp_client.load_airlines_bot()
    
    if getattr(Config, 'INFLUXDB_TOKEN', None):
        influx_client = InfluxDBClientAsync(
            url=Config.INFLUXDB_URL,
            token=Config.INFLUXDB_TOKEN,
            org=Config.INFLUXDB_ORG
        )
        influx_query_api = influx_client.query_api()
        logger.info("✅ Connected to InfluxDB Time-Series Engine")
    
    # Load tar1090 aircraft database
    await load_aircraft_db()

    health_task = asyncio.create_task(_feeder_health_monitor_task())
    stats_task = asyncio.create_task(_feeder_stats_parser_task())

    yield
    logger.info("🛑 Shutting down Web API...")
    health_task.cancel()
    stats_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass
    try:
        await stats_task
    except asyncio.CancelledError:
        pass
    await db_pool.close()
    await bot_router_mcp_client.close_client_state()
    if influx_client:
        await influx_client.close()

app = FastAPI(
    title="Raga Radar Enterprise API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bharatradar.com", "https://www.bharatradar.com", "https://cortex.bharatradar.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Manually serve docs at /* so they work behind nginx/FRP proxy
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=app.title + " - Swagger UI",
        oauth2_redirect_url="/docs/oauth2-redirect",
    )

@app.get("/docs/oauth2-redirect", include_in_schema=False)
async def swagger_ui_redirect():
    # FastAPI's built-in oauth2 redirect handler
    from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html
    return get_swagger_ui_oauth2_redirect_html()

@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_endpoint():
    return JSONResponse(
        get_openapi(title=app.title, version=app.version, routes=app.routes)
    )

@app.get("/redoc", include_in_schema=False)
async def redoc_html():
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=app.title + " - ReDoc",
    )

# ---------------------------------------------------------------------
# 🔐 AUTHENTICATION MIDDLEWARE
# ---------------------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check authentication for all routes"""
    path = request.url.path
    
    logger.info(f"[AUTH] Path: {path}, Cookies: {dict(request.cookies)}")
    
    # Allow public routes
    for public_route in PUBLIC_ROUTES:
        if path.startswith(public_route):
            logger.info(f"[AUTH] Allowing public route: {public_route}")
            return await call_next(request)
    
    # Check authentication
    if not is_session_valid(request):
        logger.info(f"[AUTH] No valid session, redirecting to login")
        # API routes should return 401 so fetch() can handle it properly
        if "/api/" in path or path.startswith("/api"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"}
            )
        elif path.startswith("/"):
            return RedirectResponse(url="/login")
        else:
            return RedirectResponse(url="/")
    
    logger.info(f"[AUTH] Authenticated, allowing request")
    return await call_next(request)

# Mount the static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/sw", StaticFiles(directory="static"), name="sw")

@app.get("/")
@app.get("/")
async def serve_dashboard_root(request: Request):
    return FileResponse("static/dashboard.html")

@app.get("/login")
@app.get("/login/")
async def serve_login_page(request: Request):
    # If already authenticated, redirect to dashboard
    if is_session_valid(request):
        return RedirectResponse(url="/dashboard")
    return FileResponse("static/login.html")

@app.get("/logout")
@app.get("/logout/")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response

@app.get("/dashboard")
@app.get("/dashboard/")
async def serve_dashboard(request: Request):
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    return FileResponse("static/dashboard.html")

@app.get("/testmodel")
@app.get("/testmodel/")
async def serve_test_model(request: Request):
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    return FileResponse("static/model_test.html")

@app.get("/feeders")
@app.get("/feeders/")
async def redirect_feeders_to_profile(request: Request):
    """Redirect old feeders page to unified profile hub"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = RedirectResponse(url="/profile")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/profile")
@app.get("/profile/")
async def serve_profile_page(request: Request):
    """User profile page"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/profile.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api-access")
@app.get("/api-access/")
async def serve_api_access_page(request: Request):
    """API Access & Documentation page"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/api_access.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/feeders/map")
@app.get("/feeders/map/")
async def serve_feeder_map_page(request: Request):
    """Feeder map visualization page"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/feeder_map.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/profile")
@app.get("/profile/")
async def serve_profile_page(request: Request):
    """User profile page"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/profile.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api-access")
@app.get("/api-access/")
async def serve_api_access_page(request: Request):
    """API Access & Documentation page"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/api_access.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/feeders/map")
@app.get("/feeders/map/")
async def serve_feeder_map_page(request: Request):
    """Global feeder coverage map"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/feeder_map.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/feeders/account/{feeder_id}")
async def serve_feeder_account_page(request: Request, feeder_id: int):
    """Per-feeder account dashboard"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/feeder_account.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/feeders/health")
@app.get("/feeders/health/")
async def serve_feeder_health_page(request: Request):
    """Feeder health dashboard (single or network-wide)"""
    if not is_session_valid(request):
        return RedirectResponse(url="/login")
    response = FileResponse("static/feeder_health.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/auth/google")
async def auth_google():
    import os
    import urllib.parse
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def auth_callback(code: str):
    import os
    import aiohttp
    from datetime import datetime
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    
    try:
        async with aiohttp.ClientSession() as session:
            token_data = {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback")
            }
            
            async with session.post(GOOGLE_TOKEN_URL, data=token_data) as resp:
                if resp.status != 200:
                    return RedirectResponse("/login?auth=error", status_code=302)
                
                tokens = await resp.json()
                access_token = tokens.get("access_token")
                
                if not access_token:
                    return RedirectResponse("/login?auth=error", status_code=302)
                
                async with session.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}) as user_resp:
                    if user_resp.status == 200:
                        user_data = await user_resp.json()
                        email = user_data.get("email", "authenticated")
                        name = user_data.get("name", email.split('@')[0])
                    else:
                        email = "authenticated"
                        name = "User"
                
                # Create DB user BEFORE setting cookie
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO api_users (email, name, tier, google_id) 
                            VALUES ($1, $2, 'free', 'google') 
                            ON CONFLICT (email) DO NOTHING
                        """, email, name)
                        logger.info(f"[AUTH] User created/verified: {email}")
                except Exception as ue:
                    logger.error(f"[AUTH] Auto-create user error: {ue}")
                    return RedirectResponse("/login?auth=error", status_code=302)
                
                session_value = f"google_{email}_{datetime.now().timestamp()}"
                redirect = RedirectResponse(url="/dashboard", status_code=302)
                redirect.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=session_value,
                    max_age=AUTH_COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                    path="/",
                    domain="cortex.bharatradar.com"
                )
                
                return redirect
    except Exception as e:
        logger.error(f"Auth callback error: {e}")
        return RedirectResponse("/login?auth=error", status_code=302)

# ---------------------------------------------------------------------
# 🌟 DIRECT GGUF TESTING ENDPOINTS
# ---------------------------------------------------------------------
@app.get("/raga_radar_model_test", response_class=HTMLResponse)
async def get_model_test_html():
    try:
        with open("model_test.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(content="Frontend file missing.", status_code=404)

@app.post("/raga_radar_chat")
async def gguf_direct_chat(request: Request):
    if not test_llm:
        return {"status": "error", "message": "GGUF Model not loaded."}

    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    user_query = body.get("query", "")
    uid = body.get("session_id", "default_test_user")
    logger.info(f"Received Direct Query: {user_query} from session: {uid}")

    bot_router_mcp_client.CURRENT_SESSION_ID.set(uid)
    entities = await bot_router_mcp_client.ROUTER.extract_entities_async(user_query) 

    messages = [{"role": "user", "content": f"{TEST_SYSTEM_PROMPT}\n\n{user_query}"}]

    try:
        response_data = await asyncio.to_thread(
            test_llm.create_chat_completion,
            messages=messages,
            temperature=0.0,
            max_tokens=250,
            repeat_penalty=1.0    
        )

        raw_response = response_data["choices"][0]["message"]["content"].strip()
        json_start_index = raw_response.find('{')
        if json_start_index == -1:
            return {"status": "error", "message": "Constraint failed", "debug": raw_response}
            
        data = json.loads(raw_response[json_start_index:])
        func_name = data.get("function")
        params = data.get("parameters", {})

        tool_def = next((t for t in bot_router_mcp_client.FAST_ROUTER_TOOLS if t['function']['name'] == func_name), None)
        if tool_def:
            for req in tool_def['function']['parameters'].get('required', []):
                needs_injection = req not in params or not params[req]
                if not needs_injection and req in ["callsign", "callsign_raw", "departing_callsign", "identifier", "future_callsign"]:
                    if not entities.get("flights"):
                        needs_injection = True
                if not needs_injection and req in ["airport", "airport_code", "code", "origin", "destination"]:
                    if not entities.get("airports"):
                        needs_injection = True

                if needs_injection:
                    if req in ["callsign", "callsign_raw", "departing_callsign", "identifier", "future_callsign"]:
                        last_flight = await bot_router_mcp_client.ROUTER._get_from_redis(uid, "last_flight")
                        if last_flight: params[req] = last_flight
                    elif req in ["airport", "airport_code", "code", "origin", "destination"]:
                        last_ap = await bot_router_mcp_client.ROUTER._get_from_redis(uid, "last_airport")
                        if last_ap: params[req] = last_ap

        for key in list(params.keys()):
            original_val = params[key]
            if "callsign" in key:
                params[key] = resolve_callsign(original_val)
            elif any(k in key for k in ["airport", "origin", "destination", "code"]):
                params[key] = resolve_airport(original_val)

        return {"status": "success", "data": {"function": func_name, "parameters": params}}

    except json.JSONDecodeError as je:
        return {"status": "error", "message": "Invalid JSON generated", "raw": raw_response}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ---------------------------------------------------------------------
# 🌟 CLEANED DB ANALYTICS ROUTES (Delegated to web_app_db.py)
# ---------------------------------------------------------------------

def create_endpoint_handler(func):
    """Decorator to create standardized endpoint handlers"""
    async def handler(*args, **kwargs):
        return await func(db_pool, *args, **kwargs)
    return handler

# ATC Endpoints
@app.get("/api/filters")
async def api_filters():
    return await web_app_db.fetch_filter_options(db_pool)

@app.get("/api/atc/live")
async def api_atc_live(request: Request, airline: str = Query("ALL"), airport: str = Query("ALL")):
    # No auth required for radar display
    flights = await web_app_db.fetch_live_flights(db_pool, airline, airport)
    return _enrich_flights(flights or [])

@app.get("/api/aircraft/all")
async def api_aircraft_all():
    """Return all aircraft for radar display"""
    try:
        pool = web_app_db.get_db_pool(db_pool)
        if pool is None:
            logger.warning("aircraft/all: db_pool is None, creating new pool")
            import asyncpg
            pool = await asyncpg.create_pool(**Config.DB_PARAMS)
        
        if pool is None:
            logger.error("aircraft/all error: Could not get database pool")
            return []
            
        async with pool.acquire() as conn:
            # Check if table has data
            count = await conn.fetchval("SELECT COUNT(*) FROM flights_in_air WHERE last_seen > NOW() - INTERVAL '30 seconds'")
            logger.info(f"aircraft/all: Found {count} aircraft in flights_in_air")
            
            rows = await conn.fetch("""
                SELECT f.hexid, f.callsign, f.lat, f.lon, f.alt, f.speed, f.heading, f.last_seen
                FROM flights_in_air f
                WHERE f.last_seen > NOW() - INTERVAL '30 seconds'
                ORDER BY f.last_seen DESC
                LIMIT 500
            """)
            result = []
            for r in rows:
                ac = dict(r)
                ac['gs'] = ac.get('speed', 0)
                # Try to get origin/destination from schedules
                if ac.get('callsign'):
                    sched = await conn.fetchrow("""
                        SELECT airport_code, route_airport FROM flight_schedules 
                        WHERE callsign ILIKE $1 AND scheduled_time >= NOW() - INTERVAL '12 hours'
                        ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
                    """, ac['callsign'])
                    if sched:
                        ac['origin'] = sched.get('airport_code', '')
                        ac['destination'] = sched.get('route_airport', '')
                result.append(ac)
            logger.info(f"aircraft/all: Returning {len(result)} aircraft")
            return result
    except Exception as e:
        logger.error(f"aircraft/all error: {e}")
        import traceback
        traceback.print_exc()
        return []

@app.get("/api/aircraft/radar")
async def api_aircraft_radar(
    lat: float = Query(...),
    lon: float = Query(...),
    radius: float = Query(default=100.0)
):
    """Return aircraft within radius miles of given lat/lon — Redis only."""
    try:
        r = await web_app_db.get_redis_client()
        exists = await r.exists(Config.REDIS_LIVE_FLIGHTS_KEY)
        if not exists:
            return []
        all_flights = await r.hgetall(Config.REDIS_LIVE_FLIGHTS_KEY)
        if not all_flights:
            return []
        result = []
        lat_rad = radians(lat)
        lon_rad = radians(lon)
        for hex_id, data in all_flights.items():
            try:
                ac = json.loads(data)
            except:
                continue
            if not ac.get('lat') or not ac.get('lon'):
                continue
            ac_lat = ac['lat']
            ac_lon = ac['lon']
            if abs(ac_lat) > 60 and abs(ac_lon) < 60:
                ac_lat, ac_lon = ac_lon, ac_lat
            dist = _haversine_miles(lat, lon, ac_lat, ac_lon)
            if dist > radius:
                continue
            route = {}
            callsign = ac.get('callsign', '')
            if callsign:
                route_data = await r.hgetall(f"flight_route:{callsign}")
                if route_data:
                    route = {
                        'origin_icao': route_data.get('origin_icao', ''),
                        'dest_icao': route_data.get('dest_icao', ''),
                        'origin_iata': route_data.get('origin_iata', ''),
                        'dest_iata': route_data.get('dest_iata', ''),
                        'ac_type': route_data.get('ac_type', ''),
                        'reg': route_data.get('reg', ''),
                        'airline_icao': route_data.get('airline_icao', ''),
                        'airline_iata': route_data.get('airline_iata', ''),
                    }
            result.append({
                "hexid": hex_id,
                "callsign": callsign,
                "lat": ac_lat,
                "lon": ac_lon,
                "alt": ac.get('alt', 0),
                "speed": ac.get('speed', 0) or ac.get('gs', 0),
                "heading": ac.get('heading', 0),
                **route,
            })
        result.sort(key=lambda x: x.get('alt', 0), reverse=True)
        return _enrich_flights(result[:500])
    except Exception as e:
        logger.error(f"aircraft/radar error: {e}")
        return []


def _haversine_miles(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

@app.get("/api/atc/congestion")
async def api_atc_congestion():
    return await web_app_db.fetch_congestion_heatmap(db_pool)

@app.get("/api/atc/stats")
async def api_atc_stats(airline: str = Query("ALL"), airport: str = Query("ALL")):
    return await web_app_db.fetch_atc_stats(db_pool, airline, airport)

@app.get("/api/atc/bands")
async def api_atc_bands(airline: str = Query("ALL"), airport: str = Query("ALL")):
    return await web_app_db.fetch_altitude_bands(db_pool, airline, airport)

@app.get("/api/atc/anomalies")
async def api_atc_anomalies(airport: str = Query("ALL"), airline: str = Query("ALL")):
    return await web_app_db.fetch_live_anomalies(db_pool, airport, airline)

# Ops Endpoints
@app.get("/api/ops/squatters")
async def api_ops_squatters(airport: str = Query("ALL"), airline: str = Query("ALL")):
    return await web_app_db.fetch_tarmac_squatters(db_pool, airport, airline)

@app.get("/api/ops/turnarounds")
async def api_ops_turnarounds(airport: str = Query("ALL"), airline: str = Query("ALL")):
    return await web_app_db.fetch_turnarounds(db_pool, airport, airline)

@app.get("/api/drilldown/turnaround")
async def api_drilldown_turnaround(target_airline: str, airport: str = Query("ALL")):
    return await web_app_db.fetch_drilldown_turnaround(db_pool, target_airline, airport)

@app.get("/api/ops/runway_demand")
async def api_ops_runway_demand(airport: str = Query("ALL")):
    return await web_app_db.fetch_runway_demand(db_pool, airport)

@app.get("/api/ops/fleet_utilization")
async def api_ops_fleet_utilization(airline: str = Query("ALL")):
    return await web_app_db.fetch_fleet_utilization(db_pool, airline)

@app.get("/api/ops/otp")
async def api_ops_otp(airport: str = Query("ALL"), airline: str = Query("ALL")):
    return await web_app_db.fetch_otp_data(db_pool, airport, airline)

@app.get("/api/ops/schedules")
async def api_ops_schedules(airport: str = Query("ALL"), direction: str = Query("ALL"), target_date: str = Query(None)):
    return await web_app_db.fetch_airport_schedules(db_pool, airport, direction, target_date)
@app.get("/api/ops/logs")
async def api_ops_logs(airport: str = Query("ALL"), direction: str = Query("ALL"), target_date: str = Query(None)):
    return await web_app_db.fetch_airport_logs(db_pool, airport, direction, target_date)

# Exec Endpoints
@app.get("/api/exec/safety")
async def api_exec_safety():
    return await web_app_db.fetch_safety_index(db_pool)

@app.get("/api/exec/routes")
async def api_exec_routes():
    return await web_app_db.fetch_top_routes(db_pool)

@app.get("/api/exec/approach_efficiency")
async def api_exec_approach():
    return await web_app_db.fetch_cdo_efficiency(db_pool)

@app.get("/api/exec/unscheduled")
async def api_exec_unscheduled(airport: str = Query("ALL")):
    return await web_app_db.fetch_unscheduled_arrivals(db_pool, airport)

@app.get("/api/exec/training")
async def api_exec_training(airport: str = Query("ALL")):
    return await web_app_db.fetch_training_activity(db_pool, airport)

# ---------------------------------------------------------------------
# 🌟 TAB WRAPPER APIs (batch all tab data into one round-trip)
# ---------------------------------------------------------------------
@app.post("/api/atc/data")
async def api_atc_data(request: Request):
    """Return all ATC tab data in one call: bands, anomalies, aircraft_db lookup."""
    body = await request.json()
    airline = body.get("airline", "ALL")
    airport = body.get("airport", "ALL")
    hex_ids = body.get("hex_ids", [])

    async def lookup_db():
        if not hex_ids:
            return {}
        result = {}
        for h in hex_ids:
            h = h.strip().upper()
            if h in AIRCRAFT_DB:
                result[h] = AIRCRAFT_DB[h]
        return {"status": "success", "count": len(result), "data": result}

    bands, anomalies, aircraft_db = await asyncio.gather(
        web_app_db.fetch_altitude_bands(db_pool, airline, airport),
        web_app_db.fetch_live_anomalies(db_pool, airport, airline),
        lookup_db(),
    )
    return {"bands": bands, "anomalies": anomalies, "aircraft_db": aircraft_db}

@app.post("/api/ops/data")
async def api_ops_data(request: Request):
    """Return all Ops tab data in one call."""
    body = await request.json()
    airport = body.get("airport", "ALL")
    airline = body.get("airline", "ALL")

    squatters, turnarounds, runway_demand, fleet_utilization, otp = await asyncio.gather(
        web_app_db.fetch_tarmac_squatters(db_pool, airport, airline),
        web_app_db.fetch_turnarounds(db_pool, airport, airline),
        web_app_db.fetch_runway_demand(db_pool, airport),
        web_app_db.fetch_fleet_utilization(db_pool, airline),
        web_app_db.fetch_otp_data(db_pool, airport, airline),
    )
    return {
        "squatters": squatters,
        "turnarounds": turnarounds,
        "runway_demand": runway_demand,
        "fleet_utilization": fleet_utilization,
        "otp": otp,
    }

@app.post("/api/exec/data")
async def api_exec_data(request: Request):
    """Return all Exec tab data in one call."""
    body = await request.json()
    airport = body.get("airport", "ALL")

    safety, routes, cdo, unscheduled, training = await asyncio.gather(
        web_app_db.fetch_safety_index(db_pool),
        web_app_db.fetch_top_routes(db_pool),
        web_app_db.fetch_cdo_efficiency(db_pool),
        web_app_db.fetch_unscheduled_arrivals(db_pool, airport),
        web_app_db.fetch_training_activity(db_pool, airport),
    )
    return {
        "safety": safety,
        "routes": routes,
        "approach_efficiency": cdo,
        "unscheduled": unscheduled,
        "training": training,
    }

# Drilldown Endpoints
@app.get("/api/drilldown/otp")
async def drilldown_otp(target_airline: str = Query("ALL"), airport: str = Query("ALL")):
    return await web_app_db.fetch_drilldown_otp(db_pool, target_airline, airport)

@app.get("/api/drilldown/fleet")
async def drilldown_fleet(hex_id: str):
    return await web_app_db.fetch_drilldown_fleet(db_pool, hex_id)

@app.get("/api/drilldown/safety")
async def drilldown_safety(target_date: str):
    return await web_app_db.fetch_drilldown_safety(db_pool, target_date)

@app.get("/api/drilldown/cdo")
async def drilldown_cdo(target_airline: str = Query("ALL")):
    return await web_app_db.fetch_drilldown_cdo(db_pool, target_airline)

@app.get("/api/drilldown/route")
async def drilldown_route(origin: str, destination: str):
    return await web_app_db.fetch_drilldown_route(db_pool, origin, destination)

@app.get("/api/drilldown/demand")
async def drilldown_demand(hour_bucket: str, airport: str = Query("ALL")):
    return await web_app_db.fetch_drilldown_demand(db_pool, hour_bucket, airport)
    
@app.get("/api/drilldown/altitude")
async def drilldown_altitude(band: str, airline: str = Query("ALL"), airport: str = Query("ALL")):
    return await web_app_db.fetch_drilldown_altitude(db_pool, band, airline, airport)

@app.get("/api/telemetry/track")
async def get_telemetry_track(hex_id: str):
    return await web_app_db.fetch_telemetry_track(influx_query_api, hex_id)

@app.get("/api/ai/operations/enrichment")
async def get_ai_enrichment_ledger():
    return await web_app_db.fetch_ai_enrichment_ledger(db_pool)

@app.get("/api/ai/operations/insights")
async def get_ai_insights_feed():
    return await web_app_db.fetch_ai_insights_feed(db_pool)

@app.get("/api/ai/audit")
async def get_flight_ai_audit(hex_id: str = None, callsign: str = None):
    return await web_app_db.fetch_flight_ai_audit(db_pool, hex_id, callsign)

# ---------------------------------------------------------------------
# 🛩️ TAR1090 AIRCRAFT DATABASE API
# ---------------------------------------------------------------------
@app.get("/api/aircraft-db")
async def get_aircraft_db(hex_id: str = Query(None), batch: str = Query(None)):
    """Lookup aircraft info by ICAO hex. Supports single hex or comma-separated batch."""
    return _lookup_aircraft_db(hex_id, batch)

@app.post("/api/aircraft-db")
async def post_aircraft_db(request: Request):
    """Lookup aircraft info by ICAO hex. POST with {hex_ids: [...]} or {batch: "..."}."""
    body = await request.json()
    hex_ids = body.get("hex_ids", [])
    batch = body.get("batch", "")
    if hex_ids:
        return _lookup_aircraft_db(None, ",".join(hex_ids))
    return _lookup_aircraft_db(None, batch)

def _lookup_aircraft_db(hex_id, batch):
    if not AIRCRAFT_DB:
        return {"status": "error", "message": "Aircraft database not loaded"}
    if batch:
        hexes = [h.strip().upper() for h in batch.split(",") if h.strip()]
        result = {}
        for h in hexes:
            if h in AIRCRAFT_DB:
                result[h] = AIRCRAFT_DB[h]
        return {"status": "success", "count": len(result), "data": result}
    if hex_id:
        h = hex_id.strip().upper()
        if h in AIRCRAFT_DB:
            return {"status": "success", "data": AIRCRAFT_DB[h]}
        return {"status": "not_found", "hex": h}
    return {"status": "error", "message": "Provide hex_id, batch, or hex_ids"}

# ---------------------------------------------------------------------
# 🌟 WEB CHAT / PUSH
# ---------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: str = "web_user_default"

@app.post("/api/chat")
async def web_chat(req: ChatRequest):
    try:
        response = await core_ai_pipeline.process_chat_message(req.session_id, req.message)
        return {"response": response}
    except Exception as e:
        return {"response": f"⚠️ System Error: {e}"}

@app.get("/api/config")
async def get_frontend_config():
    """Return frontend polling intervals and other config."""
    return {
        "radar_poll_interval_ms": getattr(Config, 'FRONTEND_RADAR_POLL_INTERVAL_MS', 2000),
        "atc_poll_interval_ms": getattr(Config, 'FRONTEND_ATC_POLL_INTERVAL_MS', 5000),
        "ops_poll_interval_ms": getattr(Config, 'FRONTEND_OPS_POLL_INTERVAL_MS', 30000),
        "exec_poll_interval_ms": getattr(Config, 'FRONTEND_EXEC_POLL_INTERVAL_MS', 60000),
        "ws_reconnect_delay_ms": getattr(Config, 'FRONTEND_WS_RECONNECT_DELAY_MS', 3000),
        "ws_enabled": getattr(Config, 'FRONTEND_WS_ENABLED', False),
        "ws_use_for_radar": getattr(Config, 'FRONTEND_WS_USE_FOR_RADAR', False),
        "ws_use_for_atc": getattr(Config, 'FRONTEND_WS_USE_FOR_ATC', False),
    }

class PushSubscription(BaseModel):
    session_id: str
    sub_data: dict

@app.get("/api/push/public_key")
async def get_vapid_public_key():
    if getattr(Config, 'ENABLE_WEB_NOTIFICATIONS', False):
        return {"public_key": getattr(Config, 'VAPID_PUBLIC_KEY', "")}
    return {"public_key": None}

@app.post("/api/push/subscribe")
async def subscribe_web_push(sub: PushSubscription):
    if not getattr(Config, 'ENABLE_WEB_NOTIFICATIONS', False):
        return {"status": "error", "message": "Web notifications are disabled in config."}
    
    sub_json_str = json.dumps(sub.sub_data)
    await bot_router_mcp_client.save_web_push_subscription(sub.session_id, sub_json_str)
    return {"status": "success", "message": "Subscription saved successfully."}

# ============================
# Historical Data API
# ============================

@app.get("/api/history/arrivals")
async def get_arrivals_history(
    airport: str = Query("ALL"),
    start_date: str = Query(None),
    end_date: str = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0)
):
    """Get historical arrival data"""
    async with db_pool.acquire() as conn:
        where = "WHERE 1=1"
        params = []
        
        if airport != "ALL":
            params.append(airport.upper())
            where += f" AND airport = ${len(params)}"
        
        if start_date:
            params.append(start_date)
            where += f" AND timestamp >= ${len(params)}::timestamp"
        
        if end_date:
            params.append(end_date)
            where += f" AND timestamp <= ${len(params)}::timestamp"
        
        rows = await conn.fetch(f"SELECT * FROM arrivals_log {where} ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}", *params)
        count = await conn.fetchval(f"SELECT COUNT(*) FROM arrivals_log {where}", *params)
        
        return {"arrivals": [dict(r) for r in rows], "count": count, "limit": limit, "offset": offset}

@app.get("/api/history/departures")
async def get_departures_history(
    airport: str = Query("ALL"),
    start_date: str = Query(None),
    end_date: str = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0)
):
    """Get historical departure data"""
    async with db_pool.acquire() as conn:
        where = "WHERE 1=1"
        params = []
        
        if airport != "ALL":
            params.append(airport.upper())
            where += f" AND airport = ${len(params)}"
        
        if start_date:
            params.append(start_date)
            where += f" AND timestamp >= ${len(params)}::timestamp"
        
        if end_date:
            params.append(end_date)
            where += f" AND timestamp <= ${len(params)}::timestamp"
        
        rows = await conn.fetch(f"SELECT * FROM departures_log {where} ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}", *params)
        count = await conn.fetchval(f"SELECT COUNT(*) FROM departures_log {where}", *params)
        
        return {"departures": [dict(r) for r in rows], "count": count, "limit": limit, "offset": offset}

@app.get("/api/history/flights")
async def get_flight_history(
    hexid: str = Query(None),
    callsign: str = Query(None),
    limit: int = Query(100, le=500)
):
    """Get combined flight history (arrivals + departures)"""
    async with db_pool.acquire() as conn:
        flights = []
        
        if hexid or callsign:
            where = "WHERE 1=1"
            if hexid:
                where += f" AND hex_id = '{hexid.upper()}'"
            if callsign:
                where += f" AND callsign LIKE '{callsign.upper()}%'"
            
            arr = await conn.fetch(f"SELECT * FROM arrivals_log {where} ORDER BY timestamp DESC LIMIT {limit}")
            dep = await conn.fetch(f"SELECT * FROM departures_log {where} ORDER BY timestamp DESC LIMIT {limit}")
            
            for r in arr:
                flights.append({"type": "arrival", "hex_id": r["hex_id"], "callsign": r["callsign"], 
                    "airport": r["airport"], "origin": r.get("origin"), "destination": r.get("destination"),
                    "runway": r.get("runway"), "timestamp": r["timestamp"].strftime('%Y-%m-%dT%H:%M:%SZ')})
            for r in dep:
                flights.append({"type": "departure", "hex_id": r["hex_id"], "callsign": r["callsign"],
                    "airport": r["airport"], "origin": r.get("origin"), "destination": r.get("destination"),
                    "runway": r.get("runway"), "timestamp": r["timestamp"].strftime('%Y-%m-%dT%H:%M:%SZ')})
        
        flights.sort(key=lambda x: x["timestamp"], reverse=True)
        return {"flights": flights[:limit], "count": len(flights)}

@app.get("/api/history/route/{origin}/{dest}")
async def get_route_history(
    origin: str,
    dest: str,
    days: int = Query(30, le=90)
):
    """Get historical flights on a specific route"""
    async with db_pool.acquire() as conn:
        arr = await conn.fetch("""
            SELECT * FROM arrivals_log 
            WHERE origin = $1 AND airport = $2 
            AND timestamp >= NOW() - INTERVAL '1 day' * $3
            ORDER BY timestamp DESC
        """, origin.upper(), dest.upper(), days)
        
        dep = await conn.fetch("""
            SELECT * FROM departures_log 
            WHERE airport = $1 AND destination = $2 
            AND timestamp >= NOW() - INTERVAL '1 day' * $3
            ORDER BY timestamp DESC
        """, origin.upper(), dest.upper(), days)
        
        return {
            "route": f"{origin.upper()} -> {dest.upper()}",
            "days": days,
            "arrivals": [dict(r) for r in arr],
            "departures": [dict(r) for r in dep],
            "arrival_count": len(arr),
            "departure_count": len(dep)
        }

@app.get("/api/history/stats")
async def get_history_stats(days: int = Query(7, le=90)):
    """Get historical statistics"""
    async with db_pool.acquire() as conn:
        arrivals_count = await conn.fetchval("""
            SELECT COUNT(*) FROM arrivals_log 
            WHERE timestamp >= NOW() - INTERVAL '1 day' * $1
        """, days)
        
        departures_count = await conn.fetchval("""
            SELECT COUNT(*) FROM departures_log 
            WHERE timestamp >= NOW() - INTERVAL '1 day' * $1
        """, days)
        
        top_arrivals = await conn.fetch("""
            SELECT airport, COUNT(*) as c FROM arrivals_log 
            WHERE timestamp >= NOW() - INTERVAL '1 day' * $1
            GROUP BY airport ORDER BY c DESC LIMIT 10
        """, days)
        
        top_departures = await conn.fetch("""
            SELECT airport, COUNT(*) as c FROM departures_log 
            WHERE timestamp >= NOW() - INTERVAL '1 day' * $1
            GROUP BY airport ORDER BY c DESC LIMIT 10
        """, days)
        
        return {
            "period_days": days,
            "total_arrivals": arrivals_count,
            "total_departures": departures_count,
            "top_arrival_airports": [dict(r) for r in top_arrivals],
            "top_departure_airports": [dict(r) for r in top_departures]
        }

# ============================
# Delay Prediction API
# ============================

# Global predictor instance
_delay_predictor = None

async def get_predictor():
    global _delay_predictor
    if _delay_predictor is None:
        from delay_predictor import create_predictor
        _delay_predictor = await create_predictor(db_pool)
    return _delay_predictor

@app.get("/api/delay/predict")
async def predict_delay(
    callsign: str = Query(None),
    origin: str = Query(None),
    destination: str = Query(None),
    airport: str = Query(None),
    direction: str = Query(None),
    scheduled_time: str = Query(None)
):
    """Predict delay for a flight using historical data."""
    try:
        predictor = await get_predictor()
        
        # Parse scheduled time
        sched_dt = None
        if scheduled_time:
            try:
                sched_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
            except:
                pass
        
        result = await predictor.predict_delay(
            callsign=callsign,
            origin=origin,
            destination=destination,
            airport_code=airport,
            direction=direction,
            scheduled_time=sched_dt
        )
        
        return result
    except Exception as e:
        logger.error(f"Delay prediction error: {e}")
        return {"error": str(e), "predicted_delay_minutes": 0}

@app.get("/api/delay/airline_otp")
async def get_airline_otp(
    airline: str = Query(None),
    limit: int = Query(10, le=50)
):
    """Get airline On-Time Performance rankings."""
    try:
        predictor = await get_predictor()
        results = await predictor.get_airline_otp(airline, limit)
        return {"airlines": results}
    except Exception as e:
        logger.error(f"Airline OTP error: {e}")
        return {"error": str(e), "airlines": []}

@app.get("/api/delay/route_otp")
async def get_route_otp(
    origin: str = Query(None),
    destination: str = Query(None),
    limit: int = Query(10, le=50)
):
    """Get route On-Time Performance."""
    try:
        predictor = await get_predictor()
        results = await predictor.get_route_otp(origin, destination, limit)
        return {"routes": results}
    except Exception as e:
        logger.error(f"Route OTP error: {e}")
        return {"error": str(e), "routes": []}

@app.get("/api/delay/airports")
async def get_airport_delay_stats():
    """Get delay statistics by airport."""
    try:
        predictor = await get_predictor()
        data = await predictor._get_historical_data()
        
        airport_stats = []
        for (airport, direction), delay in data.get('airport_avg', {}).items():
            airport_stats.append({
                'airport': airport,
                'direction': direction,
                'avg_delay_minutes': round(delay, 1)
            })
        
        airport_stats.sort(key=lambda x: x['avg_delay_minutes'], reverse=True)
        return {"airports": airport_stats[:20]}
    except Exception as e:
        logger.error(f"Airport stats error: {e}")
        return {"error": str(e), "airports": []}

# ============================================================
# 🌟 Community Feeder Network API
# ============================================================

class FeederRegistration(BaseModel):
    station_uuid: Optional[str] = None
    location: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude_m: Optional[int] = 0
    antenna_type: Optional[str] = None
    receiver_type: Optional[str] = None

class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    callsign: Optional[str] = None

class FeederStatsUpdate(BaseModel):
    feeder_id: int
    messages_count: int
    aircraft_count: int
    positions_count: int
    max_range_km: Optional[int] = 0
    avg_range_km: Optional[int] = 0
    uptime_minutes: Optional[int] = 0

TIER_THRESHOLDS = {
    'PLATINUM': 100000,
    'GOLD': 50000,
    'SILVER': 10000,
    'BRONZE': 1000
}

def calculate_tier(messages_count: int, total_active_hours: int = 0) -> str:
    """Calculate feeder tier based on daily message count and active hours."""
    if total_active_hours < Config.FEEDER_HEALTH.get('TIER_ACTIVE_HOURS_REQUIRED', 24):
        return 'BRONZE'
    for tier, threshold in TIER_THRESHOLDS.items():
        if messages_count >= threshold:
            return tier
    return 'BRONZE'

@app.post("/api/feeders/register")
async def register_feeder(feeder: FeederRegistration, request: Request):
    """Register a new ADS-B feeder. Uses authenticated user's name/callsign from api_users."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Authentication required"})

    try:
        async with db_pool.acquire() as conn:
            # Get user profile for name/callsign
            user = await conn.fetchrow(
                "SELECT name, callsign, email FROM api_users WHERE LOWER(email) = LOWER($1)",
                user_email
            )
            if not user:
                return {"status": "error", "message": "User profile not found. Please log in again."}

            # Check if station_uuid already exists
            if feeder.station_uuid:
                existing_uuid = await conn.fetchrow(
                    "SELECT id FROM feeders WHERE station_uuid = $1",
                    feeder.station_uuid
                )
                if existing_uuid:
                    return {"status": "error", "message": "Station ID already registered"}

            result = await conn.fetchrow("""
                INSERT INTO feeders (name, email, user_email, station_uuid, location, lat, lon, altitude_m, antenna_type, receiver_type, status, tier, verified)
                VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8, $9, 'PENDING', 'BRONZE', FALSE)
                RETURNING id, name, station_uuid, tier, status
            """, user['name'] or 'Unknown', user['email'], feeder.station_uuid,
                feeder.location, feeder.lat, feeder.lon, feeder.altitude_m,
                feeder.antenna_type, feeder.receiver_type)

            return {
                "status": "success",
                "message": "Feeder registered successfully!" + (" Awaiting data from station." if feeder.station_uuid else ""),
                "feeder": dict(result)
            }
    except Exception as e:
        logger.error(f"Feeder registration error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/feeders/display-names")
async def get_feeder_display_names():
    """Return mapping of station_uuid -> display name for registered feeders.
    Used by the MLAT sync map to show friendly names instead of UUIDs."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT f.station_uuid, u.callsign, u.name
                FROM feeders f
                JOIN api_users u ON LOWER(f.user_email) = LOWER(u.email)
                WHERE f.station_uuid IS NOT NULL
            """)
            result = {}
            for row in rows:
                display_name = row['callsign'] or row['name']
                if display_name:
                    result[row['station_uuid']] = display_name
            return result
    except Exception as e:
        logger.error(f"Feeder display names error: {e}")
        return {}

@app.get("/api/auth/me")
async def get_current_user_profile(request: Request):
    """Get current user's profile (name, email, callsign)."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("""
                SELECT email, name, callsign, tier, contributor_status, contributor_since, created_at
                FROM api_users
                WHERE LOWER(email) = LOWER($1)
            """, user_email)
            if not user:
                # Fallback: auto-create if cookie exists but DB record missing
                name = user_email.split('@')[0]
                await conn.execute("""
                    INSERT INTO api_users (email, name, tier, google_id)
                    VALUES ($1, $2, 'free', 'google')
                    ON CONFLICT (email) DO NOTHING
                """, user_email, name)
                user = await conn.fetchrow("""
                    SELECT email, name, callsign, tier, contributor_status, contributor_since, created_at
                    FROM api_users
                    WHERE LOWER(email) = LOWER($1)
                """, user_email)
                if not user:
                    return JSONResponse(status_code=404, content={"error": "User not found"})

            result = dict(user)
            for ts_field in ('created_at', 'contributor_since'):
                if hasattr(result.get(ts_field), 'isoformat'):
                    result[ts_field] = result[ts_field].isoformat()
            return result
    except Exception as e:
        logger.error(f"Profile fetch error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/auth/me")
async def update_current_user_profile(request: Request, update: UserProfileUpdate):
    """Update current user's profile (name and/or callsign)."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    if not update.name and not update.callsign:
        return JSONResponse(status_code=400, content={"error": "Nothing to update"})

    try:
        async with db_pool.acquire() as conn:
            # Check callsign uniqueness if provided
            if update.callsign:
                existing = await conn.fetchrow(
                    "SELECT email FROM api_users WHERE LOWER(callsign) = LOWER($1) AND LOWER(email) != LOWER($2)",
                    update.callsign, user_email
                )
                if existing:
                    return JSONResponse(status_code=409, content={"error": "Callsign already taken"})

            # Build dynamic update
            fields = []
            values = []
            if update.name:
                fields.append("name = $" + str(len(values) + 1))
                values.append(update.name)
            if update.callsign:
                fields.append("callsign = $" + str(len(values) + 1))
                values.append(update.callsign)

            values.append(user_email)
            query = f"UPDATE api_users SET {', '.join(fields)} WHERE LOWER(email) = LOWER(${len(values)}) RETURNING email, name, callsign"
            user = await conn.fetchrow(query, *values)

            if not user:
                return JSONResponse(status_code=404, content={"error": "User not found"})

            return {"status": "success", "user": dict(user)}
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


def _generate_api_key() -> str:
    return f"br_live_{secrets.token_hex(24)}"


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class CreateKeyRequest(BaseModel):
    description: Optional[str] = "Default API Key"


@app.get("/api/keys")
async def list_api_keys(request: Request):
    """List user's API keys."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id FROM api_users WHERE LOWER(email) = LOWER($1)", user_email)
            if not user:
                return JSONResponse(status_code=404, content={"error": "User not found"})

            keys = await conn.fetch("""
                SELECT id, key_hash, description, is_active, requests_today, daily_limit, created_at
                FROM api_keys WHERE user_id = $1 ORDER BY created_at DESC
            """, user['id'])

            return {"api_keys": [dict(k) for k in keys]}
    except Exception as e:
        logger.error(f"List API keys error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/keys")
async def create_api_key(request: Request, body: CreateKeyRequest):
    """Generate a new API key for the user."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id, tier FROM api_users WHERE LOWER(email) = LOWER($1)", user_email)
            if not user:
                return JSONResponse(status_code=404, content={"error": "User not found"})

            api_key = _generate_api_key()
            key_hash = _hash_api_key(api_key)

            tier_limits = {"free": 100, "bronze": 1000, "silver": 10000, "gold": 999999999, "platinum": 999999999}
            daily_limit = tier_limits.get(user['tier'], 100)

            key_id = await conn.fetchval("""
                INSERT INTO api_keys (key_hash, user_id, description, daily_limit)
                VALUES ($1, $2, $3, $4) RETURNING id
            """, key_hash, user['id'], body.description or "Default API Key", daily_limit)

            return {"status": "success", "api_key": api_key, "key_id": key_id, "daily_limit": daily_limit}
    except Exception as e:
        logger.error(f"Create API key error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/keys/{key_id}")
async def revoke_api_key(request: Request, key_id: int):
    """Revoke an API key."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id FROM api_users WHERE LOWER(email) = LOWER($1)", user_email)
            if not user:
                return JSONResponse(status_code=404, content={"error": "User not found"})

            result = await conn.execute("""
                UPDATE api_keys SET is_active = FALSE WHERE id = $1 AND user_id = $2
            """, key_id, user['id'])

            if result == "UPDATE 0":
                return JSONResponse(status_code=404, content={"error": "Key not found"})

            return {"status": "success", "message": "API key revoked"}
    except Exception as e:
        logger.error(f"Revoke API key error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/feeders/leaderboard")
async def get_feeder_leaderboard(
    tier: str = Query(None),
    limit: int = Query(50, le=100),
    days: int = Query(7, le=30)
):
    """Get feeder leaderboard with tier filtering."""
    try:
        async with db_pool.acquire() as conn:
            where = "WHERE f.status IN ('ACTIVE', 'PENDING')"
            params = []
            
            if tier:
                params.append(tier.upper())
                where += f" AND f.tier = ${len(params)}"
            
            rows = await conn.fetch(f"""
                SELECT
                    f.id, f.name, COALESCE(u.callsign, f.name) as display_name, f.location, f.tier, f.verified, f.created_at,
                    COALESCE(SUM(ds.messages_count), 0) as total_messages,
                    COALESCE(SUM(ds.aircraft_count), 0) as total_aircraft,
                    COALESCE(SUM(ds.positions_count), 0) as total_positions,
                    COALESCE(MAX(ds.max_range_km), 0) as max_range,
                    COALESCE(SUM(ds.uptime_minutes), 0) as total_uptime
                FROM feeders f
                LEFT JOIN api_users u ON LOWER(f.user_email) = LOWER(u.email)
                LEFT JOIN feeder_daily_stats ds ON f.id = ds.feeder_id
                    AND ds.stat_date >= CURRENT_DATE - INTERVAL '1 day' * ${len(params) + 1}
                {where}
                GROUP BY f.id, f.name, u.callsign, f.location, f.tier, f.verified, f.created_at
                ORDER BY total_messages DESC
                LIMIT ${len(params) + 2}
            """, *params, days, limit)
            
            def serialize_row(r):
                d = dict(r)
                for k, v in d.items():
                    if hasattr(v, 'isoformat'):
                        d[k] = v.isoformat()
                return d

            return {
                "feeders": [serialize_row(r) for r in rows],
                "count": len(rows),
                "period_days": days
            }
    except Exception as e:
        logger.error(f"Leaderboard error: {e}")
        return {"error": str(e), "feeders": []}

@app.get("/api/feeders/profile/{feeder_id}")
async def get_feeder_profile(feeder_id: int):
    """Get detailed feeder profile with stats."""
    try:
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow("""
                SELECT id, name, email, callsign, location, lat, lon, altitude_m,
                       antenna_type, receiver_type, status, tier, verified, created_at
                FROM feeders WHERE id = $1
            """, feeder_id)
            
            if not feeder:
                return {"status": "error", "message": "Feeder not found"}
            
            # Get last 30 days stats
            stats = await conn.fetch("""
                SELECT stat_date, messages_count, aircraft_count, positions_count,
                       max_range_km, avg_range_km, uptime_minutes
                FROM feeder_daily_stats
                WHERE feeder_id = $1 AND stat_date >= CURRENT_DATE - INTERVAL '30 days'
                ORDER BY stat_date DESC
            """, feeder_id)
            
            # Get achievements
            achievements = await conn.fetch("""
                SELECT achievement_type, achievement_name, description, awarded_at
                FROM feeder_achievements
                WHERE feeder_id = $1
                ORDER BY awarded_at DESC
            """, feeder_id)
            
            return {
                "feeder": dict(feeder),
                "stats": [dict(r) for r in stats],
                "achievements": [dict(r) for r in achievements]
            }
    except Exception as e:
        logger.error(f"Feeder profile error: {e}")
        return {"error": str(e)}

@app.post("/api/feeders/stats")
async def update_feeder_stats(stats: FeederStatsUpdate):
    """Update daily feeder statistics."""
    try:
        async with db_pool.acquire() as conn:
            today = datetime.now().date()
            
            # Upsert daily stats
            await conn.execute("""
                INSERT INTO feeder_daily_stats 
                    (feeder_id, stat_date, messages_count, aircraft_count, positions_count, 
                     max_range_km, avg_range_km, uptime_minutes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (feeder_id, stat_date) 
                DO UPDATE SET
                    messages_count = EXCLUDED.messages_count,
                    aircraft_count = EXCLUDED.aircraft_count,
                    positions_count = EXCLUDED.positions_count,
                    max_range_km = EXCLUDED.max_range_km,
                    avg_range_km = EXCLUDED.avg_range_km,
                    uptime_minutes = EXCLUDED.uptime_minutes
            """, stats.feeder_id, today, stats.messages_count, stats.aircraft_count,
                stats.positions_count, stats.max_range_km, stats.avg_range_km, stats.uptime_minutes)
            
            # Update feeder tier based on today's performance
            new_tier = calculate_tier(stats.messages_count)
            await conn.execute("""
                UPDATE feeders SET tier = $1, updated_at = NOW()
                WHERE id = $2 AND tier != $1
            """, new_tier, stats.feeder_id)
            
            return {"status": "success", "tier": new_tier}
    except Exception as e:
        logger.error(f"Feeder stats update error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/feeders/coverage/gaps")
async def get_coverage_gaps(
    region: str = Query(None),
    priority: str = Query(None)
):
    """Get identified coverage gaps."""
    try:
        async with db_pool.acquire() as conn:
            where = "WHERE 1=1"
            params = []
            
            if region:
                params.append(region)
                where += f" AND region = ${len(params)}"
            
            if priority:
                params.append(priority.upper())
                where += f" AND priority = ${len(params)}"
            
            rows = await conn.fetch(f"""
                SELECT id, lat, lon, radius_km, region, priority, notes, created_at
                FROM coverage_gaps {where}
                ORDER BY 
                    CASE priority 
                        WHEN 'HIGH' THEN 1 
                        WHEN 'MEDIUM' THEN 2 
                        WHEN 'LOW' THEN 3 
                    END,
                    created_at DESC
            """, *params)
            
            return {"gaps": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Coverage gaps error: {e}")
        return {"error": str(e)}

@app.get("/api/feeders/tiers/summary")
async def get_tier_summary():
    """Get summary of feeders by tier."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT tier, COUNT(*) as count,
                       COALESCE(SUM(total_messages), 0) as total_messages
                FROM (
                    SELECT f.tier, COALESCE(SUM(ds.messages_count), 0) as total_messages
                    FROM feeders f
                    LEFT JOIN feeder_daily_stats ds ON f.id = ds.feeder_id 
                        AND ds.stat_date >= CURRENT_DATE - INTERVAL '7 days'
                    WHERE f.status = 'ACTIVE'
                    GROUP BY f.id, f.tier
                ) sub
                GROUP BY tier
                ORDER BY 
                    CASE tier 
                        WHEN 'PLATINUM' THEN 1 
                        WHEN 'GOLD' THEN 2 
                        WHEN 'SILVER' THEN 3 
                        WHEN 'BRONZE' THEN 4 
                    END
            """)
            
            return {"tiers": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Tier summary error: {e}")
        return {"error": str(e)}

# ============================================================
# 🌟 Feeder Station Management & Live Stats
# ============================================================

def extract_user_email(request: Request) -> Optional[str]:
    """Extract user email from auth cookie."""
    if not is_session_valid(request):
        return None
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if not cookie:
        return None
    # Cookie format: google_email@domain.com_timestamp or just email
    if cookie.startswith("google_"):
        # Remove prefix and timestamp suffix
        parts = cookie.split("_")
        if len(parts) >= 3:
            # Reconstruct email (may contain underscores)
            return "_".join(parts[1:-1])
    return cookie

@app.get("/api/feeders/my-stations")
async def get_my_stations(request: Request):
    """Get all stations owned by the logged-in user."""
    user_email = extract_user_email(request)
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    logger.info(f"[MY-STATIONS] Cookie: {cookie}, Extracted email: {user_email}")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, name, station_uuid, location, lat, lon,
                       altitude_m, antenna_type, receiver_type, status, tier,
                       verified, last_seen_at, total_active_hours, created_at
                FROM feeders
                WHERE user_email = $1
                ORDER BY created_at DESC
            """, user_email)

            def serialize_row(r):
                d = dict(r)
                for k, v in d.items():
                    if hasattr(v, 'isoformat'):
                        d[k] = v.isoformat()
                # Hide sensitive station_uuid from frontend
                d.pop('station_uuid', None)
                return d

            response = JSONResponse(content={"stations": [serialize_row(r) for r in rows], "count": len(rows)})
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response
    except Exception as e:
        logger.error(f"My stations error: {e}")
        return {"error": str(e), "stations": []}

@app.post("/api/feeders/{feeder_id}/claim-station")
async def claim_station(feeder_id: int, request: Request, station_uuid: str = ""):
    """Claim a station by adding its UUID."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    
    if not station_uuid:
        return {"status": "error", "message": "Station UUID is required"}
    
    try:
        async with db_pool.acquire() as conn:
            # Verify ownership
            feeder = await conn.fetchrow(
                "SELECT id, user_email FROM feeders WHERE id = $1", feeder_id
            )
            if not feeder:
                return {"status": "error", "message": "Feeder not found"}
            
            if feeder['user_email'] != user_email:
                return {"status": "error", "message": "Not authorized"}
            
            # Check if UUID is already claimed
            existing = await conn.fetchrow(
                "SELECT id FROM feeders WHERE station_uuid = $1 AND id != $2",
                station_uuid, feeder_id
            )
            if existing:
                return {"status": "error", "message": "Station ID already claimed by another feeder"}
            
            # Update station UUID
            await conn.execute(
                "UPDATE feeders SET station_uuid = $1, updated_at = NOW() WHERE id = $2",
                station_uuid, feeder_id
            )
            
            return {
                "status": "success",
                "message": "Station claimed successfully"
            }
    except Exception as e:
        logger.error(f"Claim station error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/feeders/{feeder_id}/update")
async def update_feeder(feeder_id: int, request: Request):
    """Update station details (location, lat, lon, antenna, receiver, uuid)."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Authentication required"})

    try:
        body = await request.json()
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow(
                "SELECT id, user_email FROM feeders WHERE id = $1", feeder_id
            )
            if not feeder:
                return {"status": "error", "message": "Feeder not found"}
            if feeder['user_email'] != user_email:
                return {"status": "error", "message": "Not authorized"}

            # Build dynamic update
            allowed_fields = ['station_uuid', 'location', 'lat', 'lon', 'altitude_m', 'antenna_type', 'receiver_type']
            updates = []
            values = []
            for field in allowed_fields:
                if field in body and body[field] is not None:
                    updates.append(f"{field} = ${len(values) + 1}")
                    values.append(body[field])

            if not updates:
                return {"status": "error", "message": "Nothing to update"}

            values.append(feeder_id)
            query = f"UPDATE feeders SET {', '.join(updates)}, updated_at = NOW() WHERE id = ${len(values)}"
            await conn.execute(query, *values)
            return {"status": "success", "message": "Station updated"}
    except Exception as e:
        logger.error(f"Feeder update error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/feeders/{feeder_id}/stats")
async def get_feeder_stats(feeder_id: int, request: Request):
    """Get real-time stats for a specific feeder (parse readsb JSON)."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    
    try:
        async with db_pool.acquire() as conn:
            # Verify ownership
            feeder = await conn.fetchrow(
                "SELECT id, name, user_email, station_uuid, lat, lon FROM feeders WHERE id = $1",
                feeder_id
            )
            if not feeder:
                return JSONResponse(status_code=404, content={"error": "Feeder not found"})
            
            if feeder['user_email'] != user_email:
                return JSONResponse(status_code=403, content={"error": "Not authorized"})
            
            # Query tar1090 API for per-feeder aircraft data
            import json
            station_uuid = feeder.get('station_uuid')
            feeder_aircraft = []
            data_now = None

            if station_uuid:
                try:
                    import urllib.request
                    url = f"http://planes-readsb:80/data/aircraft.json?all"
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        all_aircraft = data.get('aircraft', [])
                        feeder_aircraft = [ac for ac in all_aircraft if station_uuid.startswith(ac.get('rId', ''))]
                        data_now = data.get('now')
                except Exception as e:
                    logger.warning(f"readsb API error for feeder {feeder_id}: {e}")
                    try:
                        with open('/run/readsb/aircraft.json', 'r') as f:
                            data = json.load(f)
                            feeder_aircraft = data.get('aircraft', [])
                            data_now = data.get('now')
                    except (FileNotFoundError, json.JSONDecodeError):
                        pass
            else:
                # No station UUID, return aggregate stats from raw aircraft.json
                try:
                    with open('/run/readsb/aircraft.json', 'r') as f:
                        data = json.load(f)
                        feeder_aircraft = data.get('aircraft', [])
                        data_now = data.get('now')
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            # Calculate stats
            unique_hexes = set(ac.get('hex', '') for ac in feeder_aircraft)
            total_messages = sum(ac.get('messages', 0) for ac in feeder_aircraft)

            # Calculate max range if station lat/lon available
            max_range_km = 0
            station_lat = feeder.get('lat')
            station_lon = feeder.get('lon')

            if station_lat and station_lon:
                for ac in feeder_aircraft:
                    ac_lat = ac.get('lat')
                    ac_lon = ac.get('lon')
                    if ac_lat and ac_lon:
                        from math import radians, sin, cos, sqrt, atan2
                        R = 6371  # Earth radius in km
                        lat1, lon1 = radians(station_lat), radians(station_lon)
                        lat2, lon2 = radians(ac_lat), radians(ac_lon)
                        dlat = lat2 - lat1
                        dlon = lon2 - lon1
                        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                        c = 2 * atan2(sqrt(a), sqrt(1-a))
                        distance = R * c
                        max_range_km = max(max_range_km, distance)

            return {
                "feeder_id": feeder_id,
                "name": feeder['name'],
                "aircraft_count": len(feeder_aircraft),
                "unique_aircraft": len(unique_hexes),
                "messages_count": total_messages,
                "max_range_km": round(max_range_km, 1),
                "timestamp": data_now,
                "is_live": len(feeder_aircraft) > 0
            }
    except Exception as e:
        logger.error(f"Feeder stats error: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

@app.get("/api/feeders/health")
async def get_network_health(request: Request):
    """Network-wide feeder health summary."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    try:
        async with db_pool.acquire() as conn:
            summary = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'PENDING') as pending,
                    COUNT(*) FILTER (WHERE status = 'ACTIVATED') as activated,
                    COUNT(*) FILTER (WHERE status = 'ACTIVE') as active,
                    COUNT(*) FILTER (WHERE status = 'INACTIVE') as inactive,
                    COUNT(*) as total
                FROM feeders
                WHERE user_email IS NOT NULL
            """)
            stations = await conn.fetch("""
                SELECT f.id, f.name, f.status, f.last_seen_at, f.total_active_hours,
                       COALESCE(u.callsign, u.name, f.name) as display_name
                FROM feeders f
                LEFT JOIN api_users u ON LOWER(u.email) = LOWER(f.user_email)
                WHERE f.user_email IS NOT NULL
                ORDER BY f.id
            """)
            result = dict(summary)
            result['stations'] = [
                {
                    'id': s['id'],
                    'name': s['name'],
                    'display_name': s['display_name'],
                    'status': s['status'],
                    'last_seen_at': s['last_seen_at'].isoformat() if s['last_seen_at'] else None,
                    'total_active_hours': s['total_active_hours']
                }
                for s in stations
            ]
            return result
    except Exception as e:
        logger.error(f"Network health error: {e}")
        return {"error": str(e)}

@app.get("/api/feeders/{feeder_id}/health")
async def get_feeder_health(feeder_id: int, request: Request):
    """Per-feeder health details."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    try:
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow(
                "SELECT id, name, user_email, status, last_seen_at, total_active_hours, notify_after_hours, created_at FROM feeders WHERE id = $1",
                feeder_id
            )
            if not feeder:
                return JSONResponse(status_code=404, content={"error": "Feeder not found"})
            if feeder['user_email'] != user_email:
                return JSONResponse(status_code=403, content={"error": "Not authorized"})

            minutes_since = None
            if feeder['last_seen_at']:
                minutes_since = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - $1)) / 60", feeder['last_seen_at']
                )

            uptime_pct = None
            if feeder['created_at'] and feeder['total_active_hours']:
                hours_since_created = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - $1)) / 3600", feeder['created_at']
                )
                if hours_since_created and hours_since_created > 0:
                    uptime_pct = min(round((feeder['total_active_hours'] / hours_since_created) * 100, 1), 100.0)

            return {
                "feeder_id": feeder_id,
                "name": feeder['name'],
                "status": feeder['status'],
                "last_seen_at": feeder['last_seen_at'].isoformat() if feeder['last_seen_at'] else None,
                "minutes_since_last_seen": round(minutes_since, 1) if minutes_since else None,
                "total_active_hours": feeder['total_active_hours'],
                "uptime_pct": uptime_pct,
                "notify_after_hours": feeder['notify_after_hours']
            }
    except Exception as e:
        logger.error(f"Feeder health error: {e}")
        return {"error": str(e)}

@app.post("/api/feeders/{feeder_id}/settings")
async def update_feeder_settings(feeder_id: int, request: Request):
    """Update feeder notification settings."""
    user_email = extract_user_email(request)
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    try:
        body = await request.json()
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow(
                "SELECT id, user_email FROM feeders WHERE id = $1", feeder_id
            )
            if not feeder:
                return {"status": "error", "message": "Feeder not found"}
            if feeder['user_email'] != user_email:
                return {"status": "error", "message": "Not authorized"}

            notify = body.get('notify_after_hours')
            if notify is not None and isinstance(notify, int) and notify >= 0:
                await conn.execute(
                    "UPDATE feeders SET notify_after_hours = $1, updated_at = NOW() WHERE id = $2",
                    notify, feeder_id
                )
                return {"status": "success", "message": "Settings updated"}
            return {"status": "error", "message": "Invalid notify_after_hours"}
    except Exception as e:
        logger.error(f"Feeder settings error: {e}")
        return {"status": "error", "message": str(e)}

# ============================================================
# 🗺️ Feeder Coverage Map & Account Dashboard APIs
# ============================================================

@app.get("/api/feeders/map")
async def get_feeder_map_data():
    """Get all feeder locations for coverage map (approximate for privacy)."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    f.id,
                    COALESCE(u.callsign, f.name) as display_name,
                    f.tier,
                    f.status,
                    ROUND(f.lat::numeric, 1) as lat,
                    ROUND(f.lon::numeric, 1) as lon,
                    f.location,
                    COALESCE(s.max_range_km, 0) as max_range,
                    COALESCE(s.messages_count, 0) as daily_messages
                FROM feeders f
                LEFT JOIN api_users u ON LOWER(f.user_email) = LOWER(u.email)
                LEFT JOIN LATERAL (
                    SELECT max_range_km, messages_count
                    FROM feeder_daily_stats
                    WHERE feeder_id = f.id
                    ORDER BY stat_date DESC
                    LIMIT 1
                ) s ON true
                WHERE f.status IN ('ACTIVE', 'PENDING')
                  AND f.lat IS NOT NULL AND f.lon IS NOT NULL
            """)
            return {
                "feeders": [dict(r) for r in rows],
                "count": len(rows)
            }
    except Exception as e:
        logger.error(f"Feeder map data error: {e}")
        return {"error": str(e), "feeders": []}

@app.get("/api/feeders/{feeder_id}/history")
async def get_feeder_history(
    feeder_id: int,
    request: Request,
    days: int = Query(30, le=90)
):
    """Get historical stats for a feeder (for graphs)."""
    user_email = extract_user_email(request)
    try:
        async with db_pool.acquire() as conn:
            # Check authorization if user is logged in
            feeder = await conn.fetchrow(
                "SELECT id, user_email FROM feeders WHERE id = $1", feeder_id
            )
            if not feeder:
                return JSONResponse(status_code=404, content={"error": "Feeder not found"})

            # Public stats allowed; private stats require auth
            is_owner = user_email and feeder['user_email'] == user_email

            rows = await conn.fetch("""
                SELECT
                    stat_date,
                    messages_count,
                    aircraft_count,
                    positions_count,
                    max_range_km,
                    uptime_minutes
                FROM feeder_daily_stats
                WHERE feeder_id = $1 AND stat_date >= CURRENT_DATE - INTERVAL '1 day' * $2
                ORDER BY stat_date ASC
            """, feeder_id, days)

            # If no historical data, generate mock data for demo
            if not rows:
                from datetime import datetime, timedelta
                import random
                mock_rows = []
                base_date = datetime.now().date() - timedelta(days=days)
                for i in range(days):
                    d = base_date + timedelta(days=i)
                    mock_rows.append({
                        "stat_date": d.isoformat(),
                        "messages_count": random.randint(5000, 50000),
                        "aircraft_count": random.randint(10, 80),
                        "positions_count": random.randint(1000, 20000),
                        "max_range_km": random.randint(50, 350),
                        "uptime_minutes": random.randint(1000, 1440)
                    })
                rows = mock_rows

            totals = await conn.fetchrow("""
                SELECT
                    COALESCE(SUM(messages_count), 0) as total_messages,
                    COALESCE(SUM(aircraft_count), 0) as total_aircraft,
                    COALESCE(MAX(max_range_km), 0) as max_range_ever,
                    COUNT(DISTINCT stat_date) as active_days
                FROM feeder_daily_stats
                WHERE feeder_id = $1
            """, feeder_id)

            return {
                "history": [dict(r) if hasattr(r, 'items') else r for r in rows],
                "totals": dict(totals) if totals else {},
                "is_owner": is_owner,
                "days": days
            }
    except Exception as e:
        logger.error(f"Feeder history error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/feeders/{feeder_id}/peers")
async def get_feeder_peers(
    feeder_id: int,
    radius_km: int = Query(500, le=2000)
):
    """Get nearby feeders within radius (for peer comparison)."""
    try:
        async with db_pool.acquire() as conn:
            target = await conn.fetchrow(
                "SELECT lat, lon FROM feeders WHERE id = $1 AND lat IS NOT NULL AND lon IS NOT NULL",
                feeder_id
            )
            if not target:
                return JSONResponse(status_code=404, content={"error": "Feeder not found or no location"})

            rows = await conn.fetch("""
                SELECT
                    f.id,
                    COALESCE(u.callsign, f.name) as display_name,
                    f.tier,
                    f.lat,
                    f.lon,
                    f.location,
                    (
                        6371 * acos(
                            LEAST(1, GREATEST(-1,
                                cos(radians($1)) * cos(radians(f.lat)) *
                                cos(radians(f.lon) - radians($2)) +
                                sin(radians($1)) * sin(radians(f.lat))
                            ))
                        )
                    )::int as distance_km
                FROM feeders f
                LEFT JOIN api_users u ON LOWER(f.user_email) = LOWER(u.email)
                WHERE f.id != $3
                  AND f.lat IS NOT NULL AND f.lon IS NOT NULL
                  AND f.status IN ('ACTIVE', 'PENDING')
                  AND (
                        6371 * acos(
                            LEAST(1, GREATEST(-1,
                                cos(radians($1)) * cos(radians(f.lat)) *
                                cos(radians(f.lon) - radians($2)) +
                                sin(radians($1)) * sin(radians(f.lat))
                            ))
                        )
                  ) <= $4
                ORDER BY distance_km ASC
                LIMIT 20
            """, target['lat'], target['lon'], feeder_id, radius_km)

            return {
                "peers": [dict(r) for r in rows],
                "count": len(rows),
                "radius_km": radius_km
            }
    except Exception as e:
        logger.error(f"Feeder peers error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/feeders/coverage/heatmap")
async def get_coverage_heatmap():
    """Get coverage heatmap data (feeder locations with radii)."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    f.id,
                    COALESCE(u.callsign, f.name) as display_name,
                    f.tier,
                    f.lat,
                    f.lon,
                    COALESCE(s.max_range_km, 100) as range_km
                FROM feeders f
                LEFT JOIN api_users u ON LOWER(f.user_email) = LOWER(u.email)
                LEFT JOIN LATERAL (
                    SELECT max_range_km FROM feeder_daily_stats
                    WHERE feeder_id = f.id ORDER BY stat_date DESC LIMIT 1
                ) s ON true
                WHERE f.status IN ('ACTIVE', 'PENDING')
                  AND f.lat IS NOT NULL AND f.lon IS NOT NULL
            """)
            return {
                "points": [dict(r) for r in rows],
                "count": len(rows)
            }
    except Exception as e:
        logger.error(f"Coverage heatmap error: {e}")
        return {"error": str(e), "points": []}

# ============================================================
# 🔒 Secure tar1090 proxy - hides station UUID from browser
# ============================================================

@app.api_route("/api/feeders/{feeder_id}/view/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy_tar1090(feeder_id: int, path: str, request: Request):
    """
    Reverse-proxy tar1090 for a specific feeder.
    Browser sees only /api/feeders/<id>/view/...
    Station UUID is never exposed in URLs or page source.
    """
    user_email = extract_user_email(request)
    if not user_email:
        return RedirectResponse(url="/login")

    try:
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow(
                "SELECT id, name, user_email, station_uuid FROM feeders WHERE id = $1",
                feeder_id
            )
            if not feeder or feeder['user_email'] != user_email:
                return JSONResponse(status_code=403, content={"error": "Not authorized"})

            station_uuid = feeder.get('station_uuid')
            if not station_uuid:
                return JSONResponse(status_code=404, content={"error": "No station UUID configured"})

        # Build proxy base URL (what browser sees)
        proxy_base = f"/api/feeders/{feeder_id}/view/"

        # Build target URL on local tar1090
        target_path = path or ""

        # Everything else (HTML, JS, CSS, data/aircraft.json) proxied internally
        if target_path == 'data/aircraft.json' or target_path.endswith('/data/aircraft.json'):
            target_url = f"http://planes-readsb/tar1090/re-api/aircraft.json?all&filter_uuid={station_uuid}"
        elif target_path.startswith('re-api/'):
            qs = request.url.query or ""
            target_url = f"http://reapi-readsb.bharatradar.svc.cluster.local:30152/{target_path[7:]}"
            if qs:
                target_url += "?" + qs
        else:
            target_url = f"http://planes-readsb/{target_path}"
            qs = request.url.query or ""
            if qs:
                target_url += "?" + qs

        import httpx
        forward_headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl not in ('host', 'cookie', 'content-length', 'transfer-encoding'):
                forward_headers[k] = v

        body = await request.body()

        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
                timeout=30.0,
                follow_redirects=False
            )

        content = resp.content
        content_type = resp.headers.get('content-type', '')

        # For HTML, rewrite UUID-based URLs to proxy URLs
        if 'text/html' in content_type:
            try:
                html = content.decode('utf-8', errors='replace')
                # Inject <base href> so relative URLs resolve through proxy
                fu = station_uuid
                re_api_intercept = f'''<script>
(function(){{
var rePath='re-api/';
var fu='{fu}';
function appendFu(url){{
var sep=url.indexOf('?')>=0?'&':'?';
return url+sep+'filter_uuid='+fu;
}}
var origFetch=window.fetch;
window.fetch=function(url,opts){{
if(typeof url==='string'&&url.indexOf(rePath)>=0){{
return origFetch.call(this,appendFu(url),opts);
}}
return origFetch.call(this,url,opts);
}};
var origOpen=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url){{
if(typeof url==='string'&&url.indexOf(rePath)>=0){{
arguments[1]=appendFu(url);
}}
return origOpen.apply(this,arguments);
}};
}})();
</script>'''
                if '<head>' in html:
                    html = html.replace('<head>', f'<head><base href="{proxy_base}">{re_api_intercept}')
                elif '<HEAD>' in html:
                    html = html.replace('<HEAD>', f'<HEAD><base href="{proxy_base}">{re_api_intercept}')

                # Rewrite any absolute UUID-based URLs to proxy URLs
                uuid_url_patterns = [
                    f'href="/feeder/{station_uuid}/',
                    f"href='/feeder/{station_uuid}/",
                    f'src="/feeder/{station_uuid}/',
                    f"src='/feeder/{station_uuid}/",
                    f'url(/feeder/{station_uuid}/',
                ]
                for pat in uuid_url_patterns:
                    html = html.replace(pat, pat.replace(f'/feeder/{station_uuid}/', proxy_base))

                content = html.encode('utf-8')
            except Exception as e:
                logger.warning(f"HTML rewrite error: {e}")

        # Build response headers
        response_headers = {}
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl not in ('content-length', 'transfer-encoding', 'connection', 'keep-alive', 'content-encoding'):
                response_headers[k] = v
        response_headers['content-length'] = str(len(content))

        return Response(
            content=content,
            status_code=resp.status_code,
            headers=response_headers
        )

    except Exception as e:
        logger.error(f"tar1090 proxy error: {e}")
        return JSONResponse(status_code=500, content={"error": "Proxy error"})


@app.get("/api/feeders/{feeder_id}/map")
async def get_feeder_map_page(feeder_id: int, request: Request):
    """Redirect to the proxied tar1090 view (no UUID in URL)."""
    user_email = extract_user_email(request)
    if not user_email:
        return RedirectResponse(url="/login")

    try:
        async with db_pool.acquire() as conn:
            feeder = await conn.fetchrow(
                "SELECT id, name, user_email, station_uuid FROM feeders WHERE id = $1",
                feeder_id
            )
            if not feeder or feeder['user_email'] != user_email:
                return RedirectResponse(url="/profile")

            if not feeder.get('station_uuid'):
                return RedirectResponse(url="/profile")

        return RedirectResponse(
            url=f"/api/feeders/{feeder_id}/view/",
            status_code=302
        )
    except Exception as e:
        logger.error(f"Map page error: {e}")
        return RedirectResponse(url="/profile")


# ---------------------------------------------------------------------
# 🚀 WEBSOCKET ENDPOINT — Real-time flight updates
# ---------------------------------------------------------------------
# Polls Redis live_flights hash every 2s and pushes to connected clients.
# Fallback: if WS fails, browser reverts to REST polling (Phase 1a).
# ---------------------------------------------------------------------

# Track connected WebSocket clients
_ws_clients: set[WebSocket] = set()
_ws_broadcast_task: asyncio.Task | None = None

async def _ws_broadcast_loop():
    """Background task: poll Redis live_flights every 2s and broadcast to all clients."""
    global _ws_clients
    r = None
    while True:
        try:
            if r is None:
                r = await web_app_db.get_redis_client()
            all_flights = await r.hgetall(Config.REDIS_LIVE_FLIGHTS_KEY)
            snapshot = []
            if all_flights:
                for hex_id, data in all_flights.items():
                    try:
                        ac = json.loads(data)
                    except Exception:
                        continue
                    snapshot.append({
                        "hexid": hex_id,
                        "callsign": ac.get('callsign', ''),
                        "lat": ac.get('lat', 0),
                        "lon": ac.get('lon', 0),
                        "alt": ac.get('alt', 0),
                        "speed": ac.get('speed', 0) or ac.get('gs', 0),
                        "heading": ac.get('heading', 0)
                    })
            _enrich_flights(snapshot)
            dead_clients = set()
            for ws in _ws_clients:
                try:
                    await ws.send_json({"type": "flight_snapshot", "count": len(snapshot), "flights": snapshot})
                except Exception:
                    dead_clients.add(ws)
            _ws_clients -= dead_clients
        except Exception as e:
            logger.warning(f"[WS] broadcast error: {e}")
        await asyncio.sleep(2)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _ws_broadcast_task
    await ws.accept()
    _ws_clients.add(ws)

    # Start broadcast loop on first connection
    if _ws_broadcast_task is None or _ws_broadcast_task.done():
        _ws_broadcast_task = asyncio.create_task(_ws_broadcast_loop())

    try:
        # Handle incoming messages (e.g., get_all requests)
        async for message in ws.iter_json():
            action = message.get("action")
            if action == "get_all":
                # The broadcast loop will send the next snapshot shortly
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[WS] client error: {e}")
    finally:
        _ws_clients.discard(ws)
        # If no more clients, leave broadcast task running (cheap sleep loop)
