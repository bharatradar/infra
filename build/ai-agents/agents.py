# agents.py
# AI Agents: Forensic Janitor, APOC Watchdog, Daily Analyst
import asyncio
import aiohttp
import asyncpg
import logging
import orjson
import re
from datetime import datetime, timedelta
from config import Config
from db import AsyncDatabaseManager

logger = logging.getLogger(__name__)

class AIOperationsCenter:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.db = AsyncDatabaseManager(db_pool)
        self.last_watchdog_check = datetime.now()
        self.cf_key_index = 0

    async def get_airport_coords(self, ap_code):
        for icao, data in getattr(Config, 'TARGET_AIRPORTS', {}).items():
            if icao == ap_code or data.get('iata') == ap_code:
                return float(data['lat']), float(data['lon'])
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT lat, lon FROM airports WHERE icao = $1 OR iata = $1", ap_code)
                if row and row['lat'] and row['lon']:
                    return float(row['lat']), float(row['lon'])
        except: pass
        return None, None

    async def ask_ai_json(self, system_prompt, user_prompt):
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
- "book ticket" -> {"function":"unsupported","parameters":{}}
- "hello" -> {"function":"unsupported","parameters":{}}
"""
        sys_prompt_full = system_prompt + examples + "\nIMPORTANT: You must ONLY output the JSON function call like the examples above. DO NOT include actual flight data. DO NOT wrap in markdown code blocks."
        
        primary_provider = getattr(Config, 'FAST_ROUTER_PROVIDER', 'groq')
        fallback_hierarchy = getattr(Config, 'FAST_ROUTER_FALLBACK_QUEUE', ['groq', 'cloudflare', 'llama-server', 'local_gguf'])
        
        execution_queue = [primary_provider]
        for p in fallback_hierarchy:
            if p not in execution_queue:
                execution_queue.append(p)

        messages = [
            {"role": "system", "content": sys_prompt_full},
            {"role": "user", "content": user_prompt}
        ]

        async with aiohttp.ClientSession() as session:
            for provider in execution_queue:
                try:
                    if provider == "groq":
                        headers = {
                            "Authorization": f"Bearer {getattr(Config, 'GROQ_API_KEY', '')}",
                            "Content-Type": "application/json"
                        }
                        payload = {
                            "model": getattr(Config, 'FAST_ROUTER_MODEL', 'llama-3.1-8b-instant'),
                            "messages": messages,
                            "temperature": 0.1,
                            "response_format": {"type": "json_object"}
                        }
                        async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=15) as r:
                            r.raise_for_status()
                            data = await r.json()
                            content = data["choices"][0]["message"]["content"].strip()
                            content = re.sub(r'```json\n|\n```', '', content).strip()
                            return orjson.loads(content)

                    elif provider == "cloudflare":
                        cf_keys = getattr(Config, 'CLOUDFLARE_KEYS', [])
                        if cf_keys:
                            creds = cf_keys[self.cf_key_index % len(cf_keys)]
                            self.cf_key_index += 1
                            acc, token = creds['id'], creds['token']
                        else:
                            acc, token = Config.CLOUDFLARE_ACCOUNT_ID, Config.CLOUDFLARE_API_TOKEN
                            
                        api_url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions"
                        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                        payload = {
                            "model": getattr(Config, 'FAST_ROUTER_MODEL', '@cf/meta/llama-3.1-8b-instruct'),
                            "messages": messages,
                            "max_tokens": 512
                        }
                        async with session.post(api_url, headers=headers, json=payload, timeout=15) as r:
                            r.raise_for_status()
                            data = await r.json()
                            content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
                            content = re.sub(r'```json\n|\n```', '', content).strip()
                            return orjson.loads(content)

                except Exception as e:
                    logger.warning(f"⚠️ Provider '{provider}' failed: {e}. Switching to next fallback...")
                    continue
                    
        logger.error("❌ CRITICAL: All AI providers failed.")
        return {}

    async def send_telegram_alert(self, message_html):
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": Config.TELEGRAM_CHAT_ID,
            "text": message_html,
            "parse_mode": "HTML"
        }
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload)
        except: pass

    async def forensic_janitor_loop(self):
        logger.info("🧹 Forensic Janitor AI standing by...")
        processed_hex_ids = set()
        while True:
            await asyncio.sleep(getattr(Config, 'FORENSIC_JANITOR_INTERVAL_SEC', 900))
            processed_hex_ids.clear()
            try:
                async with self.db_pool.acquire() as conn:
                    orphans = await conn.fetch("""
                        SELECT id, hex_id, callsign, airport, timestamp 
                        FROM arrivals_log 
                        WHERE (origin IS NULL OR origin = 'UNK') 
                          AND anomaly_flag IS NULL 
                          AND timestamp >= NOW() - INTERVAL '12 hours'
                        LIMIT 10
                    """)
                
                for r in orphans:
                    hex_id, callsign, arr_ap = r['hex_id'], r['callsign'], r['airport']
                    
                    if hex_id.upper() in processed_hex_ids:
                        logger.info(f"⏭️ Skipping duplicate hex {hex_id}")
                        continue
                    
                    async with self.db_pool.acquire() as conn:
                        recent_enrichment = await conn.fetchrow("""
                            SELECT ai_inferred_value, ai_reasoning, confidence_score 
                            FROM ai_enrichment_audit 
                            WHERE hex_id = $1 AND target_table = 'arrivals_log' 
                              AND timestamp > NOW() - INTERVAL '24 hours'
                            ORDER BY timestamp DESC LIMIT 1
                        """, hex_id.upper())
                    
                    if recent_enrichment:
                        inferred_origin = recent_enrichment['ai_inferred_value']
                        reasoning = f"Reused previous inference: {recent_enrichment['ai_reasoning'][:50]}..."
                        confidence = float(recent_enrichment['confidence_score'])
                        logger.info(f"♻️ Reused enrichment for {callsign}: {inferred_origin}")
                    else:
                        async with self.db_pool.acquire() as conn:
                            history = await conn.fetch("""
                                SELECT event_type, airport, timestamp 
                                FROM flight_events 
                                WHERE hex_id = $1 AND timestamp < $2 
                                ORDER BY timestamp DESC LIMIT 5
                            """, hex_id, r['timestamp'])
                            
                            if not history:
                                history = await conn.fetch("""
                                    SELECT event_type, airport, timestamp 
                                    FROM flight_events 
                                    WHERE LOWER(callsign) = LOWER($1) AND timestamp::date = $2::date AND timestamp < $3
                                    ORDER BY timestamp DESC LIMIT 5
                                """, callsign, r['timestamp'].date(), r['timestamp'])
                            
                            if not history:
                                history = await conn.fetch("""
                                    SELECT 'DEPARTED' as event_type, destination as airport, timestamp 
                                    FROM departures_log 
                                    WHERE LOWER(callsign) = LOWER($1) AND timestamp::date = $2::date AND timestamp < $3
                                    ORDER BY timestamp DESC LIMIT 5
                                """, callsign, r['timestamp'].date(), r['timestamp'])
                            
                            if not history:
                                scheduled = await conn.fetchrow("""
                                    SELECT origin, destination FROM flight_schedules 
                                    WHERE LOWER(callsign) = LOWER($1) AND DATE(scheduled_time) = $2::date
                                    LIMIT 1
                                """, callsign, r['timestamp'].date())
                                if scheduled:
                                    inferred_origin = scheduled['origin']
                                    reasoning = f"Resolved from schedule: {callsign} {scheduled['origin']} → {scheduled['destination']}"
                                    confidence = 0.95
                                    logger.info(f"📋 Resolved {callsign} from schedule: {inferred_origin}")
                                    await self.db.update_arrival_broadcast(
                                        row_id=r['id'], hex_id=hex_id, origin=inferred_origin,
                                        anomaly_flag='AI_ENRICHED', original_value='UNKNOWN',
                                        ai_reasoning=reasoning, confidence_score=confidence, callsign=callsign
                                    )
                                    continue
                        
                        hist_str = ", ".join([f"{h['event_type']} at {h['airport']} ({h['timestamp'].strftime('%H:%M')})" for h in history])
                        
                        sys_prompt = """You are an aviation forensic AI. Your ONLY job is to deduce the origin airport by analyzing radar flight history.
                        Look at the sequence of flight_events: APPROACHING at one airport, then LANDED at another = origin was the approach airport.
                        DEPARTED from airport X before approaching Y = X is likely origin.
                        Output ONLY valid ICAO codes (4 chars). If no clear radar pattern, output null - do NOT guess."""
                        user_prompt = f"Flight {callsign} (Hex: {hex_id}) landed at {arr_ap}. Before this, its radar history was: {hist_str}. Based on standard turnaround procedures and Indian domestic routes, what is the highly probable origin ICAO code? Output JSON format exactly like: {{\"origin_icao\": \"VABB\", \"reasoning\": \"string explaining logic\", \"confidence\": 0.95}}"
                        
                        ai_result = await self.ask_ai_json(sys_prompt, user_prompt)
                        
                        inferred_origin = ai_result.get("origin_icao")
                        reasoning = ai_result.get("reasoning", "Inferred via historical radar pattern matching.")
                        confidence = float(ai_result.get("confidence", 0.0))
                    
                    if inferred_origin and len(inferred_origin) == 4 and confidence > 0.80:
                        logger.info(f"✨ AI Enriched {callsign}: {inferred_origin} (Conf: {confidence})")
                        await self.db.update_arrival_broadcast(
                            row_id=r['id'], 
                            hex_id=hex_id, 
                            origin=inferred_origin, 
                            anomaly_flag='AI_ENRICHED',
                            original_value='UNKNOWN',
                            ai_reasoning=reasoning,
                            confidence_score=confidence,
                            callsign=callsign
                        )
                        processed_hex_ids.add(hex_id.upper())
            except Exception as e:
                logger.error(f"Janitor Error: {e}")

    async def apoc_watchdog_loop(self):
        logger.info("🐕‍🦺 APOC Watchdog AI standing by...")
        while True:
            await asyncio.sleep(getattr(Config, 'WATCHDOG_CHECK_INTERVAL_SEC', 120))
            try:
                now = datetime.now()
                async with self.db_pool.acquire() as conn:
                    anomalies = await conn.fetch("""
                        SELECT id, callsign, airport, event_type, anomaly_flag, details 
                        FROM flight_events 
                        WHERE anomaly_flag IN ('GO_AROUND', 'DIVERSION', 'AIR_RETURN') 
                          AND timestamp > $1
                    """, self.last_watchdog_check)
                
                self.last_watchdog_check = now

                for a in anomalies:
                    ap_code = a['airport']
                    ap_lat, ap_lon = await self.get_airport_coords(ap_code)
                    
                    async with self.db_pool.acquire() as conn:
                        if ap_lat and ap_lon:
                            inbounds = await conn.fetchval("""
                                SELECT COUNT(*) FROM flights_in_air 
                                WHERE lat IS NOT NULL 
                                AND abs(lat - $1) < 1.5 AND abs(lon - $2) < 1.5
                            """, ap_lat, ap_lon)
                            loc_str = f"in the {ap_code} regional airspace"
                        else:
                            inbounds = await conn.fetchval("SELECT COUNT(*) FROM flights_in_air WHERE lat IS NOT NULL")
                            loc_str = "in the tracked airspace"
                    
                    sys_prompt = "You are a Chief Air Traffic Control Analyst. Draft a concise, urgent situational report."
                    user_prompt = f"Flight {a['callsign']} just experienced a {a['anomaly_flag'].replace('_', ' ')} at {ap_code}. Details: {a['details']}. Currently, there are {inbounds} active flights {loc_str}. Write a 2-sentence alert for the airport operations team detailing the event and potential impact. Return JSON: {{\"alert_text\": \"your html formatted text\"}}"
                    
                    ai_result = await self.ask_ai_json(sys_prompt, user_prompt)
                    alert_text = ai_result.get("alert_text")
                    
                    if alert_text:
                        final_msg = f"🚨 <b>WATCHDOG INTELLIGENCE</b>\n\n{alert_text}"
                        await self.send_telegram_alert(final_msg)
                        await self.db.log_ai_insight("WATCHDOG_ALERT", f"{a['anomaly_flag']} by {a['callsign']}", alert_text, ap_code)
                        
            except Exception as e:
                logger.error(f"Watchdog Error: {e}")

    async def daily_analyst_loop(self):
        logger.info("📋 Daily Analyst AI standing by...")
        while True:
            now = datetime.now()
            if now.hour == 23 and now.minute == 55:
                try:
                    logger.info("📊 Generating Daily AI Report...")
                    
                    async with self.db_pool.acquire() as conn:
                        arr_stats = await conn.fetch("""
                            SELECT airport, COUNT(*) as c FROM arrivals_log 
                            WHERE timestamp >= NOW() - INTERVAL '24 hours' 
                            GROUP BY airport ORDER BY c DESC LIMIT 5
                        """)
                        anom_stats = await conn.fetch("""
                            SELECT airport, anomaly_flag, COUNT(*) as c FROM flight_events 
                            WHERE anomaly_flag IS NOT NULL AND anomaly_flag != 'AI_ENRICHED' AND timestamp >= NOW() - INTERVAL '24 hours' 
                            GROUP BY airport, anomaly_flag ORDER BY c DESC LIMIT 5
                        """)
                    
                    arr_str = ", ".join([f"{r['airport']}: {r['c']}" for r in arr_stats]) if arr_stats else "None"
                    anom_str = ", ".join([f"{r['airport']} ({r['anomaly_flag']}): {r['c']}" for r in anom_stats]) if anom_stats else "None"
                    
                    sys_prompt = "You are an Aviation Executive Analyst. Draft a localized daily wrap-up report."
                    user_prompt = f"Write a beautifully formatted Executive Summary of today's operations. Top Airports by Arrivals: {arr_str}. Key Anomalies/Incidents: {anom_str}. Mention that the Raga Engine processed all telemetry. Use emojis. Do not invent data. Output JSON: {{\"report\": \"your html text\"}}"
                    
                    ai_result = await self.ask_ai_json(sys_prompt, user_prompt)
                    report = ai_result.get("report")
                    
                    if report:
                        final_msg = f"📈 <b>DAILY EXECUTIVE BRIEFING</b>\n\n{report}"
                        await self.send_telegram_alert(final_msg)
                        await self.db.log_ai_insight("DAILY_BRIEFING", "End of Day Routine", report, "ALL")
                        
                except Exception as e:
                    logger.error(f"Analyst Error: {e}")
                
                await asyncio.sleep(getattr(Config, 'DAILY_ANALYST_INTERVAL_SEC', 900))
            else:
                await asyncio.sleep(getattr(Config, 'WATCHDOG_CHECK_INTERVAL_SEC', 120))

async def run_agents():
    db_pool = await asyncpg.create_pool(**Config.DB_PARAMS)
    center = AIOperationsCenter(db_pool)
    
    await asyncio.gather(
        center.forensic_janitor_loop(),
        center.apoc_watchdog_loop(),
        center.daily_analyst_loop()
    )

if __name__ == "__main__":
    try:
        asyncio.run(run_agents())
    except KeyboardInterrupt:
        pass