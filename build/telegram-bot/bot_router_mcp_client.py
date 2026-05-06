import asyncio
import logging
import asyncpg
import re
import csv
import os
import json
import redis.asyncio as redis
from contextlib import AsyncExitStack
from contextvars import ContextVar
from typing import Dict, Any, Optional
from config import Config, _env
import sys

try: from llama_cpp import Llama
except ImportError: Llama = None

logger = logging.getLogger(__name__)

DB_POOL = None
REDIS_POOL = None

AIRLINE_MAP = {}
IATA_TO_ICAO = {}
SPOKEN_TO_ICAO = {}
CURRENT_CHAT_ID: ContextVar[int] = ContextVar('CURRENT_CHAT_ID', default=0)
CURRENT_SESSION_ID: ContextVar[str] = ContextVar('CURRENT_SESSION_ID', default="")
_LOCAL_ROUTER_LLM = None

# Cloudflare key rotation index
_CF_KEY_INDEX = 0

async def init_client_state():
    global REDIS_POOL, MCP_SESSION, MCP_EXIT_STACK, DB_POOL
    
    # 1. Initialize Database Pool
    DB_POOL = await asyncpg.create_pool(**Config.DB_PARAMS)
    
    # 2. Create tables/columns if not exist
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS web_subscriptions (
                    session_id VARCHAR(255) PRIMARY KEY,
                    sub_data JSONB,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("ALTER TABLE user_alerts ADD COLUMN IF NOT EXISTS session_id VARCHAR(255)")
            logger.info("✅ DB tables verified.")
    except Exception as e:
        logger.error(f"⚠️ DB Migration Error: {e}")
    
    # 3. Initialize Distributed Redis Memory Pool
    try:
        redis_config = getattr(Config, 'REDIS_PARAMS', {"host": "127.0.0.1", "port": 6379, "db": 0, "decode_responses": True})
        REDIS_POOL = redis.Redis(**redis_config)
        await REDIS_POOL.ping() 
        logger.info("✅ Connected to Redis for Distributed Context Memory.")
    except Exception as e:
        logger.error(f"⚠️ Failed to connect to Redis: {e}. Context memory will be disabled.")
        REDIS_POOL = None

async def close_client_state():
    if DB_POOL: await DB_POOL.close()
    if REDIS_POOL: await REDIS_POOL.aclose()

def load_airlines_bot():
    global AIRLINE_MAP, IATA_TO_ICAO, SPOKEN_TO_ICAO
    if os.path.exists(Config.AIRLINES_FILE):
        try:
            with open(Config.AIRLINES_FILE, mode='r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    if str(row.get('Active', 'Y')).strip().upper() != 'N':
                        icao = row.get('ICAO', '').strip().upper()
                        iata = row.get('IATA', '').strip().upper()
                        name = row.get('Name', '').strip().upper()
                        if icao and len(icao) == 3 and icao not in ['N/A', '\\N']:
                            AIRLINE_MAP[icao] = name
                            if iata and len(iata) == 2 and iata not in ['N/A', '\\N', '-']:
                                IATA_TO_ICAO[iata] = icao
                            if name:
                                clean_spoken = name.replace(" AIRLINES", "").replace(" AIRWAYS", "").replace(" AIR", "").replace(" LIMITED", "").replace(" CORPORATION", "").strip().replace(" ", "")
                                if clean_spoken:
                                    SPOKEN_TO_ICAO[clean_spoken] = icao
            logger.info(f"✅ Loaded {len(AIRLINE_MAP)} airlines, {len(IATA_TO_ICAO)} IATA codes, and {len(SPOKEN_TO_ICAO)} spoken names.")
        except Exception as e:
            logger.error(f"⚠️ Failed to load airlines in bot: {e}")

async def save_web_push_subscription(session_id: str, sub_data: str):
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("""
                INSERT INTO web_subscriptions (session_id, sub_data, updated_at) 
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (session_id) DO UPDATE SET sub_data = EXCLUDED.sub_data, updated_at = NOW()
            """, session_id, sub_data)
    except Exception as e:
        logger.error(f"Failed to save web push sub: {e}")

async def normalize_callsign(callsign):
    if not callsign: return None
    clean = callsign.upper().replace(" ", "").strip()
    
    # Redis cache
    cache_key = f"alias:{clean}"
    if REDIS_POOL:
        try:
            cached = await REDIS_POOL.get(cache_key)
            if cached: return cached
        except: pass
    
    icao_variant = clean
    for spoken, icao in SPOKEN_TO_ICAO.items():
        if icao_variant.startswith(spoken):
            icao_variant = icao_variant.replace(spoken, icao, 1)
            break
    for iata, icao in IATA_TO_ICAO.items():
        if icao_variant.startswith(iata) and len(icao_variant) > len(iata) and icao_variant[len(iata)].isdigit():
            icao_variant = icao_variant.replace(iata, icao, 1)
            break
    match = re.match(r"([A-Z0-9]+)(\d+)([A-Z]*)", icao_variant)
    result = f"{match.group(1)}{int(match.group(2))}{match.group(3)}" if match else icao_variant
    
    # Cache for 4 hours
    if REDIS_POOL:
        try:
            await REDIS_POOL.setex(cache_key, 14400, result)
        except: pass
    return result

def resolve_to_icao(code):
    if not code: return None
    code = code.upper().strip()
    for icao, data in Config.TARGET_AIRPORTS.items():
        if code == icao or code == data.get('iata', '') or code == data.get('name', '').upper():
            return icao
    return code

# ==========================================
# 🌟 MCP TOOL EXECUTOR
# ==========================================
# 🌟 DIRECT TOOL EXECUTOR (no MCP)
# ==========================================
async def execute_tool_via_mcp(tool_name: str, params: dict) -> str:
    """Directly calls tool functions - no MCP protocol."""
    logger.info(f"CALLING TOOL: {tool_name} with params: {params}")
    
    if not tool_name:
        return "⚠️ No tool specified."
    
    try:
        # Initialize mcp_server's DB pool if not exists
        import mcp_server as mcp_module
        if mcp_module.DB_POOL is None:
            logger.info("📡 Initializing mcp_server DB pool")
            import asyncpg
            mcp_module.DB_POOL = await asyncpg.create_pool(**Config.DB_PARAMS)
            mcp_module.REDIS_POOL = REDIS_POOL
        
        tool_func = getattr(mcp_module, tool_name, None)
        if tool_func and callable(tool_func):
            logger.info(f"📡 Calling {tool_name} directly")
            # Map params to match function signature
            import inspect
            sig = inspect.signature(tool_func)
            valid_params = {k: v for k, v in params.items() if k in sig.parameters}
            logger.info(f"DEBUG: calling {tool_name} with params: {valid_params}")
            result = await tool_func(**valid_params)
            return result
        else:
            logger.warning(f"⚠️ Tool {tool_name} not found - using fallback")
            return await _execute_tool_local(tool_name, params)
    except Exception as e:
        logger.error(f"⚠️ Tool Error: {type(e).__name__}: {e}")
        return await _execute_tool_local(tool_name, params)
        logger.info(f"📡 Sending to MCP: {tool_name} with {params}")
        result = await MCP_SESSION.call_tool(tool_name, arguments=params)
        if result.content and len(result.content) > 0:
            return result.content[0].text
        else:
            logger.warning(f"⚠️ MCP returned empty content for {tool_name}")
            return f"⚠️ No data returned for {tool_name}."
    except Exception as e:
        logger.error(f"⚠️ MCP Execution Error: {type(e).__name__}: {e}")
        return await _execute_tool_local(tool_name, params)

async def _execute_tool_local(tool_name: str, params: dict) -> str:
    """Direct database fallback when MCP is unavailable."""
    logger.info(f"LOCAL FALLBACK for: {tool_name}")
    try:
        import redis.asyncio as redis
        
        rp = getattr(Config, 'REDIS_PARAMS', {})
        r = redis.Redis(
            host=rp.get('host', '192.168.200.15'), 
            port=rp.get('port', 6379),
            password=rp.get('password'),
            decode_responses=True
        )
        
        if tool_name in ["get_airspace_pulse", "get_inbound_flights", "get_outbound_flights"]:
            # Key is a hash, not string
            flights_data = await r.hgetall("live_flights")
            if flights_data:
                count = len(flights_data)
                logger.info(f"LOCAL FALLBACK returned: {count} aircraft")
                return f"✈️ Currently tracking {count} aircraft in airspace."
            logger.info("LOCAL FALLBACK returned: No flights")
            return "🛫 No live flights currently in tracking range."
        logger.info(f"LOCAL FALLBACK: unsupported tool {tool_name}")
        return f"⚠️ Tool {tool_name} unavailable - MCP offline."
    except Exception as e:
        logger.info(f"LOCAL FALLBACK error: {e}")
        return f"⚠️ Local fallback error: {e}"

# ==========================================
# 🌟 THE LLM SCHEMAS
# ==========================================
FAST_ROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_flight_alert",
            "description": "Sets a proactive notification (alert, ping, notify) for a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "callsign": {"type": "string", "description": "The flight callsign."},
                    "alert_type": {"type": "string", "description": "Use 'ETA_WARNING' if user mentions time. Use 'LANDED' ONLY if touchdown is requested."},
                    "threshold_mins": {"type": "integer", "description": "Minutes away to trigger the alert (0 if 'LANDED')."}
                },
                "required": ["callsign", "alert_type", "threshold_mins"]
            }
        }
    },    
    {
        "type": "function",
        "function": {
            "name": "get_unified_airport_timetable",
            "description": "Get a chronological timetable of past and future flights for an airport.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airport": {"type": "string"},
                    "board_type": {"type": "string"},
                    "time_modifier": {"type": "string"},
                    "partner_airport": {"type": "string"},
                    "airline_code": {"type": "string"}
                },
                "required": ["airport"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_flight_status",
            "description": "Get live telemetry, route, ETA, ground ops, and schedule delay for a specific flight.",
            "parameters": {
                "type": "object",
                "properties": {"callsign_raw": {"type": "string", "description": "The flight callsign (e.g., AIC416, VTE153)"}},
                "required": ["callsign_raw"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_inbound_flights",
            "description": "Use this when the user asks for flights approaching, inbound, or heading to a specific city/airport.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airport_code": {"type": "string", "description": "The destination airport code"},
                    "origin_airport": {"type": "string", "description": "Optional origin airport filter"}
                },
                "required": ["airport_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_status_board",
            "description": "Get a complete daily timeline for flights between two cities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Origin airport code"},
                    "destination": {"type": "string", "description": "Destination airport code"}
                },
                "required": ["origin", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_airframe_history",
            "description": "Trace daily multi-hop history & turnaround times for a specific callsign or Hex ID.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Flight callsign or Hex ID"}},
                "required": ["identifier"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_inbound_aircraft_status",
            "description": "Finds the incoming physical aircraft that will operate a scheduled departing flight.",
            "parameters": {
                "type": "object",
                "properties": {"departing_callsign": {"type": "string", "description": "The departing flight callsign"}},
                "required": ["departing_callsign"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "predict_flight_assignment",
            "description": "Predict which aircraft will operate a future scheduled flight.",
            "parameters": {
                "type": "object",
                "properties": {"future_callsign": {"type": "string", "description": "Future scheduled flight callsign"}},
                "required": ["future_callsign"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_airport_traffic",
            "description": "Get current airport traffic and congestion info.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Airport code"}},
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_airport_turnarounds",
            "description": "Get turnaround performance for an airport.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airport_code": {"type": "string", "description": "Airport code"},
                    "airline_code": {"type": "string", "description": "Optional airline filter"}
                },
                "required": ["airport_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_average_turnaround_by_airline",
            "description": "Get average turnaround time by airline at an airport.",
            "parameters": {
                "type": "object",
                "properties": {"airport_code": {"type": "string", "description": "Airport code"}},
                "required": ["airport_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_airport_anomalies",
            "description": "Get safety anomalies and incidents at an airport.",
            "parameters": {
                "type": "object",
                "properties": {"airport_code": {"type": "string", "description": "Airport code"}},
                "required": ["airport_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {"name": "get_airspace_pulse", "description": "Get global stats on currently tracked airspace.", "parameters": {}}
    },
    {
        "type": "function",
        "function": {"name": "get_system_health", "description": "Hardware Stats of the server.", "parameters": {}}
    },
    {
        "type": "function",
        "function": {
            "name": "get_delay_prediction",
            "description": "Predict delay for a flight, route, or airport. Use when user asks about delays, lateness, or expected arrival time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "callsign": {"type": "string", "description": "Flight callsign (optional)"},
                    "origin": {"type": "string", "description": "Origin airport code (optional)"},
                    "destination": {"type": "string", "description": "Destination airport code (optional)"},
                    "airport_code": {"type": "string", "description": "Airport code to check congestion (optional)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_airline_delay_stats",
            "description": "Get airline on-time performance and delay rankings. Use when user asks about airline punctuality, which airlines are delayed, or OTP statistics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "airline_code": {"type": "string", "description": "Airline ICAO or IATA code (optional, leave empty for rankings)"},
                    "limit": {"type": "integer", "description": "Number of results to return"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_delay_stats",
            "description": "Get route delay statistics. Use when user asks about delays on a specific route, worst routes, or route performance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Origin airport code (optional)"},
                    "destination": {"type": "string", "description": "Destination airport code (optional)"},
                    "limit": {"type": "integer", "description": "Number of results to return"}
                },
                "required": []
            }
        }
    }
]

# ==========================================
# 🌟 SEMANTIC ROUTER (The SLM Bridge)
# ==========================================
class AviationSemanticRouter:
    def __init__(self):
        self.tools = FAST_ROUTER_TOOLS
        self.city_map = {}
        csv_path = "data/airports.csv"
        if os.path.exists(csv_path):
            with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
                rows.sort(key=lambda x: bool(x.get('IATA', '').strip()))
                for row in rows:
                    code = row.get('ICAO', '').strip() or row.get('IATA', '').strip()
                    if not code: continue
                    city = row.get('Location', '').strip().lower()
                    name = row.get('Name', '').strip().lower()
                    if city:
                        self.city_map[city] = code
                        self.city_map[f"{city} airport"] = code
                    if name: self.city_map[name] = code

        manual_aliases = {
            "pune": "VAPO", "mumbai": "VABB", "bangalore": "VOBL", "hyderabad": "VOHS",
            "delhi": "VIDP", "chennai": "VOMM", "kolkata": "VECC", "ahmedabad": "VAAH",
            "pune airport": "VAPO", "mumbai airport": "VABB", "delhi airport": "VIDP"
        }        
        for alias, code in manual_aliases.items(): self.city_map[alias] = code

        for icao, data in Config.TARGET_AIRPORTS.items():
            self.city_map[data.get('name', '').lower()] = icao
            if data.get('iata'): self.city_map[data['iata'].lower()] = icao
            if data.get('city'):
                self.city_map[data['city'].lower()] = icao
                self.city_map[f"{data['city'].lower()} airport"] = icao

    async def _save_to_redis(self, uid: str, key: str, value: str):
        if REDIS_POOL and uid and value:
            try: await REDIS_POOL.setex(f"context:{uid}:{key}", 1800, value)
            except: pass

    async def _get_from_redis(self, uid: str, key: str) -> Optional[str]:
        if REDIS_POOL and uid:
            try:
                val = await REDIS_POOL.get(f"context:{uid}:{key}")
                return val if val else None
            except: pass
        return None

    async def extract_entities_async(self, text: str) -> Dict[str, Any]:
        text_lower, text_upper = text.lower(), text.upper()
        flights = re.findall(r'\b[A-Z]{2,3}\d{3,4}[A-Z]?\b|\b[A-F0-9]{6}\b', text_upper)
        airports = []
        words = re.findall(r'\b\w+\b', text_lower) 
        
        i = 0
        while i < len(words):
            match_found = False
            for n in range(4, 0, -1):
                if i + n <= len(words):
                    phrase = " ".join(words[i:i+n])
                    if phrase in self.city_map:
                        airports.append(self.city_map[phrase])
                        i += n 
                        match_found = True
                        break
            if not match_found: i += 1

        time_match = re.search(r'(\d+)\s*(?:mins?|minutes?|min|away|before)', text_lower)
        threshold = int(time_match.group(1)) if time_match else None
        
        time_mod = None
        for tm in ["morning", "afternoon", "evening", "night", "today", "tomorrow"]:
            if tm in text_lower: time_mod = tm; break
        
        uid = str(CURRENT_CHAT_ID.get() or CURRENT_SESSION_ID.get())
        if uid:
            if flights: await self._save_to_redis(uid, "last_flight", flights[0])
            if airports: await self._save_to_redis(uid, "last_airport", airports[0])

        return {"flights": flights, "airports": airports, "threshold_mins": threshold, "time_modifier": time_mod, "board_type": "ARRIVALS" if any(x in text_lower for x in ["arriv", "landing", "approach"]) else "DEPARTURES"}

    async def route_free_text(self, query: str) -> Dict[str, Any]:
        entities = await self.extract_entities_async(query)
        return await self._llm_semantic_route(query, entities)

    async def _llm_semantic_route(self, user_input: str, entities: dict) -> dict:
        SYSTEM_PROMPT_LLAMA_SEREVR = "You are a flight routing AI. You must ONLY output JSON. Allowed functions: set_flight_alert, get_unified_airport_timetable, get_flight_status, get_inbound_flights, get_route_status_board, get_airport_turnarounds, get_average_turnaround_by_airline, get_airport_anomalies, get_airframe_history, get_inbound_aircraft_status, predict_flight_assignment, get_airport_traffic, get_airspace_pulse, get_system_health, unsupported."
        
        examples = """
Examples:
- "Jabalpur board" -> {"function":"get_unified_airport_timetable","parameters":{"airport":"Jabalpur","board_type":"ARRIVALS"}}
- "arrivals Delhi" -> {"function":"get_unified_airport_timetable","parameters":{"airport":"Delhi","board_type":"ARRIVALS"}}
- "departures Mumbai" -> {"function":"get_unified_airport_timetable","parameters":{"airport":"Mumbai","board_type":"DEPARTURES"}}
- "planes at VEGY" -> {"function":"get_airport_traffic","parameters":{"code":"VEGY"}}
- "aircraft parked at Chennai" -> {"function":"get_airport_traffic","parameters":{"code":"Chennai"}}
- "where is aic1376" -> {"function":"get_inbound_aircraft_status","parameters":{"callsign":"aic1376"}}
- "is IGO456 flying" -> {"function":"get_flight_status","parameters":{"callsign_raw":"IGO456"}}
- "track 6E712" -> {"function":"get_flight_status","parameters":{"callsign_raw":"6E712"}}
- "flights Pune to Jaipur" -> {"function":"get_route_status_board","parameters":{"origin":"Pune","destination":"Jaipur"}}
- "incoming to Goa" -> {"function":"get_inbound_flights","parameters":{"airport_code":"Goa"}}
- "alert when JAI773 lands" -> {"function":"set_flight_alert","parameters":{"callsign":"JAI773","alert_type":"LANDING","threshold_mins":30}}
- "turnaround at DEL" -> {"function":"get_airport_turnarounds","parameters":{"airport_code":"DEL"}}
- "Air India at BLR" -> {"function":"get_airport_turnarounds","parameters":{"airport_code":"BLR","airline_code":"Air India"}}
- "delays at Trivandrum" -> {"function":"get_airport_anomalies","parameters":{"airport_code":"Trivandrum"}}
- "flights in air" -> {"function":"get_airspace_pulse","parameters":{}}
- "predict IXO561" -> {"function":"predict_flight_assignment","parameters":{"future_callsign":"IXO561"}}
- "which plane will JAI5963 be" -> {"function":"predict_flight_assignment","parameters":{"future_callsign":"JAI5963"}}
- "Indigo performance at Ahmedabad" -> {"function":"get_average_turnaround_by_airline","parameters":{"airport_code":"Ahmedabad"}}
- "average turnaround SpiceJet" -> {"function":"get_average_turnaround_by_airline","parameters":{"airport_code":null}}
- "is my flight delayed" -> {"function":"get_delay_prediction","parameters":{"callsign":"IGO123"}}
- "delay on DEL-BOM route" -> {"function":"get_route_delay_stats","parameters":{"origin":"DEL","destination":"BOM"}}
- "which airline is most delayed" -> {"function":"get_airline_delay_stats","parameters":{}}
- "IndiGo delay stats" -> {"function":"get_airline_delay_stats","parameters":{"airline_code":"IGO"}}
- "worst route for delays" -> {"function":"get_route_delay_stats","parameters":{}}
- "book ticket" -> {"function":"unsupported","parameters":{}}
- "hello" -> {"function":"unsupported","parameters":{}}
"""
        
        system_prompt = f"""You are an expert aviation AI assistant that routes natural English queries to the correct API tool.
You MUST output ONLY valid JSON with no markdown, explanations, or extra text.
JSON format: {{"function": "<tool_name>", "parameters": {{<param_key>: <param_value>}}}}

ROUTING RULES:
1. "alert/notify/remind" → set_flight_alert
2. "where/track/status" for a flight → get_flight_status
3. "inbound/approaching/heading to" → get_inbound_flights with optional origin filter if mentioned
4. "arrivals/departures/timetable/board" → get_unified_airport_timetable with inferred board type and time modifier
5. "incoming/connecting/plane for" a departing flight → get_inbound_aircraft_status
6. "history/past/airframe/trace" → get_airframe_history
7. "route/origin to destination" → get_route_status_board
8. "leaving for {{city}} from {{city}}" → get_route_status_board
9. "arriving from {{city}} to {{city}}" → get_route_status_board
10. "assign/predict/aircraft/tail" → predict_flight_assignment
11. "busy/congestion/traffic/airspace" → get_airport_traffic or get_airspace_pulse
12. "anomaly/incident/safety/diversion/go-around" → get_airport_anomalies
13. "turnaround/performance" → get_airport_turnarounds or get_average_turnaround_by_airline
14. "system/health/backend" → get_system_health
15. "delay/late/on-time/punctual/when will it arrive" for specific flight → get_delay_prediction with callsign
16. "delay on route/worst route/most delayed route" → get_route_delay_stats with origin/destination if mentioned
17. "airline delay/which airline is delayed/OTP/punctuality" → get_airline_delay_stats with airline_code if mentioned

{examples}
IMPORTANT: Output ONLY JSON function call like the examples above. DO NOT wrap in markdown code blocks."""
        
        # Use provider hierarchy from config
        primary_provider = getattr(Config, 'FAST_ROUTER_PROVIDER', 'groq')
        fallback_hierarchy = getattr(Config, 'FAST_ROUTER_FALLBACK_QUEUE', ['groq', 'cloudflare'])
        logger.info(f"🔧 Provider: primary={primary_provider}, fallback={fallback_hierarchy}")
        
        # Build execution queue
        execution_queue = [primary_provider]
        for p in fallback_hierarchy:
            if p not in execution_queue:
                execution_queue.append(p)
        
        # Try each provider in order
        for provider in execution_queue:
            try:
                logger.info(f"==============================================")
                logger.info(f"PROVIDER: {provider}")
                logger.info(f"INPUT: {user_input}")
                
                response = None
                raw_content = None
                
                if provider == "local_gguf":
                    global _LOCAL_ROUTER_LLM
                    if not _LOCAL_ROUTER_LLM and Llama:
                        _LOCAL_ROUTER_LLM = Llama(
                            model_path=Config.LOCAL_GGUF_PATH, 
                            n_ctx=getattr(Config, 'LOCAL_GGUF_CTX', 1024), 
                            n_batch=512, n_gpu_layers=0, n_threads=3, 
                            chat_format="gemma", use_mlock=True, verbose=False
                        )
                    if not _LOCAL_ROUTER_LLM:
                        raise Exception("Local GGUF router not initialized")
                    raw_prompt = f"<start_of_turn>user\n{SYSTEM_PROMPT_LLAMA_SEREVR}\n\n{user_input}<end_of_turn>\n<start_of_turn>model\n"
                    response = await asyncio.to_thread(_LOCAL_ROUTER_LLM, raw_prompt, max_tokens=250, temperature=0.0, stop=["<end_of_turn>"])
                    raw_content = response["choices"][0]["text"].strip()
                elif provider == "groq":
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            json={
                                "model": getattr(Config, 'FAST_ROUTER_MODEL', 'llama-3.1-8b-instant'),
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_input}
                                ],
                                "max_tokens": 512,
                                "temperature": 0.1,
                                "response_format": {"type": "json_object"}
                            },
                            headers={"Authorization": f"Bearer {Config.GROQ_API_KEY}", "Content-Type": "application/json"}
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                raw_content = data["choices"][0]["message"]["content"].strip()
                            else:
                                body = await resp.text()
                                logger.error(f"⚠️ Groq API error {resp.status}: {body}")
                                continue
                elif provider == "cloudflare":
                    import aiohttp
                    # Use CLOUDFLARE_KEYS for rotation, fallback to single account
                    cf_keys = getattr(Config, 'CLOUDFLARE_KEYS', [])
                    global _CF_KEY_INDEX
                    if cf_keys:
                        creds = cf_keys[_CF_KEY_INDEX % len(cf_keys)]
                        _CF_KEY_INDEX += 1
                        cf_account, cf_token = creds['id'], creds['token']
                    else:
                        cf_account, cf_token = Config.CLOUDFLARE_ACCOUNT_ID, Config.CLOUDFLARE_API_TOKEN
                    
                    api_url = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1/chat/completions"
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            api_url,
                            json={
                                "model": "@cf/meta/llama-3.1-8b-instruct",
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_input}
                                ],
                                "max_tokens": 512
                            },
                            headers={"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                raw_content = data["choices"][0]["message"]["content"].strip()
                            else:
                                continue
                elif provider == "llama-server":
                    import aiohttp
                    raw_prompt = f"<start_of_turn>user\n{SYSTEM_PROMPT_LLAMA_SEREVR}\n\n{user_input}<end_of_turn>\n<start_of_turn>model\n"
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            Config.LLAMA_SERVER_URL.replace("chat/completions", "completions"),
                            json={"prompt": raw_prompt, "temperature": 0.0, "max_tokens": 250, "stop": ["<end_of_turn>"]},
                            timeout=aiohttp.ClientTimeout(total=30)
) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                raw_content = data["choices"][0]["message"]["content"].strip()
                                logger.info(f"GROQ_RESPONSE: {raw_content}")
                            else:
                                continue
                else:
                    continue  # Skip unknown providers
                
                # Parse response
                logger.info(f"🔍 Raw LLM response: {raw_content}")
                json_start_index = raw_content.find('{')
                if json_start_index == -1: raise Exception("No JSON found in response")
                
                try:
                    tc = json.loads(raw_content[json_start_index:])
                except json.JSONDecodeError as je:
                    raise Exception(f"Failed to parse JSON: {je}")
                
                clean_params = {k: v for k, v in tc.get("parameters", {}).items() if v is not None}
                
                logger.info(f"EXTRACTED FUNCTION: {tc.get('function')}")
                logger.info(f"EXTRACTED PARAMS: {clean_params}")
                
                # Normalize parameter names that LLM might get wrong (only if target not already set)
                if "flight_number" in clean_params and "callsign_raw" not in clean_params:
                    clean_params["callsign_raw"] = clean_params.pop("flight_number")
                if "flight_callsign" in clean_params and "callsign_raw" not in clean_params:
                    clean_params["callsign_raw"] = clean_params.pop("flight_callsign")
                
                # Remap callsign_raw to callsign for delay prediction tools
                if tc.get("function") == "get_delay_prediction" and "callsign_raw" in clean_params:
                    clean_params["callsign"] = clean_params.pop("callsign_raw")
                    clean_params["callsign_raw"] = clean_params.pop("flight_callsign")
                if "callsign" in clean_params and "callsign_raw" not in clean_params:
                    clean_params["callsign_raw"] = clean_params.pop("callsign")
                if "dest" in clean_params and "destination" not in clean_params:
                    clean_params["destination"] = clean_params.pop("dest")
                if "src" in clean_params and "origin" not in clean_params:
                    clean_params["origin"] = clean_params.pop("src")
                if "to" in clean_params and "destination" not in clean_params:
                    clean_params["destination"] = clean_params.pop("to")
                if "from" in clean_params and "origin" not in clean_params:
                    clean_params["origin"] = clean_params.pop("from")
                    
                return {"function": tc.get("function"), "parameters": clean_params}
                
            except Exception as e:
                logger.warning(f"⚠️ Router provider {provider} failed: {e}")
                continue  # Try next provider
        
        # All providers failed - use fallback
        logger.warning("⚠️ All router providers failed, using fallback")
        if entities.get("flights"): return {"function": "get_flight_status", "parameters": {"callsign_raw": entities["flights"][0]}}
        elif entities.get("airports"): return {"function": "get_unified_airport_timetable", "parameters": {"airport": entities["airports"][0], "board_type": "ARRIVALS", "time_modifier": "all"}}
        return {"function": "get_airspace_pulse", "parameters": {}}

ROUTER = AviationSemanticRouter()

async def smart_route_free_text(query: str) -> str:
    """Takes user text, processes context, and delegates to the MCP Server."""
    result = await ROUTER.route_free_text(query)
    tool_name = result.get("function")
    params = result.get("parameters", {})
    
    logger.info(f"ROUTE RESULT: {tool_name} with {params}")

    for key, val in params.items():
        if isinstance(val, str):
            if key in ["airport", "airport_code", "code", "origin", "destination", "partner_airport"]:
                old_val = val
                if val.lower() in ROUTER.city_map: 
                    params[key] = ROUTER.city_map[val.lower()]
                    logger.info(f"CITY MAP TRANSFORM: {key}: {old_val} -> {params[key]}")
            elif key in ["callsign", "callsign_raw", "departing_callsign", "future_callsign"]:
                params[key] = await normalize_callsign(val)

    # Context Injection
    uid = str(CURRENT_CHAT_ID.get() or CURRENT_SESSION_ID.get())
    tool_def = next((t for t in FAST_ROUTER_TOOLS if t['function']['name'] == tool_name), None)
    
    if tool_def:
        required_params = tool_def['function']['parameters'].get('required', [])
        for req in required_params:
            if req not in params or not params[req]:
                if uid:
                    if req in ["callsign", "callsign_raw", "departing_callsign", "identifier", "future_callsign"]:
                        last_flight = await ROUTER._get_from_redis(uid, "last_flight")
                        if last_flight: params[req] = last_flight
                    elif req in ["airport", "airport_code", "code", "origin", "destination"]:
                        last_ap = await ROUTER._get_from_redis(uid, "last_airport")
                        if last_ap: params[req] = last_ap
        
        missing = [p for p in required_params if p not in params or params[p] is None]
        if missing:
            if "callsign_raw" in missing or "callsign" in missing: return "What is the specific flight number?"
            if "airport_code" in missing or "airport" in missing: return "Which airport or city are you asking about?"
            return f"❓ Need more info: {', '.join(missing)}"

    # Validate tool_name before execution
    if not tool_name or tool_name is None:
        logger.warning("⚠️ No function returned by router, using fallback")
        # Extract entities locally for fallback
        fallback_entities = await ROUTER.extract_entities_async(query)
        if fallback_entities.get("flights"):
            return await execute_tool_via_mcp("get_flight_status", {"callsign_raw": fallback_entities["flights"][0]})
        elif fallback_entities.get("airports"):
            return await execute_tool_via_mcp("get_unified_airport_timetable", {"airport": fallback_entities["airports"][0], "board_type": "ARRIVALS", "time_modifier": "all"})
        return await execute_tool_via_mcp("get_airspace_pulse", {})
    
    # Proxy the system-level details for alerts
    if tool_name == "set_flight_alert":
        params["chat_id"] = CURRENT_CHAT_ID.get() or 0
        params["session_id"] = CURRENT_SESSION_ID.get() or ""

    # 🚀 SEND TO MCP SERVER 🚀
    return await execute_tool_via_mcp(tool_name, params)


# 🌟 FUNCTION_DISPATCHER for core_ai_pipeline compatibility
class FunctionTool:
    """Wrapper to make smart_route_free_text compatible with .ainvoke() calls."""
    async def ainvoke(self, query: str) -> str:
        return await smart_route_free_text(query)


FUNCTION_DISPATCHER = {
    "smart_route_free_text": FunctionTool()
}

DB_POOL = None

async def get_db_pool():
    global DB_POOL
    if DB_POOL is None:
        import asyncpg
        DB_POOL = await asyncpg.create_pool(**Config.DB_PARAMS)
    return DB_POOL

async def get_active_alerts():
    """Get all ACTIVE alerts from user_alerts table with web push data."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT a.id, a.chat_id, a.session_id, a.target_callsign, a.alert_type, a.threshold_mins, a.status, w.sub_data 
                FROM user_alerts a 
                LEFT JOIN web_subscriptions w ON a.session_id = w.session_id
                WHERE a.status = 'ACTIVE'
            """)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_active_alerts error: {e}")
        return []

async def update_alert_status(alert_id: int, status: str):
    """Update alert status in database."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE user_alerts SET status = $1 WHERE id = $2", status, alert_id)
    except Exception as e:
        logger.error(f"update_alert_status error: {e}")

async def resolve_watchdog_target(clean_cs: str):
    """Resolve the actual aircraft for connecting flight."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            dep_sched = await conn.fetchrow("""
                SELECT hex_id, airport_code, scheduled_time 
                FROM flight_schedules 
                WHERE (callsign = $1 OR flight_number = $1) AND direction = 'DEPARTURES' 
                AND scheduled_time >= NOW() - INTERVAL '12 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours' 
                AND hex_id IS NOT NULL 
                ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
            """, clean_cs)
            
            if dep_sched and dep_sched['hex_id']:
                arr_sched = await conn.fetchrow("""
                    SELECT callsign FROM flight_schedules 
                    WHERE LOWER(hex_id) = LOWER($1) AND airport_code = $2 AND direction = 'ARRIVALS' AND scheduled_time <= $3 
                    ORDER BY scheduled_time DESC LIMIT 1
                """, dep_sched['hex_id'], dep_sched['airport_code'], dep_sched['scheduled_time'])
                if arr_sched and arr_sched['callsign']:
                    return arr_sched['callsign']
    except Exception as e:
        logger.error(f"resolve_watchdog_target error: {e}")
    return clean_cs

async def calculate_watchdog_eta(target_cs: str):
    """Calculate ETA and destination for a flight."""
    import math
    import aiohttp
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            air = await conn.fetchrow("SELECT lat, lon, speed, alt FROM flights_in_air WHERE callsign = $1", target_cs.upper())
            if not air or float(air.get('speed') or 0) <= 0:
                return None, None
            
            dest_code = None
            
            # Try FlightRadar24 + adsbdb (primary)
            try:
                from utils import get_iata_from_icao_fr24
                async with aiohttp.ClientSession() as session:
                    norm = await normalize_callsign(target_cs)
                    iata_code, iata_flight, operator = await get_iata_from_icao_fr24(norm, session)
                    if iata_flight:
                        # Use IATA flight number for adsbdb
                        async with aiohttp.ClientSession() as adsb_session:
                            async with adsb_session.get(f"https://api.adsbdb.com/v0/callsign/{iata_flight}", timeout=3) as r:
                                if r.status == 200:
                                    d = (await r.json()).get("response", {}).get("flightroute", {})
                                    dest_code = resolve_to_icao(d.get("destination", {}).get("icao_code") or d.get("destination", {}).get("iata_code", ""))
                                    logger.info(f"✈️ FR24→adsbdb resolved {norm} ({iata_flight}) dest: {dest_code}")
                                    if d.get("destination", {}).get("latitude"):
                                        dest_lat = float(d["destination"]["latitude"])
                                        dest_lon = float(d["destination"]["longitude"])
                                        c_lat, c_lon = float(air['lat']), float(air['lon'])
                                        c_spd = float(air['speed'])
                                        
                                        # Inline haversine
                                        R = 3440.065
                                        dLat = math.radians(dest_lat - c_lat)
                                        dLon = math.radians(dest_lon - c_lon)
                                        a = math.sin(dLat/2)**2 + math.cos(math.radians(c_lat)) * math.cos(math.radians(dest_lat)) * math.sin(dLon/2)**2
                                        dist_nm = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                                        
                                        if c_spd > 0:
                                            eta_mins = int((dist_nm / c_spd) * 60)
                                            return eta_mins, dest_code
            except Exception as e:
                logger.warning(f"FR24 resolve failed for {target_cs}: {e}")
            
            # Fallback: adsbdb with ICAO callsign
            if not dest_code:
                try:
                    async with aiohttp.ClientSession() as session:
                        norm = await normalize_callsign(target_cs)
                        async with session.get(f"https://api.adsbdb.com/v0/callsign/{norm}", timeout=3) as r:
                            if r.status == 200:
                                d = (await r.json()).get("response", {}).get("flightroute", {})
                                dest_code = resolve_to_icao(d.get("destination", {}).get("icao_code") or d.get("destination", {}).get("iata_code", ""))
                                if d.get("destination", {}).get("latitude"):
                                    dest_lat = float(d["destination"]["latitude"])
                                    dest_lon = float(d["destination"]["longitude"])
                                    c_lat, c_lon = float(air['lat']), float(air['lon'])
                                    c_spd = float(air['speed'])
                                    
                                    # Inline haversine
                                    R = 3440.065
                                    dLat = math.radians(dest_lat - c_lat)
                                    dLon = math.radians(dest_lon - c_lon)
                                    a = math.sin(dLat/2)**2 + math.cos(math.radians(c_lat)) * math.cos(math.radians(dest_lat)) * math.sin(dLon/2)**2
                                    dist_nm = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                                    
                                    if c_spd > 0:
                                        eta_mins = int((dist_nm / c_spd) * 60)
                                        return eta_mins, dest_code
                except: pass
            
            # Fallback to schedule
            sched = await conn.fetchrow("""
                SELECT route_airport, airport_code, direction FROM flight_schedules 
                WHERE (callsign = $1 OR flight_number = $1) 
                AND scheduled_time >= NOW() - INTERVAL '12 hours' AND scheduled_time <= NOW() + INTERVAL '12 hours'
                ORDER BY ABS(EXTRACT(EPOCH FROM (scheduled_time - NOW()))) ASC LIMIT 1
            """, target_cs.upper())
            if sched:
                dest_code = resolve_to_icao(sched['airport_code'] if sched['direction'] == 'ARRIVALS' else sched['route_airport'])
            
            return None, dest_code
    except Exception as e:
        logger.error(f"calculate_watchdog_eta error: {e}")
    return None, None

async def get_watchdog_ground_data(callsign: str):
    """Get ground position data for a flight."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            ground = await conn.fetchrow("""
                SELECT airport, landed_at as timestamp 
                FROM ground_ops 
                WHERE current_callsign = $1 OR inbound_callsign = $1
            """, callsign.upper())
            if ground:
                return {"airport": ground['airport'], "timestamp": ground['timestamp']}
            # Fallback to arrivals_log
            ground = await conn.fetchrow("""
                SELECT airport, timestamp 
                FROM arrivals_log 
                WHERE callsign = $1 
                ORDER BY timestamp DESC LIMIT 1
            """, callsign.upper())
            if ground:
                return {"airport": ground['airport'], "timestamp": ground['timestamp']}
    except Exception as e:
        logger.error(f"get_watchdog_ground_data error: {e}")
    return None