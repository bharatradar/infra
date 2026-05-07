#!/usr/bin/env python3
"""Patch web_app.py for cortex-webapp integration"""

import re

# Read the file
with open('/Users/Shared/bharatradar/infra/build/cortex-webapp/web_app.py', 'r') as f:
    content = f.read()

# 1. Update PUBLIC_ROUTES - remove /command_center prefix references
content = content.replace('/command_center/login', '/login')
content = content.replace('/command_center/logout', '/logout')
content = content.replace('/command_center/auth/', '/auth/')
content = content.replace('/command_center/static/', '/static/')
content = content.replace('/command_center/api/', '/api/')
content = content.replace('/command_center/dashboard', '/dashboard')
content = content.replace('/command_center/docs', '/docs')
content = content.replace('/command_center/redoc', '/redoc')
content = content.replace('/command_center/openapi.json', '/openapi.json')

# 2. Update redirect URLs in auth functions
content = content.replace('url="/command_center/dashboard"', 'url="/dashboard"')
content = content.replace('"/command_center/login?auth=error"', '"/login?auth=error"')

# 3. Replace auth_google_root_proxy and auth_callback_root_proxy with direct OAuth
# Find and replace the proxy functions
old_auth_google = '''@app.get("/auth/google")
async def auth_google_root_proxy():
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{Config.API_INTERNAL_URL}/auth/google", follow_redirects=False)
        return RedirectResponse(response.headers.get("location", "/"), status_code=response.status_code)'''

new_auth_google = '''@app.get("/auth/google")
async def auth_google():
    import os
    import urllib.parse
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)'''

content = content.replace(old_auth_google, new_auth_google)

# 4. Replace auth_callback_root_proxy
old_auth_callback = '''@app.get("/auth/callback")
async def auth_callback_root_proxy(code: str):
    import httpx
    from datetime import datetime, timedelta
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{Config.API_INTERNAL_URL}/auth/callback?code={code}", follow_redirects=False)
            if response.status_code == 200:
                # Get user info from response
                try:
                    user_data = response.json()
                    email = user_data.get('user', {}).get('email') or user_data.get('email', 'authenticated')
                    session_value = f"google_{email}_{datetime.now().timestamp()}"
                except Exception as e:
                    logger.error(f"Auth callback JSON parse error: {e}")
                    session_value = f"authenticated_{datetime.now().timestamp()}"

                # Set session cookie and redirect to dashboard
                redirect = RedirectResponse(url="/dashboard", status_code=302)
                redirect.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=session_value,
                    max_age=AUTH_COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                    path="/",
                    domain=".bharatradar.com"
                )
                return redirect
            else:
                return RedirectResponse("/login?auth=error", status_code=302)
    except Exception as e:
        logger.error(f"Auth callback error: {e}")
        return RedirectResponse("/login?auth=error", status_code=302)'''

new_auth_callback = '''@app.get("/auth/callback")
async def auth_callback(code: str):
    import os
    import aiohttp
    from datetime import datetime
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    
    try:
        async with aiohttp.ClientSession() as session:
            token_data = {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback")
            }
            
            async with session.post(GOOGLE_TOKEN_URL, data=token_data) as resp:
                if resp.status != 200:
                    return RedirectResponse("/login?auth=error", status_code=302)
                
                tokens = await resp.json()
                access_token = tokens.get("access_token")
                
                if not access_token:
                    return RedirectResponse("/login?auth=error", status_code=302)
                
                async with session.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}) as user_resp:
                    if user_resp.status == 200:
                        user_data = await user_resp.json()
                        email = user_data.get("email", "authenticated")
                        name = user_data.get("name", email.split('@')[0])
                    else:
                        email = "authenticated"
                        name = "User"
                
                session_value = f"google_{email}_{datetime.now().timestamp()}"
                redirect = RedirectResponse(url="/dashboard", status_code=302)
                redirect.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=session_value,
                    max_age=AUTH_COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                    path="/",
                    domain="cortex.bharatradar.com"
                )
                
                # Auto-create user in api_users
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO api_users (email, name, tier, google_id) 
                            VALUES ($1, $2, 'free', 'google') 
                            ON CONFLICT (email) DO NOTHING
                        """, email, name)
                        logger.info(f"[AUTH] User created/verified: {email}")
                except Exception as ue:
                    logger.error(f"[AUTH] Auto-create user error: {ue}")
                
                return redirect
    except Exception as e:
        logger.error(f"Auth callback error: {e}")
        return RedirectResponse("/login?auth=error", status_code=302)'''

content = content.replace(old_auth_callback, new_auth_callback)

# 5. Replace auth_google_proxy and auth_callback_proxy (command_center versions)
old_cmd_auth_google = '''@app.get("/auth/google")
async def auth_google():
    import os
    import urllib.parse
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def auth_callback(code: str):'''

# These are already replaced above, now remove the duplicate command_center versions
# The command_center versions should be removed entirely since we're using root paths
content = re.sub(
    r'@app\.get\("/auth/google"\)\nasync def auth_google\(\):.*?return RedirectResponse\(auth_url\)',
    '''@app.get("/auth/google")
async def auth_google():
    import os
    import urllib.parse
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "https://cortex.bharatradar.com/auth/callback"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)''',
    content,
    flags=re.DOTALL
)

# Write the file
with open('/Users/Shared/bharatradar/infra/build/cortex-webapp/web_app.py', 'w') as f:
    f.write(content)

print("web_app.py patched successfully")
