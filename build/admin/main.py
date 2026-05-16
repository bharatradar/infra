import os
import hashlib
import secrets
import urllib.parse
from datetime import datetime, timedelta

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = "https://admin.bharatradar.com/auth/callback"
SESSION_EXPIRE_DAYS = 7

db_pool = None
sessions = {}

app = FastAPI()


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        database=os.environ.get("DB_NAME", "flight_db"),
        user=os.environ.get("DB_USER", "flight_db_user"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


def _get_session_email(token: str):
    entry = sessions.get(token)
    if not entry:
        return None
    email, expires = entry
    if datetime.utcnow() > expires:
        del sessions[token]
        return None
    return email


def _require_admin(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated")
    email = _get_session_email(token)
    if not email:
        raise HTTPException(401, "Session expired")
    return email


@app.get("/auth/login")
async def auth_login():
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email",
        "access_type": "online",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(code: str, response: Response):
    async with httpx.AsyncClient() as client:
        tok_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        if tok_resp.status_code != 200:
            raise HTTPException(401, "OAuth token exchange failed")
        tokens = tok_resp.json()

        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=10,
        )
        if user_resp.status_code != 200:
            raise HTTPException(401, "Failed to get user info")
        user_info = user_resp.json()
        email = user_info.get("email", "")

    if not email:
        raise HTTPException(401, "No email from Google")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_admin FROM api_users WHERE email = $1", email
        )
        if not row or not row["is_admin"]:
            raise HTTPException(403, "Access denied — not an admin user")

    token = secrets.token_urlsafe(32)
    sessions[token] = (email, datetime.utcnow() + timedelta(days=SESSION_EXPIRE_DAYS))
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_EXPIRE_DAYS * 86400,
    )
    return RedirectResponse(url="/admin/")


@app.get("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("session")
    return RedirectResponse(url="/admin/")


@app.get("/admin/", response_class=HTMLResponse)
async def admin_page():
    html = open("/app/templates/admin.html").read()
    return HTMLResponse(html)


class SettingsUpdate(BaseModel):
    units_limit: int | None = None
    daily_burn: int | None = None
    alert_days: int | None = None


@app.get("/admin/api/usage")
async def api_usage(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM download_config WHERE id = 1")
        if not row:
            raise HTTPException(404, "No config found")
        used = row["rapidapi_units_used"] or 0
        limit = row["rapidapi_units_limit"] or 600
        burn = row["rapidapi_daily_burn"] or 280
        days = row["rapidapi_alert_days"] or 23
        remaining = limit - used
        days_left = remaining / max(burn, 1)
        return {
            "units_used": used,
            "units_limit": limit,
            "daily_burn": burn,
            "alert_days": days,
            "remaining": remaining,
            "days_left": round(days_left, 1),
            "last_alert_at": str(row["rapidapi_last_alert_at"] or ""),
            "next_run": str(row["next_run"] or ""),
            "last_run": str(row["last_run"] or ""),
            "last_status": row["last_status"] or "",
        }


@app.put("/admin/api/settings")
async def api_settings(request: Request, body: SettingsUpdate):
    _require_admin(request)
    sets = []
    vals = {}
    if body.units_limit is not None:
        sets.append("rapidapi_units_limit = :limit")
        vals["limit"] = body.units_limit
    if body.daily_burn is not None:
        sets.append("rapidapi_daily_burn = :burn")
        vals["burn"] = body.daily_burn
    if body.alert_days is not None:
        sets.append("rapidapi_alert_days = :days")
        vals["days"] = body.alert_days
    if not sets:
        raise HTTPException(400, "No fields to update")
    sets.append("updated_at = NOW()")
    sql = f"UPDATE download_config SET {', '.join(sets)} WHERE id = 1"
    async with db_pool.acquire() as conn:
        await conn.execute(sql, *vals.values())
    return {"status": "ok"}


@app.post("/admin/api/reset-usage")
async def api_reset_usage(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE download_config SET rapidapi_units_used = 0, "
            "rapidapi_last_alert_at = NULL, updated_at = NOW() WHERE id = 1"
        )
    return {"status": "ok"}


class KeyUpdate(BaseModel):
    key: str


@app.post("/admin/api/key")
async def api_update_key(request: Request, body: KeyUpdate):
    _require_admin(request)
    new_key = body.key.strip()
    if not new_key:
        raise HTTPException(400, "Key cannot be empty")
    key_hash = hashlib.sha256(new_key.encode()).hexdigest()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE download_config SET rapidapi_key_hash = $1, "
            "rapidapi_units_used = 0, rapidapi_last_alert_at = NULL, "
            "updated_at = NOW() WHERE id = 1",
            key_hash,
        )
    return {"status": "ok", "message": "Key hash stored. Update the K8s secret separately."}


@app.get("/health")
async def health():
    return {"status": "ok"}
