import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command, or_f
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.session.aiohttp import AiohttpSession
from config import Config

import bot_router_mcp_client
import core_ai_pipeline
import asyncpg

# Import for direct DB queries
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Helper function to send formatted messages with fallback
async def safe_reply(message: types.Message, text: str, parse_mode=None):
    """Safely reply to a message with fallback parsing"""
    try:
        await message.reply(text, parse_mode=parse_mode or "HTML")
    except TelegramBadRequest:
        if parse_mode:  # Try without parse_mode if HTML failed
            await message.reply(text, parse_mode=None)
        else:
            raise  # Re-raise if both attempts failed
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        raise

# Initialize bot and dispatcher
custom_session = AiohttpSession(timeout=120.0)
bot = Bot(token=Config.TELEGRAM_TOKEN, session=custom_session)
dp = Dispatcher()

# Help text constant to avoid duplication
HELP_TEXT = """
🛠️ <b>Raga Radar Advanced - Master Command List</b>

<b>1. ✈️ Live Flight Tracking & Concierge</b>
• <i>"Where is my flight IGO123?"</i>
• <i>"Status of QP1312"</i>

<b>2. 🗓️ Airport Timetables & Boards</b>
• <i>"Show me the departures board for Pune"</i>
• <i>"Afternoon arrivals in Delhi"</i>
• <i>"What flights are leaving Mumbai tonight?"</i>
• <i>"Give me the next 5"</i> (Follow-up)

<b>3. 🗺️ Route & Approaching Radar</b>
• <i>"What is approaching Pune right now?"</i>
• <i>"Status of all flights from Chennai to Pune today"</i>

<b>4. 🚨 Proactive Alerts</b>
• <i>"Notify me when IGO123 lands"</i>
• <i>"Alert me when AIC456 is 30 mins away from landing"</i>

<b>5. 📊 Turnarounds, Ground Ops & Anomalies</b>
• <i>"Average turnaround time by airline at BOM"</i>
• <i>"Show me planes currently parked at Pune"</i>
• <i>"Any go-arounds or diversions in Delhi?"</i>

<b>6. ⚙️ Deep Intel & System</b>
• <i>"History of hex 800bc4"</i>
• <i>"Airspace pulse"</i>

🧹 <b>Type /clear to wipe the AI memory if it gets confused.</b>
"""

# 🌟 NEW: Expansive, fully-detailed Help Menu
@dp.message(or_f(Command("help"), F.text.lower() == "help", F.text.lower() == r"\help"))
async def send_help(m: types.Message):
    await safe_reply(m, HELP_TEXT)

@dp.message(CommandStart())
async def start(m: types.Message):
    mode_text = "Fast Router" if getattr(Config, 'BOT_ENGINE_MODE', 'zeroclaw') == 'fast_router' else "ZeroClaw Agent"
    welcome_text = f"✈️ <b>Raga Radar Advanced</b> ({mode_text} Mode)\nSend /help to see everything you can ask me!"
    await safe_reply(m, welcome_text)

@dp.message(Command("clear"))
async def clear_memory(m: types.Message):
    uid = str(m.from_user.id)
    core_ai_pipeline.clear_user_memory(uid)
    await safe_reply(m, "🧹 <b>Chat memory cleared!</b> The AI has a fresh brain and won't rely on past history.")

# ============================================================
# 🌟 NEW: Direct Command Handlers (Bypass LLM for speed)
# ============================================================

async def _resolve_airport(text: str) -> str:
    """Resolve airport code from text (city name or ICAO/IATA)."""
    text = text.strip().upper()
    # Direct ICAO code
    if len(text) == 4 and text.startswith('V'):
        return text
    # Check city map from router
    router = bot_router_mcp_client.ROUTER
    if text.lower() in router.city_map:
        return router.city_map[text.lower()]
    # Check TARGET_AIRPORTS
    for icao, data in Config.TARGET_AIRPORTS.items():
        if data.get('iata', '').upper() == text:
            return icao
        if data.get('city', '').upper() == text:
            return icao
        if data.get('name', '').upper() == text:
            return icao
    return text  # Return as-is if no match

async def _format_flight_list(flights: list, title: str) -> str:
    """Format a list of flights for Telegram display."""
    if not flights:
        return f"✈️ <b>{title}</b>\n\nNo flights found."
    
    lines = [f"✈️ <b>{title}</b>\n"]
    for i, f in enumerate(flights[:15], 1):  # Limit to 15
        callsign = f.get('callsign', 'N/A')
        origin = f.get('origin', f.get('route_airport', ''))
        dest = f.get('destination', f.get('airport', ''))
        alt = f.get('altitude', f.get('alt', 0))
        speed = f.get('ground_speed', f.get('speed', 0))
        status = f.get('status', 'Unknown')
        
        lines.append(f"{i}. <b>{callsign}</b> | {origin}→{dest}")
        lines.append(f"   Alt: {alt}ft | Spd: {speed}kts | {status}")
        lines.append("")
    
    if len(flights) > 15:
        lines.append(f"<i>... and {len(flights) - 15} more flights</i>")
    
    return "\n".join(lines)

@dp.message(Command("flights"))
async def cmd_flights(m: types.Message):
    """Show live flights at an airport. Usage: /flights DEL"""
    parts = m.text.split()
    if len(parts) < 2:
        await safe_reply(m, "✈️ <b>Usage:</b> <code>/flights &lt;airport&gt;</code>\nExample: <code>/flights DEL</code> or <code>/flights Delhi</code>")
        return
    
    airport = await _resolve_airport(parts[1])
    
    # Use typing indicator
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        # Call MCP tool directly for speed
        result = await bot_router_mcp_client.execute_tool_via_mcp(
            "get_airport_traffic", {"code": airport}
        )
        await safe_reply(m, result)
    except Exception as e:
        logger.error(f"cmd_flights error: {e}")
        await safe_reply(m, f"⚠️ Error fetching flights: {str(e)}")

@dp.message(Command("status"))
async def cmd_status(m: types.Message):
    """Get flight status. Usage: /status IGO123"""
    parts = m.text.split()
    if len(parts) < 2:
        await safe_reply(m, "📡 <b>Usage:</b> <code>/status &lt;flight&gt;</code>\nExample: <code>/status IGO123</code> or <code>/status AIC416</code>")
        return
    
    callsign = await bot_router_mcp_client.normalize_callsign(parts[1])
    
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        result = await bot_router_mcp_client.execute_tool_via_mcp(
            "get_flight_status", {"callsign_raw": callsign}
        )
        await safe_reply(m, result)
    except Exception as e:
        logger.error(f"cmd_status error: {e}")
        await safe_reply(m, f"⚠️ Error fetching status: {str(e)}")

@dp.message(Command("route"))
async def cmd_route(m: types.Message):
    """Show flights on a route. Usage: /route DEL BOM"""
    parts = m.text.split()
    if len(parts) < 3:
        await safe_reply(m, "🗺️ <b>Usage:</b> <code>/route &lt;origin&gt; &lt;dest&gt;</code>\nExample: <code>/route DEL BOM</code> or <code>/route Delhi Mumbai</code>")
        return
    
    origin = await _resolve_airport(parts[1])
    dest = await _resolve_airport(parts[2])
    
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        result = await bot_router_mcp_client.execute_tool_via_mcp(
            "get_route_status_board", {"origin": origin, "destination": dest}
        )
        await safe_reply(m, result)
    except Exception as e:
        logger.error(f"cmd_route error: {e}")
        await safe_reply(m, f"⚠️ Error fetching route: {str(e)}")

@dp.message(Command("delayed"))
async def cmd_delayed(m: types.Message):
    """Show delayed flights. Usage: /delayed DEL or /delayed 6E"""
    parts = m.text.split()
    if len(parts) < 2:
        await safe_reply(m, "⏱️ <b>Usage:</b> <code>/delayed &lt;airport|airline&gt;</code>\nExamples:\n<code>/delayed DEL</code> - Delayed flights at Delhi\n<code>/delayed 6E</code> - Delayed IndiGo flights")
        return
    
    query = parts[1].upper()
    
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        # Check if it's an airline code (2-3 chars) or airport
        if len(query) <= 3 and not query.startswith('V'):
            # Assume airline code - query anomalies by airline
            # For now, use airport anomalies as fallback
            # TODO: Add airline-specific delay query when B1 is done
            result = await bot_router_mcp_client.execute_tool_via_mcp(
                "get_airport_anomalies", {"airport_code": "ALL"}
            )
            # Add note about airline filter
            result = f"⏱️ <b>Delayed Flights - Airline {query}</b>\n\n<i>Note: Airline-specific delay filtering coming soon with B1 (Real Delay Calculation)</i>\n\n{result}"
        else:
            # Airport query
            airport = await _resolve_airport(query)
            result = await bot_router_mcp_client.execute_tool_via_mcp(
                "get_airport_anomalies", {"airport_code": airport}
            )
        
        await safe_reply(m, result)
    except Exception as e:
        logger.error(f"cmd_delayed error: {e}")
        await safe_reply(m, f"⚠️ Error fetching delays: {str(e)}")

@dp.message(Command("board"))
async def cmd_board(m: types.Message):
    """Show airport board (arrivals/departures). Usage: /board DEL arrivals"""
    parts = m.text.split()
    if len(parts) < 2:
        await safe_reply(m, "🛫 <b>Usage:</b> <code>/board &lt;airport&gt; [arrivals|departures]</code>\nExample: <code>/board DEL arrivals</code>")
        return
    
    airport = await _resolve_airport(parts[1])
    board_type = "ARRIVALS" if len(parts) < 3 or parts[2].lower() in ['arr', 'arrivals'] else "DEPARTURES"
    
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        result = await bot_router_mcp_client.execute_tool_via_mcp(
            "get_unified_airport_timetable", 
            {"airport": airport, "board_type": board_type, "time_modifier": "all"}
        )
        await safe_reply(m, result)
    except Exception as e:
        logger.error(f"cmd_board error: {e}")
        await safe_reply(m, f"⚠️ Error fetching board: {str(e)}")

# Update help text to include new commands
HELP_TEXT = """
🛠️ <b>Raga Radar Advanced - Master Command List</b>

<b>📱 Quick Commands</b>
• <code>/flights DEL</code> - Live flights at Delhi
• <code>/status IGO123</code> - Flight status
• <code>/route DEL BOM</code> - Flights on route
• <code>/delayed DEL</code> - Delayed flights at airport
• <code>/board DEL arrivals</code> - Airport timetable

<b>1. ✈️ Live Flight Tracking & Concierge</b>
• <i>"Where is my flight IGO123?"</i>
• <i>"Status of QP1312"</i>

<b>2. 🗓️ Airport Timetables & Boards</b>
• <i>"Show me the departures board for Pune"</i>
• <i>"Afternoon arrivals in Delhi"</i>
• <i>"What flights are leaving Mumbai tonight?"</i>
• <i>"Give me the next 5"</i> (Follow-up)

<b>3. 🗺️ Route & Approaching Radar</b>
• <i>"What is approaching Pune right now?"</i>
• <i>"Status of all flights from Chennai to Pune today"</i>

<b>4. 🚨 Proactive Alerts</b>
• <i>"Notify me when IGO123 lands"</i>
• <i>"Alert me when AIC456 is 30 mins away from landing"</i>

<b>5. 📊 Turnarounds, Ground Ops & Anomalies</b>
• <i>"Average turnaround time by airline at BOM"</i>
• <i>"Show me planes currently parked at Pune"</i>
• <i>"Any go-arounds or diversions in Delhi?"</i>

<b>6. ⚙️ Deep Intel & System</b>
• <i>"History of hex 800bc4"</i>
• <i>"Airspace pulse"</i>

🧹 <b>Type /clear to wipe the AI memory if it gets confused.</b>
"""

@dp.message()
async def query(m: types.Message):
    uid = str(m.from_user.id)
    text_msg = m.text.strip()

    async def typing_heartbeat():
        max_iterations = 60  # Max 4 minutes (60 * 4s)
        try:
            for _ in range(max_iterations):
                await bot.send_chat_action(m.chat.id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    heartbeat_task = asyncio.create_task(typing_heartbeat())

    try:
        # 🧠 Pass everything directly to the Central Core Engine!
        logger.info(f"text_msg = {text_msg}")
        ans = await core_ai_pipeline.process_chat_message(uid, text_msg)
        logger.info(f"ans = {ans[:100] if ans else 'None'}...")

        if ans:
            await safe_reply(m, ans)

    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

async def main():
    # Initialize dependencies
    await bot_router_mcp_client.init_client_state()
    bot_router_mcp_client.load_airlines_bot()
    
    mode = "Fast Router" if getattr(Config, 'BOT_ENGINE_MODE', 'zeroclaw') == 'fast_router' else "ZeroClaw Agent"
    logger.info(f"🚀 Interactive Telegram Bot Interface Started ({mode} Engine connected via Core)")
    
    try: 
        while True:
            try: 
                await dp.start_polling(bot)
            except Exception as e:
                logger.error(f"⚠️ Telegram Polling connection lost: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
    finally:
        await bot_router_mcp_client.close_client_state()

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")