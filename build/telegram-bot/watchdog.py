#!/usr/bin/env python3
"""
BharatRadar Alert Watchdog Service
Monitors user alerts and sends Telegram/web notifications when flights approach landing.
Runs as a separate process from the bot polling.
"""
import asyncio
import logging
import json
from datetime import datetime
import asyncpg
import aiohttp
import urllib.request
import ssl
import math
from aiogram import Bot

from config import Config
import bot_router_mcp_client

try:
    from pywebpush import webpush
except ImportError:
    webpush = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_WEB_PUSH_ENABLED = getattr(Config, 'ENABLE_WEB_NOTIFICATIONS', False)
_WEB_PUSH_AVAILABLE = webpush is not None

async def send_web_push(sub_data, message_text):
    if not _WEB_PUSH_ENABLED:
        return False
    
    if not _WEB_PUSH_AVAILABLE:
        logger.warning("⚠️ Web push not available - pywebpush not installed")
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

async def alert_watchdog(bot: Bot):
    logger.info("🛑 Starting Alert Watchdog Service")
    check_interval = getattr(Config, 'ETA_ALERT_CHECK_INTERVAL_SEC', 60)
    
    while True:
        try:
            alerts = await bot_router_mcp_client.get_active_alerts()
            logger.info(f"Checking {len(alerts)} active alerts...")
            
            for a in alerts:
                alert_id = a['id']
                clean_cs = a['target_callsign']
                alert_type = a['alert_type']
                threshold_mins = a['threshold_mins']
                chat_id = a['chat_id']
                
                logger.info(f"Checking {alert_type} for {clean_cs} (threshold: {threshold_mins}m)")
                
                target_cs = clean_cs
                
                # Resolve connecting flight if needed
                if alert_type in ('CONNECTING_LANDED', 'CONNECTING_ETA'):
                    target_cs = await bot_router_mcp_client.resolve_watchdog_target(clean_cs)
                
                msg_to_send = None
                should_close = False
                
                # 1. Get current ETA and destination
                eta, dest = await bot_router_mcp_client.calculate_watchdog_eta(target_cs)
                
                # 2. Get ground position (landing)
                ground = await bot_router_mcp_client.get_watchdog_ground_data(target_cs)
                
                # 3. Check landing state first
                if ground and ground.get('timestamp'):
                    ts = ground['timestamp']
                    diff = abs((datetime.now() - ts).total_seconds())
                    timeout = getattr(Config, 'ASSUMED_LANDING_TIMEOUT_SEC', 300)
                    if diff < timeout:
                        msg_to_send = f"🛬 <b>Arrival Alert: {clean_cs} has LANDED!</b>\nIt touched down at {ground['airport']}."
                        should_close = True
                
                # 4. Check ETA state
                if not msg_to_send and eta is not None and eta <= threshold_mins:
                    msg_to_send = f"⏳ <b>ETA Alert: {clean_cs} is approaching!</b>\nIt is approximately {eta} minutes away from landing at {dest or 'destination'}."
                    should_close = True
                
                # 5. Dispatch notification
                if msg_to_send:
                    dispatch_success = False
                    
                    # Web push or Telegram
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
                    
                    # Update alert status
                    status = 'COMPLETED' if dispatch_success else 'FAILED'
                    await bot_router_mcp_client.update_alert_status(alert_id, status)
                    logger.info(f"✅ Alert {alert_id} for {clean_cs}: {status}")
                    
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        
        await asyncio.sleep(check_interval)

async def main():
    await bot_router_mcp_client.init_client_state()
    bot_router_mcp_client.load_airlines_bot()
    
    bot = Bot(token=Config.TELEGRAM_TOKEN)
    logger.info("🚀 Watchdog Service Started")
    
    try:
        await alert_watchdog(bot)
    except asyncio.CancelledError:
        pass
    finally:
        await bot.session.close()
        await bot_router_mcp_client.close_client_state()

if __name__ == "__main__":
    asyncio.run(main())