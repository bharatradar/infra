import os
import json
import time
import hashlib
import urllib.parse
from datetime import datetime
from typing import Optional
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiohttp
from dotenv import load_dotenv
load_dotenv('.env')

from config import Config
import api.auth as auth

app = FastAPI(
    title="Bharat Radar API",
    version="1.0.0",
    description="REST API for Indian flight tracking data with free tier",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting storage (use Redis in production)
rate_limits = defaultdict(list)
REQUEST_WINDOW = 60  # 60 seconds window

# Google OAuth URLs
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Tier daily limits
TIER_LIMITS = {
    "free": 5,        # 5 requests per minute
    "bronze": 100,    # 100 requests per minute
    "silver": 500,    # 500 requests per minute
    "gold": 2000,     # 2000 requests per minute
    "platinum": 10000 # 10000 requests per minute
}

# Daily limits (reset at midnight)
TIER_DAILY_LIMITS = {
    "free": 100,
    "bronze": 1000,
    "silver": 10000,
    "gold": 50000,
    "platinum": 999999999
}

# Valid API keys (store in DB in production)
API_KEYS = {
    "bharat_free_test": {"tier": "free", "name": "Test Key"},
}

# Rate limiter
async def check_rate_limit(api_key: str, tier: str):
    now = time.time()
    limit = TIER_LIMITS.get(tier, 100)
    
    # Clean old entries
    rate_limits[api_key] = [t for t in rate_limits[api_key] if now - t < REQUEST_WINDOW]
    
    if len(rate_limits[api_key]) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Upgrade tier at https://bharat-radar.vellur.in/api/register"
        )
    
    rate_limits[api_key].append(now)

# API Key dependency
async def verify_api_key(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required. Get free key at https://bharat-radar.vellur.in/api/register")
    
    key_data = API_KEYS.get(x_api_key)
    if not key_data:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    await check_rate_limit(x_api_key, key_data["tier"])
    return key_data


class UserResponse(BaseModel):
    email: str
    name: str
    tier: str


class ApiKeyResponse(BaseModel):
    api_key: str
    description: str
    daily_limit: int


@app.get("/")
async def root():
    return {"message": "Bharat Radar API", "version": "1.0.0", "docs": "/docs"}

@app.get("/test-nginx")
async def test_nginx():
    return {"status": "nginx works"}


@app.get("/auth/google")
async def google_login():
    """Redirect to Google OAuth."""
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent"
    }
    
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def google_callback(code: str):
    """Handle Google OAuth callback."""
    # Exchange code for tokens
    async with aiohttp.ClientSession() as session:
        # Get tokens
        token_data = {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI")
        }
        
        async with session.post(GOOGLE_TOKEN_URL, data=token_data) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to get tokens")
            tokens = await resp.json()
        
        # Get user info
        async with session.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        ) as resp:
            userinfo = await resp.json()
    
    # Create or update user in database
    db_pool = await auth.get_db_pool()
    try:
        user = await auth.create_or_update_user(
            email=userinfo["email"],
            name=userinfo.get("name", ""),
            google_id=userinfo["id"],
            db_pool=db_pool
        )
        
        # Create JWT token
        jwt_token = auth.create_jwt_token(user["id"], user["email"], user["tier"])
        
        # Return user info and token
        return {
            "user": user,
            "token": jwt_token,
            "message": "Login successful! Generate API key at /auth/api-key"
        }
    finally:
        await db_pool.close()


@app.post("/auth/api-key")
async def create_api_key(
    description: str = Query("Default API Key"),
    authorization: str = Header(None)
):
    """Generate API key for authenticated user."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.replace("Bearer ", "")
    payload = auth.verify_jwt_token(token)
    
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    db_pool = await auth.get_db_pool()
    try:
        api_key = await auth.generate_user_api_key(payload["user_id"], description, db_pool)
        
        return {
            "api_key": api_key,
            "message": "Save this API key - it won't be shown again!",
            "tier": payload["tier"],
            "daily_limit": TIER_LIMITS.get(payload["tier"], 100)
        }
    finally:
        await db_pool.close()


@app.get("/auth/me")
async def get_current_user(authorization: str = Header(None)):
    """Get current user info."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.replace("Bearer ", "")
    payload = auth.verify_jwt_token(token)
    
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    db_pool = await auth.get_db_pool()
    try:
        user = await auth.get_user_info(payload["user_id"], db_pool)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        keys = await auth.get_user_api_keys(payload["user_id"], db_pool)
        
        return {
            "user": dict(user),
            "api_keys": [dict(k) for k in keys]
        }
    finally:
        await db_pool.close()


@app.get("/api/v1/airports")
async def get_airports(
    country: str = Query(None, description="Filter by country code"),
    limit: int = Query(50, ge=1, le=100)
):
    """Get airport list (Free tier endpoint)."""
    # This would fetch from database
    return {
        "airports": [
            {"icao": "VABB", "iata": "BOM", "name": "Chhatrapati Shivaji International Airport", "city": "Mumbai"},
            {"icao": "VICG", "iata": "DEL", "name": "Indira Gandhi International Airport", "city": "Delhi"},
        ],
        "count": 2
    }


@app.get("/api/v1/flights/search")
async def search_flights(
    callsign: str = Query(None),
    origin: str = Query(None),
    destination: str = Query(None)
):
    """Search flights (Free tier endpoint)."""
    return {
        "flights": [],
        "message": "Flight search implementation pending database integration"
    }


@app.get("/api/v1/positions")
async def get_positions(
    airport: str = Query(None),
    limit: int = Query(100, ge=1, le=500)
):
    """Get current aircraft positions (Free tier endpoint)."""
    return {
        "positions": [],
        "message": "Positions API - requires database integration"
    }


@app.get("/api/v1/health")
async def health_check():
    """API health check."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# ============================
# API Key Management
# ============================

@app.post("/api/v1/keys/register")
async def register_api_key(
    email: str = Query(...),
    name: str = Query(...),
    tier: str = Query("free")
):
    """Register for a free API key."""
    if tier not in TIER_LIMITS:
        tier = "free"
    
    # Generate API key
    api_key = f"br_{hashlib.sha256(f'{email}{time.time()}'.encode()).hexdigest()[:32]}"
    API_KEYS[api_key] = {"tier": tier, "name": name, "email": email}
    
    return {
        "api_key": api_key,
        "tier": tier,
        "daily_limit": TIER_LIMITS[tier],
        "instructions": "Use header 'X-API-Key: YOUR_KEY' for all requests"
    }


@app.get("/api/v1/keys/usage")
async def get_api_usage(x_api_key: str = Header(...)):
    """Get current API usage."""
    key_data = API_KEYS.get(x_api_key)
    if not key_data:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    now = time.time()
    requests_used = len([t for t in rate_limits.get(x_api_key, []) if now - t < REQUEST_WINDOW])
    tier = key_data["tier"]
    
    return {
        "tier": tier,
        "requests_per_minute_limit": TIER_LIMITS.get(tier, 5),
        "daily_limit": TIER_DAILY_LIMITS.get(tier, 100),
        "requests_this_minute": requests_used,
        "remaining_this_minute": TIER_LIMITS.get(tier, 5) - requests_used
    }


@app.get("/api/v1/rate-limits")
async def get_rate_limits():
    """Get rate limits for all tiers."""
    return {
        "tiers": {
            tier: {
                "requests_per_minute": TIER_LIMITS[tier],
                "requests_per_day": TIER_DAILY_LIMITS[tier],
                "access": "All endpoints" if tier != "free" else "Basic endpoints only"
            }
            for tier in TIER_LIMITS
        },
        "current_limits": {
            "free": {"requests_per_minute": 5, "requests_per_day": 100},
            "description": "Free tier: 5 requests/min, 100 requests/day. Upgrade for higher limits."
        },
        "docs_url": "/docs",
        "register_url": "/api/v1/keys/register"
    }


# ============================
# Protected API Endpoints
# ============================

@app.get("/api/v1/airports")
async def get_airports(
    x_api_key: str = Header(...),
    country: str = Query(None, description="Filter by country code (e.g., IN)"),
    limit: int = Query(50, ge=1, le=100)
):
    """Get airport list (requires API key)."""
    key_data = await verify_api_key(x_api_key)
    
    # Fetch from database
    db_pool = await auth.get_db_pool()
    try:
        async with db_pool.acquire() as conn:
            query = "SELECT icao, iata, name, city, country FROM airports"
            params = []
            if country:
                query += " WHERE country = $1"
                params.append(country.upper())
            query += f" LIMIT {limit}"
            rows = await conn.fetch(query, *params)
            return {"airports": [dict(r) for r in rows], "count": len(rows), "tier": key_data["tier"]}
    finally:
        await db_pool.close()


@app.get("/api/v1/flights/search")
async def search_flights(
    x_api_key: str = Header(...),
    callsign: str = Query(None),
    origin: str = Query(None),
    destination: str = Query(None),
    limit: int = Query(50, ge=1, le=100)
):
    """Search flights (requires API key)."""
    key_data = await verify_api_key(x_api_key)
    
    db_pool = await auth.get_db_pool()
    try:
        async with db_pool.acquire() as conn:
            where = "WHERE 1=1"
            params = []
            if callsign:
                params.append(callsign.upper())
                where += f" AND callsign LIKE ${len(params)}"
            if origin:
                params.append(origin.upper())
                where += f" AND origin = ${len(params)}"
            if destination:
                params.append(destination.upper())
                where += f" AND destination = ${len(params)}"
            
            query = f"""
                SELECT hex_id, callsign, origin, destination, airport, timestamp 
                FROM arrivals_log {where} 
                UNION ALL
                SELECT hex_id, callsign, origin, destination, airport, timestamp 
                FROM departures_log {where.replace('origin', 'destination').replace('destination', 'origin')}
                ORDER BY timestamp DESC LIMIT {limit}
            """
            rows = await conn.fetch(query, *params)
            return {"flights": [dict(r) for r in rows], "count": len(rows), "tier": key_data["tier"]}
    finally:
        await db_pool.close()


@app.get("/api/v1/positions")
async def get_positions(
    x_api_key: str = Header(...),
    airport: str = Query(None),
    limit: int = Query(100, ge=1, le=500)
):
    """Get current aircraft positions (requires API key)."""
    key_data = await verify_api_key(x_api_key)
    
    db_pool = await auth.get_db_pool()
    try:
        async with db_pool.acquire() as conn:
            query = """
                SELECT hexid, callsign, lat, lon, alt, speed, heading, last_seen 
                FROM flights_in_air 
                WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
            if airport:
                # Filter by proximity to airport (simplified)
                query += f" LIMIT {limit}"
            else:
                query += f" LIMIT {limit}"
            
            rows = await conn.fetch(query)
            return {"positions": [dict(r) for r in rows], "count": len(rows), "tier": key_data["tier"]}
    finally:
        await db_pool.close()


# ============================
# Documentation Endpoint
# ============================

@app.get("/api/v1/docs")
async def api_docs():
    """Get API documentation."""
    return {
        "title": "Bharat Radar API Documentation",
        "version": "1.0.0",
        "tiers": {
            "free": {"daily_limit": 100, "endpoints": ["airports", "flights/search", "positions"]},
            "bronze": {"daily_limit": 1000, "endpoints": ["all"]},
            "silver": {"daily_limit": 10000, "endpoints": ["all"]},
            "gold": {"daily_limit": 999999999, "endpoints": ["all"]}
        },
        "getting_started": {
            "step1": "Register at POST /api/v1/keys/register?email=your@email.com&name=YourName",
            "step2": "Use the returned API key in header: X-API-Key: YOUR_KEY",
            "step3": "Make requests to /api/v1/* endpoints"
        },
        "swagger_ui": "/docs",
        "redoc_ui": "/redoc"
    }


if __name__ == "__main__":
    import uvicorn
    import urllib.parse
    uvicorn.run(app, host="0.0.0.0", port=8000)