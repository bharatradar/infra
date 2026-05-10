# Wingbits Feature Integration — Implementation Plan

## Overview
Migrate cortex.bharatradar.com from OpenLayers/DOM markers to MapLibre GL JS
(WebGL canvas), fix smooth movement via velocity extrapolation + WebSocket,
join aircraft type data from back-end, and add a preferences panel in
fullscreen mode.

## Phase Order (sequential)

### Phase 1a — Fix Smooth Movement (Velocity Extrapolation + Faster Polling)
- Change `FRONTEND_ATC_POLL_INTERVAL_MS` 5000→2000
- Add velocity extrapolation in `interpolateAircraftPositions()`:
  between poll updates, predict (lat,lon) using heading + speed + elapsed time
  instead of lerping toward a fixed target
- Increase `TRANSITION_SPEED` from 0.1 to match new interval

### Phase 1b — WebSocket Endpoint + AWS Nginx + Auto-Fallback
- **AWS nginx:** Add `proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";` to cortex server block
- **`web_app.py`:** Add `@app.websocket("/ws")` that subscribes to Redis
  `raga_flight_status:flight_events` pub/sub (already populated by flight-tracker
  every 1s) and pushes flight_snapshot messages to browser clients
- **`web_app_db.py`:** Add `subscribe_flight_events()` — returns an
  async generator from `redis.pubsub().listen()`
- **`config.py`:** Add `FRONTEND_WS_ENABLED = True`, `FRONTEND_WS_USE_FOR_ATC = True`,
  `FRONTEND_WS_USE_FOR_RADAR = True`
- **`app.js`:** Change `WS_URL` from `ws://localhost:8002` to
  `wss://${location.host}/ws`; fix fallback logic — if WebSocket fails,
  revert to REST polling with velocity extrapolation (Phase 1a fallback)
- **`deployment.yaml`:** Set env `WS_ENABLED: "true"` (currently `"false"`)

### Phase 2 — MapLibre GL JS Migration (Both Views)
- Add MapLibre GL JS 4.7.x CDN to `dashboard.html`
- **Main map (`initMainMap()`):** Replace `ol.Map` → `maplibregl.Map`
  - Same basemaps (CARTO dark-matter-gl-style)
  - Aircraft as GeoJSON source + circle/symbol layer
  - Remove tar1090 DOM marker imports
- **Fullscreen map (`initFullscreenMap()`):** Same migration
- **Aircraft updates:** Replace `ol.source.Vector.addFeature()` with
  `map.getSource('aircraft').setData(geojsonFeatureCollection)`
- MapLibre GPU interpolation provides smooth movement automatically
- Keep all other dashboard UI (tabs, stats, charts, side panels) unchanged

### Phase 3 — Aircraft Type Back-End Join
In `fetch_live_flights()` (web_app_db.py), after fetching from Redis/DB,
enrich each flight with `ac_type`, `reg`, `desc` from the in-memory
`AIRCRAFT_DB` dict:
```python
if flight['hexid'] in AIRCRAFT_DB:
    info = AIRCRAFT_DB[flight['hexid']]
    flight['ac_type'] = info['type']
    flight['reg'] = info['reg']
    flight['desc'] = info['desc']
```

### Phase 4 — Preferences Panel (Fullscreen Only)
Add gear icon in fullscreen header; slide-in panel with:
- **Color by** — None / Altitude / Category radio buttons
- **Altitude filter** — range slider [0–60000] ft
- **Show aircraft type** — toggle chips (Light Aircraft, Small, Large, Heavy,
  Helicopter, Glider, Balloon, UAV/Drone, etc.)
- localStorage keys: `map.colorBy`, `map.altitudeRange`, `map.hiddenAircraftTypes`
- Apply filters before passing to MapLibre `setData()`:
  filter hiddenAircraftTypes, clip altitudeRange, set colorBy paint property

## Key AWS Changes Needed
```nginx
# /etc/nginx/sites-enabled/cortex — add after proxy_pass:
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```
Then `sudo nginx -t && sudo systemctl reload nginx`.

## Dependencies & Risks
- Phase 1b blocked on Phase 1a (Phase 1a is the fallback)
- Phase 2 is the largest effort — replacing OpenLayers with MapLibre in both
  views requires careful testing of all interactions (click, hover, zoom, filters)
- Phase 4 builds on Phase 2 (preferences modify MapLibre paint properties)

## Rollback
- Phase 1a: revert poll interval, revert velocity extrapolation
- Phase 1b: set `WS_ENABLED: "false"` in deployment, revert nginx config
- Phase 2: revert to OpenLayers CDN + tar1090 markers
- Phase 4: remove preferences panel HTML/CSS/JS
