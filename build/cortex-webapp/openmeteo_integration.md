# Open-Meteo Weather Integration

Integrates free, open-source weather data from [Open-Meteo](https://open-meteo.com/) into the BharatRadar Command Center for airport weather display, delay prediction enhancement, LLM/bot weather queries, and proactive alerts.

**Free tier:** 10,000 API calls/day, no API key required, no registration.

---

## Features

### 1. Weather Data Service

**File:** `weather_service.py`

Core service that fetches weather from Open-Meteo and caches in Redis.

| Endpoint | Cache Key | TTL | Frequency |
|----------|-----------|-----|-----------|
| Current conditions | `weather:current:{icao}` | 15 min | ~96 calls/airport/day |
| 48h hourly forecast | `weather:forecast:{icao}` | 1 hour | ~24 calls/airport/day |
| Past days (92d) | `weather:historical:{icao}` | 6 hours | ~4 calls/airport/day |

**Batch optimization:** Multiple airports queried in a single Open-Meteo call via comma-separated lat/lon.

**Weather variables fetched:**
- Current: temperature, humidity, weather_code, wind speed/dir/gusts, pressure, visibility, low cloud cover
- Hourly: temperature, precipitation, weather_code, visibility, wind, pressure (48h)
- Daily: max temp, precipitation sum, max wind

### 2. API Routes

**File:** `web_app.py`

| Route | Description |
|-------|-------------|
| `GET /api/weather/{icao}` | Current + 48h forecast for one airport |
| `GET /api/weather/batch?icaos=VABB,VIDP,VOBL` | Batch weather for multiple airports |

### 3. Frontend Weather Widget

**Files:** `dashboard.html`, `app.js`

- Weather card appears in ATC tab when an airport is selected from the filter dropdown
- Also shown in the radar popup (origin/destination weather when clicking a flight)
- Displays: temperature, condition icon, wind speed/direction, visibility, pressure, 3h forecast strip
- Uses the existing OpenLayers map and Tailwind CSS styling

### 4. Delay Prediction Enhancement

**File:** `delay_predictor.py`

Weather factors added to the rule-based delay model:

| Condition | Delay Impact |
|-----------|-------------|
| Visibility < 1 km | +20 min |
| Visibility 1-3 km | +10 min |
| Visibility 3-5 km | +5 min |
| Wind gusts > 35 kn | +15 min |
| Wind gusts > 25 kn | +8 min |
| Wind gusts > 20 kn | +4 min |
| Rain > 5 mm/h | +10 min |
| Rain > 2 mm/h | +5 min |
| Snowfall > 0 cm | +20 min |
| Thunderstorm (WMO 95-99) | +25 min |
| Low cloud cover > 80% | +8 min |
| Low cloud cover > 60% | +4 min |

Weather data served from Redis cache — no extra API cost per prediction.

### 5. LLM/Bot Weather Queries

**File:** `bot_router_mcp_client.py`

**New tool:**
- `get_airport_weather(icao)` — returns current weather + forecast for LLM context

**Enhanced existing:**
- `get_delay_prediction` includes weather breakdown in the response
- Semantic router handles queries like "weather at Mumbai", "is it foggy in Delhi", "how will weather affect flights"

### 6. Proactive Weather Alerts

**Backend:** Background task runs every 15 min, checks weather thresholds for all `TARGET_AIRPORTS`, publishes alerts to Redis + WebSocket.

**Alert Thresholds:**

| Alert Type | Trigger | Severity |
|------------|---------|----------|
| Low visibility | Vis < 3 km | 🟡 ≥1km / 🟠 <1km |
| Strong winds | Gusts > 25 kn | 🟠 ≥30kn / 🔴 ≥40kn |
| Thunderstorm | WMO 95-99 | 🔴 |
| Heavy rain | Precip > 10 mm/h | 🟠 |
| Snowfall | Snow > 0 cm | 🔴 |
| Extreme heat | Temp > 42°C | 🟡 |

**Storage:** `weather:alerts` sorted set in Redis (score = severity level, member = ICAO code).

**WebSocket:** Broadcast alert updates to connected clients (reuses existing `/ws` broadcast loop).

**Frontend:** Alert banner at top of ATC tab — clickable to expand alert details.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Browser (dashboard.html)               │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ ATC Map  │  │ Weather      │  │ Alert Banner         │  │
│  │ (OL)     │  │ Widget       │  │                      │  │
│  └────┬─────┘  └──────┬───────┘  └─────────┬────────────┘  │
│       │               │                     │               │
│       ▼               ▼                     ▼               │
│    fetchATC()    fetchWeather()      WebSocket msg          │
└───────┼───────────────┬─────────────────────┬───────────────┘
        │               │                     │
┌───────▼───────────────▼─────────────────────▼───────────────┐
│                   API Server (web_app.py)                   │
│  ┌────────┐  ┌──────────┐  ┌───────────┐  ┌─────────────┐  │
│  │ /api/  │  │ /api/    │  │ /api/     │  │ /ws          │  │
│  │ atc/*  │  │ weather/* │  │ delay/*   │  │ (WebSocket)  │  │
│  └────┬───┘  └────┬─────┘  └─────┬─────┘  └──────┬──────┘  │
│       │           │              │                │         │
│       ▼           ▼              ▼                ▼         │
│  ┌────────────────────────────────────────────────────┐     │
│  │              Background Tasks                      │     │
│  │  ┌──────────────┐  ┌────────────────────────┐      │     │
│  │  │ Alert        │  │ Weather Cache Refresher │      │     │
│  │  │ Monitor      │  │ (every 15 min)          │      │     │
│  │  └──────┬───────┘  └─────────┬──────────────┘      │     │
│  └─────────┼────────────────────┼──────────────────────┘     │
└────────────┼────────────────────┼────────────────────────────┘
             │                    │
             ▼                    ▼
     ┌───────────────┐    ┌──────────────┐
     │   Redis       │    │  weather_    │
     │  weather:*    │◄───│  service.py  │
     │  alerts       │    └──────┬───────┘
     └───────────────┘           │
                          ┌──────▼───────┐
                          │  Open-Meteo  │
                          │  API         │
                          └──────────────┘
     ┌───────────────┐
     │  delay_       │◄──── gets cached weather
     │  predictor.py │      from Redis (no API cost)
     └───────────────┘
     ┌───────────────┐
     │ bot_router_   │◄──── gets cached weather
     │ mcp_client.py │      from Redis (no API cost)
     └───────────────┘
```

---

## Redis Keys

| Key Pattern | Type | TTL | Example |
|-------------|------|-----|---------|
| `weather:current:{icao}` | String (JSON) | 15 min | `weather:current:VABB` |
| `weather:forecast:{icao}` | String (JSON) | 1 hour | `weather:forecast:VIDP` |
| `weather:historical:{icao}` | String (JSON) | 6 hours | `weather:historical:VOBL` |
| `weather:alerts` | Sorted Set | N/A (managed) | score=2, member=`VABB` |
| `weather:alert:{icao}` | String (JSON) | 1 hour | Details of active alert |

**Alert severity scores:**
- 1 = 🟡 Yellow (low visibility, high heat)
- 2 = 🟠 Orange (fog, strong winds, heavy rain)
- 3 = 🔴 Red (thunderstorm, snowfall, extreme gusts)

---

## API Response Format

### `GET /api/weather/VABB`

```json
{
  "icao": "VABB",
  "airport": "Mumbai",
  "current": {
    "temperature_2m": 28.3,
    "relative_humidity_2m": 72,
    "weather_code": 3,
    "weather_description": "Overcast",
    "wind_speed_10m": 12.5,
    "wind_direction_10m": 250,
    "wind_gusts_10m": 18.2,
    "pressure_msl": 1013.2,
    "visibility": 4000,
    "cloud_cover_low": 88
  },
  "hourly": [
    {"time": "2026-05-14T14:00", "temperature_2m": 27, "weather_code": 3, "precipitation": 0.2, "wind_speed_10m": 11},
    {"time": "2026-05-14T15:00", "temperature_2m": 26, "weather_code": 61, "precipitation": 1.5, "wind_speed_10m": 14},
    ...
  ],
  "daily": [
    {"date": "2026-05-14", "temperature_2m_max": 30, "precipitation_sum": 2.5, "wind_speed_10m_max": 18}
  ],
  "alerts": ["strong_winds"],
  "cached_at": "2026-05-14T12:30:00Z"
}
```

### `GET /api/weather/batch?icaos=VABB,VIDP`

```json
{
  "airports": [
    { "icao": "VABB", "airport": "Mumbai", "current": {...} },
    { "icao": "VIDP", "airport": "Delhi", "current": {...} }
  ]
}
```

### WebSocket Alert Message

```json
{
  "type": "weather_alert",
  "alerts": [
    {"icao": "VABB", "severity": 3, "type": "thunderstorm", "message": "Thunderstorm at Mumbai (VABB)"},
    {"icao": "VIDP", "severity": 1, "type": "low_visibility", "message": "Low visibility at Delhi (VIDP): 2.1 km"}
  ]
}
```

---

## Files Modified

| File | Action | Purpose |
|------|--------|---------|
| `build/cortex-webapp/weather_service.py` | **NEW** | Core weather fetch + cache logic |
| `build/cortex-webapp/web_app.py` | Modified | `/api/weather/*` routes, background alert task |
| `build/cortex-webapp/delay_predictor.py` | Modified | Weather factor in delay prediction |
| `build/cortex-webapp/bot_router_mcp_client.py` | Modified | Weather tool + semantic routing |
| `build/cortex-webapp/static/js/app.js` | Modified | Weather widget, alert banner, fetch logic |
| `build/cortex-webapp/static/dashboard.html` | Modified | Weather + alert UI containers |

---

## Implementation Order

1. `weather_service.py` + Redis caching
2. `/api/weather/*` routes in `web_app.py`
3. Frontend weather widget (HTML + JS)
4. Delay prediction weather factors
5. LLM/bot weather tools
6. Proactive weather alerts (backend + frontend)

Each phase is built, tagged (`v2026.05.14.xx`), and deployed to the `feature/openmeteo-integration` branch.

---

## Rate Limit Budget

Open-Meteo free tier: **10,000 calls/day**, 5,000/hour, 600/minute.

| Source | Calls/day | Notes |
|--------|-----------|-------|
| Current weather poll (30 airports × 96/day) | 2,880 | Every 15 min |
| Forecast refresh (30 airports × 24/day) | 720 | Every 1 hour |
| User-triggered queries | ~500 | Airport selection, bot queries |
| Delay predictor (1 call per prediction ÷ cached) | ~50 | Nearly all cached |
| Alert checker (every 15 min) | 0 | Reuses cached data |
| **Total** | **~4,150/day** | **41% of limit** |

30+ free calls remain for growth. Batch endpoint can reduce further if needed.

---

## Rollback

If issues arise:
- `git revert` the feature branch commits
- Rebuild + deploy the reverted image with `:latest` tag
- Notify users via the alert banner if applicable
