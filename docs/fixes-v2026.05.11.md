# Fixes & Changelog — v2026.05.11

All fixes are committed to `main`, tagged `v2026.05.11.15`, and pushed to GHCR as `:latest`.

---

## 1. Security: PUBLIC_ROUTES wildcard bypass

**Files:** `build/cortex-webapp/web_app.py:44`

**Bug:** `"/"` was the first entry in `PUBLIC_ROUTES`. The auth middleware checks `path.startswith(public_route)` — since every path starts with `"/"`, **all routes bypassed authentication**. Any unauthenticated user could access any endpoint.

**Fix:** Removed `"/"` from `PUBLIC_ROUTES`. Explicit whitelist only.

---

## 2. Dashboard Data API Endpoints returning 401

**File:** `build/cortex-webapp/web_app.py:63-71`

**Bug:** The radar/ATC/ops/exec/delay/drilldown/telemetry API endpoints were not in `PUBLIC_ROUTES`. Unauthenticated users got 401 when viewing the public dashboard. Only `/api/atc/live` was whitelisted.

**Fix:** Added to `PUBLIC_ROUTES`:
- `/api/aircraft/`
- `/api/atc/`
- `/api/ops/`
- `/api/exec/`
- `/api/delay/`
- `/api/drilldown/`
- `/api/telemetry/`
- `/api/config`

---

## 3. WebSocket REST polling bypass in switchTab()

**File:** `build/cortex-webapp/static/js/app.js:300-304`

**Bug:** `switchTab('atc')` unconditionally called `fetchATC()` and started a 5-second REST polling timer (`tabTimers.atc`) every time the user switched to the ATC tab. This completely bypassed the WebSocket guard in `startFetchATC()` (line 1927) which correctly skips REST polling when WS is enabled. That guard only runs on page load — tab switching bypasses it.

Result: Every tab switch to ATC triggered immediate REST call to `/api/aircraft/radar` + 5s polling, even when WebSocket was connected.

**Fix:** Conditionalized the timer:
```javascript
if (!(FRONTEND_CONFIG.ws_use_for_atc && FRONTEND_CONFIG.ws_enabled)) {
    tabTimers.atc = setInterval(fetchATC, ...);
}
```

The one-time `fetchATC()` call on tab switch still fires (may use REST if `wsFlightsData` is empty), but the polling spam stops.

---

## 4. Schedule-downloader: Redis port crash from K8s env injection

**File:** `build/schedule-downloader/config.py:369` (standalone code)

**Bug:** `REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))` crashed because Kubernetes injects `REDIS_PORT=tcp://10.43.96.10:6379` (service DNS format). `int("tcp://10.43.96.10:6379")` raises `ValueError`.

**Fix:** Added `_parse_port()` helper that handles `tcp://host:port` and `redis://host:port` formats. Also applied same fix to `Config.REDIS_PARAMS` and `Config.DB_PARAMS` within the class.

---

## 5. my.bharatradar.com 500 error: Wrong ADSBLOL_REDIS_HOST placement

**File:** `manifests/default/api/base/api.yaml` (note: later fixed via deployment)

**Bug:** `ADSBLOL_REDIS_HOST=redis://:password@127.0.0.1:6379` was set on the **nginx sidecar container** instead of the **api container**. The api container still pointed to `redis://127.0.0.1:6379` (no password).

**Fix:** Moved `ADSBLOL_REDIS_HOST` to the api container env. Removed wrong env vars (`MY_DOMAIN`, etc.) from nginx container.

---

## 6. Telegram-bot CrashLoopBackOff: unresolved env vars

**File:** `manifests/default/telegram-bot.yaml` (deployment manifest)

**Bug:** `DB_HOST` and `REDIS_HOST` were set to literal string `SHARED_SERVICES_HOST` — the templating system was supposed to replace this but the file wasn't in the patching list.

**Fix:** Set explicit values `DB_HOST=45.88.189.38`, `REDIS_HOST=redis` in deployment. Bot now stable.

---

## 7. In-cluster Redis missing password

**File:** In-cluster Redis deployment (applied via manifest)

**Bug:** In-cluster Redis service had no `--requirepass` configured. Host Redis (`45.88.189.38:6379`) had a password but in-cluster Redis (`redis:6379`) didn't. Bots and services connecting to `redis:6379` without password would fail when the code expected auth.

**Fix:** Added `--requirepass` matching `redis-credentials` K8s secret password. Both Redis instances now share the same password.

---

## 8. schedule-downloader: missing req dependencies

**File:** `build/schedule-downloader/requirements.txt`

**Bug:** Multiple missing Python packages caused schedule-downloader to crash on startup.

**Fix:** All dependencies added. Image rebuilt as `:latest`.

---

## 9. Schedule-downloader FR24 API returning 403

**External issue (not code):** FR24 API returning HTTP 403. The schedule-downloader code is correct; this is an upstream API restriction that needs investigation.

---

## 10. cortex-webapp nginx container had wrong env vars

**File:** `manifests/default/cortex-webapp/default/deployment.yaml`

**Bug:** The nginx sidecar container had `MY_DOMAIN=my.bharatradar.com` and other api-specific env vars that don't apply to nginx. `ADSBLOL_REDIS_HOST` was on the nginx container instead of the api container.

**Fix:** Cleaned up env var placement — only the api container gets api-related env vars. Nginx container has minimal config.

---

## 11. Public routes had duplicate entries

**File:** `build/cortex-webapp/web_app.py:44-81`

**Bug:** PUBLIC_ROUTES had many duplicate entries (e.g., `/login` 3 times, `/auth/` twice, `/static/` twice, `/favicon.ico` twice) — remnants of the `/command_center` prefix migration.

**Fix:** Deduplicated entries. Each route appears once.

---

## 12. binCraft decoder lat/lon swap

**File:** `build/cortex-webapp/web_app.py:985-987`, `web_app_db.py:179-183,209-213`

**Bug:** The binCraft decoder swaps latitude and longitude values in some aircraft records. India's latitude range is ~6-37°N (always below 60) and longitude ~68-98°E (always above 60 for most of India, but can be below 60 in some regions).

**Fix:** If `abs(lat) > 60` and `abs(lon) < 60`, swap them back. Applied in both the Redis-based radar endpoint and the PostgreSQL-based flight query.

---

## Summary

| # | Issue | Type | File |
|---|-------|------|------|
| 1 | `"/"` in PUBLIC_ROUTES bypasses all auth | Security | `web_app.py:44` |
| 2 | Dashboard API endpoints return 401 | Auth | `web_app.py:63-71` |
| 3 | switchTab() unconditional REST polling | WS | `app.js:300-304` |
| 4 | REDIS_PORT crash from K8s env injection | Crash | `config.py:369` |
| 5 | Wrong container for ADSBLOL_REDIS_HOST | Config | `deployment.yaml` |
| 6 | Telegram-bot unresolved SHARED_SERVICES_HOST | Config | `telegram-bot.yaml` |
| 7 | In-cluster Redis missing password | Config | Redis manifest |
| 8 | schedule-downloader missing deps | Build | `requirements.txt` |
| 9 | FR24 API 403 | External | (API issue) |
| 10 | Wrong env vars on nginx container | Config | `deployment.yaml` |
| 11 | PUBLIC_ROUTES duplicate entries | Cleanup | `web_app.py:44-81` |
| 12 | binCraft lat/lon swap | Data | `web_app.py:985-987` |
