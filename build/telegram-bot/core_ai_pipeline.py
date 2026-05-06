import asyncio
import logging
import re
from collections import OrderedDict
from config import Config
import bot_router_mcp_client

logger = logging.getLogger(__name__)

# LRU cache for user memory with automatic cleanup to prevent memory leaks
# Using OrderedDict to implement LRU (Least Recently Used) cache
USER_MEMORY = OrderedDict()
MAX_MEMORY_ENTRIES = 1000  # Maximum number of users to keep in memory
MAX_MESSAGES_PER_USER = 3  # Maximum messages to store per user

def clear_user_memory(uid: str):
    """Utility to clear memory for a specific user/session."""
    if uid in USER_MEMORY:
        del USER_MEMORY[uid]

def _cleanup_memory():
    """Remove oldest entries when memory limit is exceeded."""
    while len(USER_MEMORY) > MAX_MEMORY_ENTRIES:
        USER_MEMORY.popitem(last=False)  # Remove oldest item

async def process_chat_message(uid: str, text_msg: str) -> str:
    """The central brain. Processes text from Telegram OR the Web Dashboard."""
    
    # Set the ContextVar for alerts (Parse to int if it's a telegram ID, otherwise 0 for web users)
    bot_router_mcp_client.CURRENT_CHAT_ID.set(int(uid) if str(uid).isdigit() else 0)
    bot_router_mcp_client.CURRENT_SESSION_ID.set(uid if not str(uid).isdigit() else "")
    
    text_msg = text_msg.strip()
    
    # 🌟 NATIVE COMMAND INTERCEPTORS
    text_lower = text_msg.lower()
    if text_lower in ["/help", "help", "\\help"]:
        return """
🛠️ <b>Raga Radar Advanced - Master Command List</b>

<b>1. ✈️ Live Flight Tracking & Concierge</b>
• <i>"Where is my flight IGO123?"</i>
• <i>"Status of QP1312"</i>

<b>2. 🗓️ Airport Timetables & Boards</b>
• <i>"Show me the departures board for Pune"</i>
• <i>"Afternoon arrivals in Delhi"</i>

<b>3. 🗺️ Route & Approaching Radar</b>
• <i>"What is approaching Pune right now?"</i>
• <i>"Status of all flights from Chennai to Pune today"</i>

<b>4. 🚨 Proactive Alerts</b>
• <i>"Notify me when IGO123 is 20 mins away"</i>
• <i>"Alert me when AIC456 lands"</i>

<b>5. 📊 Turnarounds & Ground Ops</b>
• <i>"Show me planes currently parked at Pune"</i>

🧹 <b>Type /clear to wipe the memory.</b>
"""

    if text_lower in ["/clear", "clear", "\\clear"]:
        clear_user_memory(uid)
        return "🧹 <b>Chat memory cleared!</b>"

    try:
        # ==========================================
        # ⚡ THE NEW HYBRID SEMANTIC ROUTER PATH
        # ==========================================
        
        logger.info(f"⚡ FAST PATH: Routing query: '{text_msg}'")
        
        # 1. Provide conversation context for partial queries
        if uid not in USER_MEMORY: 
            USER_MEMORY[uid] = []
            _cleanup_memory()  # Cleanup if needed when adding new user
            
        # Move user to end (most recently used) if already exists
        elif uid in USER_MEMORY:
            USER_MEMORY.move_to_end(uid)
            
        # If the query is very short (like just a city name "Pune" or a time "evening")
        # Prepend the previous request to give the Regex router context
        context_query = text_msg
        if len(text_msg.split()) <= 2 and len(USER_MEMORY[uid]) > 0:
            last_msg = USER_MEMORY[uid][-1]
            # Don't prepend bot errors or questions
            if not any(word in last_msg.lower() for word in ["error", "need more info", "which airport"]):
                 context_query = f"{last_msg} {text_msg}"
                 logger.info(f"🧠 Context applied: '{context_query}'")
        logger.info(f"🧠 Context query: '{context_query}'")
        # 2. Execute the Master Routing Tool
        ans = await bot_router_mcp_client.FUNCTION_DISPATCHER["smart_route_free_text"].ainvoke(context_query)
        
        # 3. Store in memory for context (if it wasn't an error or prompt for more info)
        if "Need more info" not in str(ans) and "❓" not in str(ans):
            # Keep memory lean (last N messages per user)
            if len(USER_MEMORY[uid]) >= MAX_MESSAGES_PER_USER:
                USER_MEMORY[uid].pop(0)  # Remove oldest message
            USER_MEMORY[uid].append(text_msg)
            
        if ans:
            ans = str(ans).replace("<br>", "\n").replace("</br>", "\n")
            ans = re.sub(r'</?status>', '', ans)
            return ans
             
        return "No response generated."
    
    except Exception as e:
        logger.error(f"Error processing chat message: {e}")
        return f"⚠️ System Error: {e}"