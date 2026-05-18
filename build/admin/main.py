import os
import hashlib
import json
import secrets
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = "https://admin.bharatradar.com/auth/callback"
SESSION_EXPIRE_DAYS = 7

RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"
SECRET_NAME = "aerodatabox-credentials"
K8S_API = "https://kubernetes.default.svc"

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
SA_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

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
    if datetime.now(timezone.utc) > expires:
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


def _read_jsonb(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        return json.loads(raw)
    return []


def _get_k8s_context():
    if not os.path.exists(SA_TOKEN_PATH):
        raise HTTPException(502, "Not running in cluster (no SA token)")
    with open(SA_TOKEN_PATH) as f:
        token = f.read().strip()
    ca = SA_CA_PATH if os.path.exists(SA_CA_PATH) else False
    ns = "bharatradar"
    if os.path.exists(SA_NS_PATH):
        with open(SA_NS_PATH) as f:
            ns = f.read().strip()
    return ns, token, ca


async def _k8s_get_secret(ns, token, ca):
    url = f"{K8S_API}/api/v1/namespaces/{ns}/secrets/{SECRET_NAME}"
    async with httpx.AsyncClient(verify=ca) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.status_code != 200:
            raise HTTPException(502, f"K8s read secret: HTTP {r.status_code}")
        return r.json()


async def _k8s_put_secret(ns, token, ca, body):
    url = f"{K8S_API}/api/v1/namespaces/{ns}/secrets/{SECRET_NAME}"
    async with httpx.AsyncClient(verify=ca) as c:
        r = await c.put(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
        if r.status_code not in (200, 201):
            try:
                detail = r.json().get("message", "")
            except Exception:
                detail = r.text[:200]
            raise HTTPException(502, f"K8s write secret: HTTP {r.status_code} — {detail}")


async def _do_refresh(ns, token, ca):
    secret = await _k8s_get_secret(ns, token, ca)
    raw_data = secret.get("data", {})

    seen = set()
    deduped = {}
    for k in sorted(raw_data, key=lambda x: (0 if x.startswith("rapidapi_key_") else 1, x)):
        v = raw_data[k]
        if k.startswith("rapidapi_key"):
            try:
                decoded = base64.b64decode(v).decode()
                if decoded not in seen:
                    deduped[k] = decoded
                    seen.add(decoded)
            except Exception:
                pass

    if not deduped:
        raise HTTPException(404, "No RapidAPI keys in K8s secret")

    key_results = []
    total_units_limit = 0
    total_units_remaining = 0
    refresh_errors = []

    async with httpx.AsyncClient(verify=True) as c:
        for key_name in sorted(deduped):
            key_value = deduped[key_name]
            key_hash = hashlib.sha256(key_value.encode()).hexdigest()
            entry = {"key_name": key_name, "hash": key_hash, "tier": "unknown", "active": True}

            try:
                hc = await c.get(
                    f"https://{RAPIDAPI_HOST}/health/services/feeds/FlightSchedules",
                    headers={"X-RapidAPI-Key": key_value, "X-RapidAPI-Host": RAPIDAPI_HOST},
                    timeout=10,
                )
                tier = (hc.headers.get("x-tier", "")).lower()
                entry["tier"] = "free" if "free" in tier else (tier if tier else "unknown")

                raw = hc.headers.get("x-ratelimit-api-units-limit")
                if raw:
                    v = int(raw)
                    entry["units_limit"] = v
                    total_units_limit += v

                raw = hc.headers.get("x-ratelimit-api-units-remaining")
                if raw:
                    v = int(raw)
                    entry["units_remaining"] = v
                    total_units_remaining += v

                raw = hc.headers.get("x-ratelimit-api-units-reset")
                if raw:
                    try:
                        secs = int(raw)
                        reset_dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
                        entry["units_reset"] = reset_dt.isoformat()
                        entry["days_until_reset"] = secs // 86400
                    except (ValueError, OSError):
                        entry["units_reset"] = raw

                raw = hc.headers.get("x-ratelimit-requests-limit")
                if raw:
                    entry["requests_limit"] = int(raw)

                raw = hc.headers.get("x-ratelimit-requests-remaining")
                if raw:
                    entry["requests_remaining"] = int(raw)

                if hc.status_code == 401:
                    entry["error"] = "health check 401 (key invalid)"
                    refresh_errors.append(f"{key_name}: health check 401")
                elif hc.status_code not in (200, 403):
                    entry["error"] = f"health check HTTP {hc.status_code}"
                    refresh_errors.append(f"{key_name}: HTTP {hc.status_code}")
            except Exception as e:
                entry["error"] = str(e)
                refresh_errors.append(f"{key_name}: {str(e)[:80]}")

            key_results.append(entry)

    used = total_units_limit - total_units_remaining
    first_hash = key_results[0]["hash"] if key_results else ""

    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE download_config SET
                rapidapi_keys = $1::jsonb,
                rapidapi_key_hash = $2,
                rapidapi_units_used = GREATEST($3, rapidapi_units_used),
                rapidapi_units_limit = $4,
                updated_at = NOW()
            WHERE id = 1
        """, json.dumps(key_results), first_hash, max(used, 0), total_units_limit)

    return {"status": "ok", "keys": key_results, "total_units_limit": total_units_limit,
            "total_units_remaining": total_units_remaining, "errors": refresh_errors}


# ─── Auth ─────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def auth_login():
    params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": REDIRECT_URI,
              "response_type": "code", "scope": "openid email", "access_type": "online"}
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}")


@app.get("/auth/callback")
async def auth_callback(code: str):
    async with httpx.AsyncClient() as client:
        tok = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code, "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
        }, timeout=15)
        if tok.status_code != 200:
            raise HTTPException(401, "OAuth token exchange failed")
        tokens = tok.json()
        user = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=10)
        if user.status_code != 200:
            raise HTTPException(401, "Failed to get user info")
        email = user.json().get("email", "")
    if not email:
        raise HTTPException(401, "No email from Google")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_admin FROM api_users WHERE email = $1", email)
        if not row or not row["is_admin"]:
            raise HTTPException(403, "Access denied")
    token = secrets.token_urlsafe(32)
    sessions[token] = (email, datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRE_DAYS))
    resp = RedirectResponse(url="/admin/")
    resp.set_cookie(key="session", value=token, httponly=True, secure=True,
                    samesite="lax", max_age=SESSION_EXPIRE_DAYS * 86400)
    return resp


@app.get("/auth/logout")
async def auth_logout():
    resp = RedirectResponse(url="/admin/")
    resp.delete_cookie("session")
    return resp


# ─── Pages ────────────────────────────────────────────────────────────

@app.get("/admin/", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(open("/app/templates/admin.html").read())


@app.get("/")
async def root():
    return RedirectResponse(url="/admin/")


# ─── API: Usage & Keys (read-only from DB) ───────────────────────────

@app.get("/admin/api/usage")
async def api_usage(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM download_config WHERE id = 1")
        if not row:
            raise HTTPException(404, "No config found")
        used = row["rapidapi_units_used"] or 0
        limit = row["rapidapi_units_limit"] or 600
        actual = await conn.fetchval("""
            SELECT SUM(units_used)::float / GREATEST(COUNT(DISTINCT date(logged_at)), 1)
            FROM api_usage_log WHERE logged_at >= NOW() - INTERVAL '7 days'
        """)
        burn = round(max(actual or row["rapidapi_daily_burn"] or 280, 1))
        days = row["rapidapi_alert_days"] or 23
        remaining = limit - used
        days_left = remaining / max(burn, 1)
        keys = _read_jsonb(row["rapidapi_keys"])
        return {
            "units_used": used, "units_limit": limit, "remaining": remaining,
            "days_left": round(days_left, 1), "daily_burn": burn, "alert_days": days,
            "last_alert_at": str(row["rapidapi_last_alert_at"] or ""),
            "next_run": str(row["next_run"] or ""), "last_run": str(row["last_run"] or ""),
            "last_status": row["last_status"] or "", "keys": keys,
        }


@app.get("/admin/api/keys")
async def api_list_keys(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT rapidapi_keys FROM download_config WHERE id = 1")
        return {"keys": _read_jsonb(row["rapidapi_keys"] if row else None)}


# ─── API: Refresh ─────────────────────────────────────────────────────

@app.post("/admin/api/plan/refresh")
async def api_plan_refresh(request: Request):
    _require_admin(request)
    ns, token, ca = _get_k8s_context()
    return await _do_refresh(ns, token, ca)


# ─── API: Add Key (writes K8s secret, then refreshes) ─────────────────

@app.post("/admin/api/keys")
async def api_add_key(request: Request):
    _require_admin(request)
    body = await request.json()
    key_value = body.get("key", "").strip()
    if not key_value:
        raise HTTPException(400, "Key value is required")
    user_name = body.get("name", "").strip()

    ns, token, ca = _get_k8s_context()
    secret = await _k8s_get_secret(ns, token, ca)
    raw_data = secret.get("data", {})

    existing_names = {k for k in raw_data if k.startswith("rapidapi_key")}
    i = 1
    while f"rapidapi_key_{i}" in existing_names:
        i += 1
    auto_key_name = f"rapidapi_key_{i}"

    new_data = dict(raw_data)
    encoded = base64.b64encode(key_value.encode()).decode()
    new_data[auto_key_name] = encoded

    if not existing_names:
        new_data["rapidapi_key"] = encoded
        new_data["rapidapi_key_1"] = encoded

    secret["data"] = new_data
    await _k8s_put_secret(ns, token, ca, secret)

    result = await _do_refresh(ns, token, ca)

    if user_name:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                UPDATE download_config SET
                    rapidapi_keys = (
                        SELECT jsonb_agg(
                            CASE WHEN elem->>'key_name' = $1
                                 THEN elem || jsonb_build_object('key_name', $2)
                                 ELSE elem
                            END
                        )
                        FROM jsonb_array_elements(rapidapi_keys) elem
                    )
                WHERE id = 1
                RETURNING rapidapi_keys
            """, auto_key_name, user_name)

    return {"status": "ok", "key_name": user_name or auto_key_name, "refresh": result}


# ─── API: Delete Key (writes K8s secret, then refreshes) ──────────────

@app.delete("/admin/api/keys/{key_name}")
async def api_delete_key(request: Request, key_name: str):
    _require_admin(request)
    if not key_name.startswith("rapidapi_key"):
        raise HTTPException(400, "Invalid key name")

    ns, token, ca = _get_k8s_context()
    secret = await _k8s_get_secret(ns, token, ca)
    raw_data = secret.get("data", {})

    if key_name not in raw_data:
        raise HTTPException(404, f"Key {key_name} not found in secret")

    key_value_b64 = raw_data[key_name]
    key_value_decoded = base64.b64decode(key_value_b64).decode()

    new_data = {k: v for k, v in raw_data.items() if k != key_name}

    if key_name.startswith("rapidapi_key_") and key_name != "rapidapi_key":
        legacy = new_data.get("rapidapi_key")
        if legacy and base64.b64decode(legacy).decode() == key_value_decoded:
            del new_data["rapidapi_key"]

    secret["data"] = new_data
    await _k8s_put_secret(ns, token, ca, secret)

    result = await _do_refresh(ns, token, ca)
    return {"status": "ok", "removed": key_name, "refresh": result}


# ─── API: Usage Log (per-day per-endpoint) ─────────────────────────

@app.get("/admin/api/usage/daily")
async def api_usage_daily(request: Request):
    _require_admin(request)
    days = request.query_params.get("days", "7")
    try:
        days = max(1, min(90, int(days)))
    except ValueError:
        days = 7
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                date(logged_at) AS day,
                endpoint,
                SUM(units_used) AS total_units,
                COUNT(*) AS calls
            FROM api_usage_log
            WHERE logged_at >= NOW() - $1::interval
            GROUP BY day, endpoint
            ORDER BY day DESC, total_units DESC
        """, timedelta(days=days))
        total = await conn.fetchval("SELECT COALESCE(SUM(units_used), 0) FROM api_usage_log")
        return {
            "logs": [dict(r) for r in rows],
            "total_units_all_time": total,
            "days": days,
        }


# ─── API: Schedule ──────────────────────────────────────────────────

@app.get("/admin/api/schedule")
async def api_schedule_get(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT scheduler_enabled, enabled, schedule_time,
                   last_run, last_status, next_run
            FROM download_config WHERE id = 1
        """)
        if not row:
            raise HTTPException(404, "No config")
        return dict(row)


@app.post("/admin/api/schedule")
async def api_schedule_update(request: Request):
    _require_admin(request)
    body = await request.json()
    async with db_pool.acquire() as conn:
        sets = []
        vals = []
        i = 1
        for key in ("scheduler_enabled", "enabled"):
            if key in body:
                sets.append(f"{key} = ${i}")
                vals.append(bool(body[key]))
                i += 1
        if "next_run" in body:
            sets.append("next_run = $%d" % i)
            dt = datetime.fromisoformat(body["next_run"].replace("Z", ""))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            vals.append(dt)
            i += 1
        if sets:
            query = "UPDATE download_config SET " + ", ".join(sets) + " WHERE id = 1"
            await conn.execute(query, *vals)
        row = await conn.fetchrow("""
            SELECT scheduler_enabled, enabled, schedule_time,
                   last_run, last_status, next_run
            FROM download_config WHERE id = 1
        """)
        return dict(row)


# ─── API: Airports ──────────────────────────────────────────────────

@app.get("/admin/api/airports")
async def api_airports_list(request: Request):
    _require_admin(request)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT icao, iata, name, download_schedules
            FROM airports
            ORDER BY icao
        """)
        return {"airports": [dict(r) for r in rows]}


@app.post("/admin/api/airports/{icao}/toggle")
async def api_airport_toggle(request: Request, icao: str):
    _require_admin(request)
    icao = icao.upper()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE airports SET download_schedules = NOT download_schedules WHERE icao = $1 RETURNING icao, iata, name, download_schedules",
            icao,
        )
        if not row:
            raise HTTPException(404, f"Airport {icao} not found")
        return dict(row)


# ─── Misc ─────────────────────────────────────────────────────────────

@app.get("/.well-known/acme-challenge/{token}")
async def acme_challenge(token: str):
    content = os.environ.get(f"ACME_{token}", "")
    if not content:
        raise HTTPException(404, "Challenge token not found")
    from fastapi.responses import Response
    return Response(content=content, media_type="text/plain")


@app.get("/health")
async def health():
    return {"status": "ok"}
