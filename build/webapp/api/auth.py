import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional
import asyncpg
from jose import jwt
from config import Config

SECRET_KEY = os.getenv("JWT_SECRET", secrets.token_hex(32))
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24


async def get_db_pool():
    """Get database pool for API operations."""
    return await asyncpg.create_pool(
        host=Config.DB_PARAMS["host"],
        port=int(Config.DB_PARAMS["port"]),
        user=Config.DB_PARAMS["user"],
        password=Config.DB_PARAMS["password"],
        database=Config.DB_PARAMS["database"]
    )


def create_jwt_token(user_id: int, email: str, tier: str) -> str:
    """Create JWT token for authenticated user."""
    payload = {
        "user_id": user_id,
        "email": email,
        "tier": tier,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_jwt_token(token: str) -> Optional[dict]:
    """Verify JWT token and return payload."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def generate_api_key() -> str:
    """Generate random API key."""
    return f"br_live_{secrets.token_hex(24)}"


def hash_api_key(api_key: str) -> str:
    """Hash API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def get_tier_limits(tier: str) -> dict:
    """Get daily limits for tier."""
    limits = {
        "free": 100,
        "bronze": 1000,
        "silver": 10000,
        "gold": 999999999
    }
    return {"daily_limit": limits.get(tier, 100)}


async def create_or_update_user(email: str, name: str, google_id: str, db_pool) -> dict:
    """Create or update user from Google OAuth."""
    # Check if user exists
    user = await db_pool.fetchrow(
        "SELECT * FROM api_users WHERE email = $1 OR google_id = $2",
        email, google_id
    )
    
    if user:
        # Update existing user
        await db_pool.execute(
            "UPDATE api_users SET name = $1, google_id = $2, last_login = NOW() WHERE id = $3",
            name, google_id, user['id']
        )
        return {
            "id": user['id'],
            "email": user['email'],
            "name": name,
            "tier": user['tier']
        }
    else:
        # Create new user
        user_id = await db_pool.fetchval(
            "INSERT INTO api_users (email, name, google_id, tier) VALUES ($1, $2, $3, 'free') RETURNING id",
            email, name, google_id
        )
        return {
            "id": user_id,
            "email": email,
            "name": name,
            "tier": "free"
        }


async def generate_user_api_key(user_id: int, description: str, db_pool) -> str:
    """Generate and store API key for user."""
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    tier_limits = get_tier_limits("free")  # Default to free tier
    
    await db_pool.execute(
        """INSERT INTO api_keys (key_hash, user_id, description, daily_limit) 
           VALUES ($1, $2, $3, $4)""",
        key_hash, user_id, description, tier_limits["daily_limit"]
    )
    
    return api_key


async def validate_api_key(api_key: str, db_pool) -> Optional[dict]:
    """Validate API key and check rate limits."""
    key_hash = hash_api_key(api_key)
    
    key_data = await db_pool.fetchrow(
        """SELECT ak.*, au.email, au.name, au.tier 
           FROM api_keys ak 
           JOIN api_users au ON ak.user_id = au.id 
           WHERE ak.key_hash = $1 AND ak.is_active = TRUE""",
        key_hash
    )
    
    if not key_data:
        return None
    
    # Check if daily limit reset needed
    last_reset = key_data['last_reset']
    now = datetime.utcnow()
    
    if last_reset.date() < now.date():
        # Reset daily counter
        await db_pool.execute(
            "UPDATE api_keys SET requests_today = 0, last_reset = NOW() WHERE id = $1",
            key_data['id']
        )
        key_data['requests_today'] = 0
        key_data['last_reset'] = now
    
    # Check rate limit
    if key_data['requests_today'] >= key_data['daily_limit']:
        return {"error": "rate_limit_exceeded", "limit": key_data['daily_limit']}
    
    # Increment usage
    await db_pool.execute(
        "UPDATE api_keys SET requests_today = requests_today + 1 WHERE id = $1",
        key_data['id']
    )
    
    return {
        "user_id": key_data['user_id'],
        "email": key_data['email'],
        "tier": key_data['tier'],
        "key_id": key_data['id']
    }


async def get_user_info(user_id: int, db_pool) -> Optional[dict]:
    """Get user information."""
    return await db_pool.fetchrow(
        "SELECT id, email, name, tier, created_at, last_login FROM api_users WHERE id = $1",
        user_id
    )


async def get_user_api_keys(user_id: int, db_pool) -> list:
    """Get all API keys for user."""
    return await db_pool.fetch(
        """SELECT id, description, is_active, created_at, expires_at, daily_limit, requests_today 
           FROM api_keys WHERE user_id = $1 ORDER BY created_at DESC""",
        user_id
    )


async def revoke_api_key(key_id: int, user_id: int, db_pool) -> bool:
    """Revoke an API key."""
    result = await db_pool.execute(
        "UPDATE api_keys SET is_active = FALSE WHERE id = $1 AND user_id = $2",
        key_id, user_id
    )
    return result == "UPDATE 1"