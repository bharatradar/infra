# user_alert_watchdog.py
# User ETA Alert Watchdog - monitors user alerts and sends notifications
import asyncio
import logging
import json
import math
import re
import orjson
from datetime import datetime
import asyncpg
import aiohttp
import redis.asyncio as redis
from aiogram import Bot

from config import Config

try:
    from pywebpush import webpush
except ImportError:
    webpush = None

logger = logging.getLogger(__name__)
DB_POOL = None
REDIS_POOL = None

_WEB_PUSH_ENABLED = getattr(Config, 'ENABLE_WEB_NOTIFICATIONS', False)
_WEB_PUSH_AVAILABLE = webpush is not None

async def get_db_pool():
    global DB_POOL, REDIS_POOL
    if DB_POOL is None:
        DB_POOL = await asyncpg.create_pool(**Config.DB_PARAMS)
    if REDIS_POOL is None:
        try:
            REDIS_POOL = redis.from_url(getattr(Config, 'REDIS_URL', 'redis://localhost:6379/0'))
        except Exception as e:
            logger.warning(f"Redis not available: {e}")
    return DB_POOL

async def send_web_push(sub_data, message_text):
    if not _WEB_PUSH_ENABLED:
        return False
    if not _WEB_PUSH_AVAILABLE:
        logger.warning("⚠️ Web push not available")
        return False
    
    vapid_key = getattr(Config, 'VAPID_PRIVATE_KEY', 'REPLACE_ME')
    if "REPLACE_ME" in vapid_key:
        logger.warning("⚠️ VAPID keys not configured")
        return False
    
    try:
        sub_info = json.loads(sub_data) if isinstance(sub_data, str) else sub_data
        clean_text = message_text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        payload = json.dumps({
            "title": "✈️ BharatRadar Alert",
            "body": clean_text,
            "icon": "https://cdn-icons-png.flaticon.com/512/785/785116.png"
        })
        await asyncio.to_thread(
            webpush,
            subscription_info=sub_info,
            data=payload,
            vapid_private_key=vapid_key,
            vapid_claims=getattr(Config, 'VAPID_CLAIMS', {"sub": "mailto:raghavan@vellur.in"})
        )
        return True
    except Exception as e:
        logger.error(f"❌ Web Push Failed: {e}")
        return False

async def normalize_callsign(cs):
    if not cs:
        return ""
    cs = str(cs).strip().upper()
    cs = re.sub(r'[^A-Z0-9]', '', cs)
    return cs

def resolve_to_icao(code):
    if not code:
        return None
    code = str(code).strip().upper()
    if len(code) == 3:
        for icao, iata in getattr(Config, 'ICAO_TO_IATA', {}).items():
            if iata == code:
                return icao
    return code

async def get_active_alerts():
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
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE user_alerts SET status = $1 WHERE id = $2", status, alert_id)
    except Exception as e:
        logger.error(f"update_alert_status error: {e}")

async def resolve_watchdog_target(clean_cs: str):
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
    try:
        pool = await get_db_pool()
        air = None
        if REDIS_POOL:
            try:
                flights_data = await REDIS_POOL.hgetall(getattr(Config, 'REDIS_LIVE_FLIGHTS_KEY', 'live_flights'))
                for data_json in flights_data.values():
                    fl = orjson.loads(data_json)
                    if (fl.get('callsign') or '').upper() == target_cs.upper():
                        speed = fl.get('speed')
                        if speed and float(speed) > 0:
                            air = fl
                        break
            except:
                pass
        if air is None:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT lat, lon, speed, alt FROM flights_in_air WHERE callsign = $1", target_cs.upper())
                if row and float(row.get('speed') or 0) > 0:
                    air = row
        if air is None:
            return None, None
            
            dest_code = None
            
            # Try adsbdb
            try:
                norm = await normalize_callsign(target_cs)
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://api.adsbdb.com/v0/callsign/{norm}", timeout=3) as r:
                        if r.status == 200:
                            d = (await r.json()).get("response", {}).get("flightroute", {})
                            dest_code = resolve_to_icao(d.get("destination", {}).get("icao_code") or d.get("destination", {}).get("iata_code", ""))
                            if d.get("destination", {}).get("latitude"):
                                dest_lat = float(d["destination"]["latitude"])
                                dest_lon = float(d["destination"]["longitude"])
                                c_lat, c_lon = float(air['lat']), float(air['lon'])
                                c_spd = float(air['speed'])
                                
                                R = 3440.065
                                dLat = math.radians(dest_lat - c_lat)
                                dLon = math.radians(dest_lon - c_lon)
                                a = math.sin(dLat/2)**2 + math.cos(math.radians(c_lat)) * math.cos(math.radians(dest_lat)) * math.sin(dLon/2)**2
                                dist_nm = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                                
                                if c_spd > 0:
                                    eta_mins = int((dist_nm / c_spd) * 60)
                                    return eta_mins, dest_code
            except Exception as e:
                logger.error(f"calculate_watchdog_eta adsbdb error: {e}")
            
            return None, dest_code
    except Exception as e:
        logger.error(f"calculate_watchdog_eta error: {e}")
    return None, None

async def get_watchdog_ground_data(callsign: str):
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

async def alert_watchdog(bot: Bot):
    logger.info("🛑 Starting User Alert Watchdog Service")
    check_interval = getattr(Config, 'ETA_ALERT_CHECK_INTERVAL_SEC', 60)
    
    while True:
        try:
            alerts = await get_active_alerts()
            logger.info(f"Checking {len(alerts)} active alerts...")
            
            for a in alerts:
                alert_id = a['id']
                clean_cs = a['target_callsign']
                alert_type = a['alert_type']
                threshold_mins = a['threshold_mins']
                chat_id = a['chat_id']
                
                logger.info(f"Checking {alert_type} for {clean_cs} (threshold: {threshold_mins}m)")
                
                target_cs = clean_cs
                
                if alert_type in ('CONNECTING_LANDED', 'CONNECTING_ETA'):
                    target_cs = await resolve_watchdog_target(clean_cs)
                
                msg_to_send = None
                should_close = False
                
                eta, dest = await calculate_watchdog_eta(target_cs)
                ground = await get_watchdog_ground_data(target_cs)
                
                if ground and ground.get('timestamp'):
                    ts = ground['timestamp']
                    diff = abs((datetime.now() - ts).total_seconds())
                    timeout = getattr(Config, 'ASSUMED_LANDING_TIMEOUT_SEC', 300)
                    if diff < timeout:
                        msg_to_send = f"🛬 <b>Arrival Alert: {clean_cs} has LANDED!</b>\nIt touched down at {ground['airport']}."
                        should_close = True
                
                if not msg_to_send and eta is not None and eta <= threshold_mins:
                    msg_to_send = f"⏳ <b>ETA Alert: {clean_cs} is approaching!</b>\nIt is approximately {eta} minutes away from landing at {dest or 'destination'}."
                    should_close = True
                
                if msg_to_send:
                    dispatch_success = False
                    
                    if chat_id == 0 and a.get('sub_data'):
                        logger.info(f"🌐 Web Push for {clean_cs}...")
                        dispatch_success = await send_web_push(a['sub_data'], msg_to_send)
                    elif chat_id > 0:
                        logger.info(f"📱 Telegram Alert for {clean_cs}...")
                        try:
                            await bot.send_message(chat_id=chat_id, text=msg_to_send, parse_mode="HTML")
                            dispatch_success = True
                        except Exception as e:
                            logger.error(f"Telegram send failed: {e}")
                    
                    status = 'COMPLETED' if dispatch_success else 'FAILED'
                    await update_alert_status(alert_id, status)
                    logger.info(f"✅ Alert {alert_id} for {clean_cs}: {status}")
                    
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        
        await asyncio.sleep(check_interval)

async def main():
    await get_db_pool()
    
    bot = Bot(token=Config.TELEGRAM_TOKEN)
    logger.info("🚀 User Alert Watchdog Service Started")
    
    try:
        await alert_watchdog(bot)
    except asyncio.CancelledError:
        pass
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())