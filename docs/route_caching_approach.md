# Route Caching Approach

> Comprehensive architecture & implementation plan for enriching ADS-B flight data with route/airline/aircraft info via Flightradar24 and Redis.

## Table of Contents

1. [Current State](#current-state)
2. [Problem](#problem)
3. [Proposed Architecture](#proposed-architecture)
4. [FR24 Data Format](#fr24-data-format)
5. [Redis Key Layout](#redis-key-layout)
6. [Data Flow](#data-flow)
7. [Code Changes](#code-changes)
8. [Deployment](#deployment)
9. [Installation Script Updates](#installation-script-updates)
10. [FAQ & Decisions](#faq--decisions)

---

## Current State

### Data Sources

| Source | URL | Fields | Cadence |
|--------|-----|--------|---------|
| ADSBExchange (primary) | `globe.adsbexchange.com/re-api/?binCraft&zstd&box=...` | hex, flight, lat, lon, alt_baro, alt_geom, gs, track, baro_rate | 10s |
| adsb.lol (fallback) | `api.adsb.lol/v2/point/...` | same as above | 10s |
| adsb.one (fallback) | `api.adsb.one/v2/point/...` | same as above | 10s |

### Current Redis Layout

| Key | Type | TTL | Data |
|-----|------|-----|------|
| `live_flights` | Hash | 30s | `hex_id → {lat, lon, alt, speed, heading, callsign, last_seen}` |
| `live_flights_meta` | Hash | 30s | `{count, last_update, source}` |

### Current PostgreSQL Tables (relevant)

- **`flights_in_air`** — The full live aircraft table with position + route data. Updated every 10s by `bulk_upsert_flights_in_air()`. UNIQUE on `hexid`.
- **`aircraft_info`** — Per-aircraft reference data: `hex_id`, `registration`, `type`, `updated_at`. No `airline_icao` column yet.
- **`flight_schedules`** — Daily schedules downloaded at 10pm by `schedule-downloader` CronJob. Contains `callsign`, `route_airport`, `scheduled_time`, etc.
- **`flight_events`**, **`arrivals_log`**, **`departures_log`** — Event logs for landings, takeoffs, etc.

### Current Enrichment Workers

- **`gap_filler_worker()`** (runs every 10s) — Queries `flights_in_air WHERE origin_icao IS NULL`, calls `fetch_flight_details()` (FR24 search + FlightAware + adsbdb) to resolve routes. Updates via `update_flight_in_air_route()`.
- **`route_enrichment_worker()`** (runs every 30s) — Same logic but batches LIMIT 10.

Both workers:
- Call FR24 search API (`flightradar24.com/v1/search/web/find?query={callsign}`) which is a **different endpoint** from the `data.js` zone API
- Set `callsign_iata = None` in all code paths (route enrichment never populates it)
- Write enriched routes back to `flights_in_air` table
- Read from `flights_in_air` to find flights missing enrichment

---

## Problem

### What's Missing

1. **No aircraft registration/type data in live pipeline** — The `_enrich_flights()` function in cortex-webapp reads from tar1090's `aircraft.csv.gz` (downloaded separately, once daily), not from live data

2. **No `airline_icao` in aircraft_info** — Need to track which airline operates each aircraft

3. **Enrichment workers are complex, poll-based, and DB-bottlenecked** — They poll `flights_in_air` table, make serial external API calls, and write back to DB. If there are 500+ unenriched flights, it takes minutes to catch up.

4. **`callsign_iata` never populated by flight-tracker** — Only the telegram-bot occasionally sets it via adsbdb API

5. **`flights_in_air` is written every 10s for all aircraft** — High write volume to PostgreSQL for data that could be served from Redis

6. **No route data in Redis** — `/api/aircraft/radar` has to fall back to PostgreSQL for enriched data

### What We Want

1. When ADSBExchange data arrives:
   - Cache live position to Redis (as now, `live_flights` hash, 30s TTL)
   - Publish to Redis pubsub for enrichment
2. A consumer picks up new flights, checks persistent route cache:
   - If not cached → call FR24 `data.js?airline={code}&bounds=...` (batched by airline ICAO code)
   - Cache route data persistently in Redis (no TTL)
   - Upsert `aircraft_info` (new hexes only via in-memory cache)
   - Update `flight_schedules` if route mismatch/missing
3. Remove `flights_in_air` from the write path entirely (event logs kept)
4. `/api/aircraft/radar` reads from Redis only
5. Remove enrichment workers (`gap_filler`, `route_enrichment`)

---

## Proposed Architecture

### High-Level Data Flow

```
ADSBExchange (every 10s)
  │
  ├─► sync_valid_flights_to_db()
  │     ├─► _cache_flights_to_redis()          → live_flights hash (30s TTL)
  │     ├─► log_telemetry()                    → InfluxDB flight_path
  │     └─► Redis PUBLISH flight_enrichment     → "{hex_id}|{callsign}"
  │
  ▼
Redis channel flight_enrichment
  │
  ▼
fr24_enrichment_consumer (async, debounce 5s)
  │
  ├─► For each (hex_id, callsign):
  │     ├─► airline_icao = callsign[:3]
  │     ├─► Check Redis: flight_route:{callsign} exists? → skip
  │     └─► If not cached → queue airline_icao for batch
  │
  └─► After 5s debounce, for each unique airline_icao:
        │
        ├─► GET https://data-cloud.flightradar24.com/zones/fcgi/data.js
        │      ?airline={airline_icao}
        │      &bounds={north},{south},{west},{east}
        │      (same bounding box as BinCraft source)
        │
        ├─► Parse response → array of aircraft per airline
        │
        ├─► For each aircraft in response:
        │     ├─► Extract: hex_id, lat, lon, heading, alt, speed,
        │     │            ac_type, registration, origin_iata, dest_iata,
        │     │            flight_number_iata, callsign_icao, airline_icao
        │     ├─► Convert origin_iata → origin_icao (airports.csv cache)
        │     ├─► Convert dest_iata → dest_icao (airports.csv cache)
        │     ├─► Look up lat/lon for origin/dest (airports.csv / TARGET_AIRPORTS)
        │     │
        │     ├─► Redis HSET flight_route:{callsign_icao}
        │     │     {callsign_icao, flight_number_iata, airline_icao,
        │     │      airline_iata, origin_icao, origin_iata,
        │     │      origin_lat, origin_lon, dest_icao, dest_iata,
        │     │      dest_lat, dest_lon, hex_id, ac_type, reg}
        │     │     → NO TTL (persistent until app restart)
        │     │     → Check date: if cached route was fetched today → skip FR24 call
        │     │
        │     ├─► aircraft_info in-memory cache:
        │     │     if hex_id NOT in memory_cache:
        │     │       memory_cache[hex_id] = (reg, ac_type, airline_icao)
        │     │       DB UPSERT aircraft_info (new hexes only)
        │     │
        │     └─► Check flight_schedules for today:
        │           if callsign_icao exists in schedules:
        │             if route differs → UPDATE schedule
        │           else:
        │             INSERT into flight_schedules
        │
        └─► Rate-limit: 1s delay between FR24 API calls
```

### Component View

```
┌─────────────────────────────────────────────────────────┐
│                    flight-tracker pod                     │
│                                                          │
│  ┌──────────────────────┐                                 │
│  │  radar_producer()    │──10s──► ADSBExchange + fallbacks│
│  │  (every 10s)         │         │                      │
│  └──────────┬───────────┘         │                      │
│             │ gets data           │                      │
│             ▼                     │                      │
│  ┌──────────────────────┐         │                      │
│  │  radar_consumer()    │◄──queue─┘                      │
│  │  ┌────────────────┐  │                                 │
│  │  │ process_new    │  │  starts tracking new aircraft  │
│  │  │ _candidates()  │  │                                 │
│  │  └───────┬────────┘  │                                 │
│  │          ▼           │                                 │
│  │  ┌────────────────┐  │                                 │
│  │  │ update_tracked │  │  updates positions + calls      │
│  │  │ _flights()     │  │                                 │
│  │  │                │  │  1. _cache_flights_to_redis()   │
│  │  │                │  │  2. log_telemetry()            │
│  │  │                │  │  3. PUBLISH flight_enrichment  │
│  │  └────────────────┘  │                                 │
│  └──────────────────────┘                                 │
│                                                          │
│  ┌─────────────────────────────────────────────┐          │
│  │           fr24_enrichment_consumer()         │          │
│  │  ┌─────────────┐  ┌──────────────────────┐  │          │
│  │  │ Subscribe to│  │ On debounce (5s):     │  │          │
│  │  │ flight_     │  │ - Group by airline    │  │          │
│  │  │ enrichment  │  │ - Call FR24 per       │  │          │
│  │  │ channel     │  │   airline             │  │          │
│  │  └─────────────┘  │ - Store routes→Redis  │  │          │
│  │                    │ - aircraft_info→DB    │  │          │
│  │                    │ - schedules→DB        │  │          │
│  │                    └──────────────────────┘  │          │
│  └─────────────────────────────────────────────┘          │
│                                                          │
│  ┌──────────────────────┐              ┌───────────────┐ │
│  │  janitor_worker()    │              │  event_       │ │
│  │  (cleanup ground_ops,│              │  handlers     │ │
│  │   stale tracked      │              │  (takeoff,    │ │
│  │   flights, etc.)     │              │   landing)    │ │
│  └──────────────────────┘              └───────────────┘ │
└─────────────────────────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │  Redis   │   │ InfluxDB │   │  PostgreSQL  │
    │          │   │          │   │              │
    │ live_    │   │ flight_  │   │ aircraft_    │
    │ flights  │   │ path     │   │ info         │
    │ (30s)    │   │          │   │              │
    │          │   │          │   │ flight_      │
    │ flight_  │   │          │   │ schedules    │
    │ route:{} │   │          │   │              │
    │ (no TTL) │   │          │   │ flight_      │
    │          │   │          │   │ events       │
    │ aircraft_│   │          │   │ (arrivals,   │
    │ info:{}  │   │          │   │ departures)  │
    │ (no TTL) │   │          │   │              │
    └──────────┘   └──────────┘   └──────────────┘
                          │
                          ▼
                    ┌──────────┐
                    │  cortex- │
                    │  webapp  │
                    │          │
                    │ /api/    │
                    │ aircraft │
                    │ /radar   │
                    │          │
                    │ (Redis   │
                    │  only,   │
                    │  no DB   │
                    │  fallback│
                    │  → [])   │
                    └──────────┘
```

### Call Flow Detail

```
ADSBExchange BinCraft response (every 10s)
  │
  ▼
sync_valid_flights_to_db(aircraft_list)
  │
  ├─► For each aircraft with valid hex/alt/position:
  │     └─► Append tuple (hex_id, callsign, lat, lon, alt, speed, heading)
  │
  ├─► _cache_flights_to_redis(valid_flights)
  │     ├─► DELETE live_flights
  │     ├─► Pipeline HSET live_flights hex_id → {lat, lon, alt, speed, heading, callsign, last_seen}
  │     ├─► HSET live_flights_meta {count, last_update, source}
  │     ├─► EXPIRE live_flights 30s
  │     └─► For each flight:
  │           └─► PUBLISH flight_enrichment "{hex_id}|{callsign}"
  │
  └─► [REMOVED] bulk_upsert_flights_in_air() — no longer called

  NOTE: update_tracked_flights() also does the same for tracked flights
  (per-hex state), but also calls log_telemetry() for InfluxDB.
```

---

## FR24 Data Format

### Endpoint

```
https://data-cloud.flightradar24.com/zones/fcgi/data.js?airline={ICAO_CODE}&bounds={NORTH},{SOUTH},{WEST},{EAST}&air=1
```

### Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `airline` | 3-letter ICAO airline code | `AIC` (Air India), `IGO` (IndiGo) |
| `bounds` | Geographic bounding box: `North,South,West,East` | `33.58,9.33,60.87,104.09` (India) |
| `air` | Include airborne only (1=true) | `1` |
| `ground` | Include ground aircraft (1=true) | optional |

### Response Format

```json
{
  "full_count": 20862,
  "version": 4,
  "3fa6140f": [
    "8004DD",           // [0] hex_id (ICAO 24-bit address)
    38.4540,            // [1] lat
    -123.0604,          // [2] lon
    142,                // [3] heading (degrees)
    26075,              // [4] altitude (feet)
    395,                // [5] speed (knots)
    "",                 // [6] squawk code
    "F-BDWY1",          // [7] data source / radar type
    "B77W",             // [8] aircraft type (ICAO type designator)
    "VT-ALO",           // [9] registration (tail number)
    1778592350,         // [10] timestamp (epoch)
    "DEL",              // [11] origin airport (IATA code)
    "SFO",              // [12] destination airport (IATA code)
    "AI173",            // [13] flight number (IATA, e.g. "AI173")
    0,                  // [14] on ground flag (0=airborne, 1=ground)
    -2240,              // [15] vertical rate (ft/min)
    "AIC173",           // [16] callsign (ICAO, e.g. "AIC173")
    0,                  // [17] unknown flag
    "AIC"               // [18] airline ICAO code
  ],
  "3fa650ce": [
    "800463",
    50.9554,
    -133.3924,
    94,
    37000,
    495,
    "",
    "F-BDWY1",
    "B77W",
    "VT-ALN",
    1778592349,
    "DEL",
    "YVR",
    "AI185",
    0,
    0,
    "AIC185",
    0,
    "AIC"
  ],
  "stats": {
    "total": {
      "ads-b": 17031,
      "mlat": 1408,
      "faa": 165,
      "flarm": 413,
      "estimated": 268,
      "satellite": 1167,
      "uat": 95,
      "other": 239
    },
    "visible": {
      "ads-b": 80,
      "mlat": 2,
      "faa": 0,
      "flarm": 0,
      "estimated": 0,
      "satellite": 0,
      "uat": 0,
      "other": 0
    }
  }
}
```

### Array Index Reference

| Index | Field | Type | Example | Notes |
|-------|-------|------|---------|-------|
| [0] | hex_id | string | `"8004DD"` | ICAO 24-bit address, uppercase |
| [1] | lat | float | `38.4540` | Decimal degrees |
| [2] | lon | float | `-123.0604` | Decimal degrees |
| [3] | heading | int | `142` | Degrees |
| [4] | alt | int | `26075` | Feet |
| [5] | speed | int | `395` | Knots |
| [6] | squawk | string | `""` | Transponder code |
| [7] | source | string | `"F-BDWY1"` | Data source label |
| [8] | ac_type | string | `"B77W"` | ICAO aircraft type code |
| [9] | registration | string | `"VT-ALO"` | Aircraft registration |
| [10] | timestamp | int | `1778592350` | Unix epoch |
| [11] | origin_iata | string | `"DEL"` | Origin airport IATA code |
| [12] | dest_iata | string | `"SFO"` | Destination airport IATA code |
| [13] | flight_number_iata | string | `"AI173"` | Commercial flight number |
| [14] | on_ground | int | `0` | 0=airborne, 1=ground |
| [15] | vertical_rate | int | `-2240` | ft/min |
| [16] | callsign_icao | string | `"AIC173"` | ICAO callsign |
| [17] | unknown | int | `0` | Unknown flag |
| [18] | airline_icao | string | `"AIC"` | ICAO airline code |

### BinCraft Bounding Box → FR24 Bounds Conversion

Current BinCraft box: `box=9.337602,33.583193,60.875230,104.092506`
- BinCraft format: `box=minLat,maxLat,minLon,maxLon`
- FR24 format: `bounds=maxLat,minLat,minLon,maxLon`

Result: `bounds=33.58,9.34,60.88,104.09`

Configurable via new env var `FR24_BOUNDS` with fallback extracted from `ADSB_EXCHANGE_BINCRAFT_URL`.

---

## Redis Key Layout

### Post-Change Keys

| Key | Type | TTL | Description | Fields |
|-----|------|-----|-------------|--------|
| `live_flights` | Hash | 30s | Live position data (unchanged) | `hex_id → json({lat, lon, alt, speed, heading, callsign, last_seen})` |
| `live_flights_meta` | Hash | 30s | Metadata (unchanged) | `{count, last_update, source}` |
| `flight_route:{callsign}` | Hash | None (persistent) | Route + airline data by ICAO callsign | `{callsign_icao, flight_number_iata, airline_icao, airline_iata, origin_icao, origin_iata, origin_lat, origin_lon, dest_icao, dest_iata, dest_lat, dest_lon, hex_id, ac_type, reg}` |
| `aircraft_info:{hex_id}` | Hash | None (persistent) | Aircraft reference data | `{registration, type, airline_icao}` |
| `flight_enrichment` | PubSub | - | Channel for new-flight notifications | Messages: `"{hex_id}\|{callsign}"` |

### Key Rationale

- **`flight_route:{callsign}`** — Keyed by ICAO callsign (e.g., `AIC173`) because a callsign has the same route every day (same flight number, same origin/dest). No TTL because route data doesn't expire mid-flight. Cache staleness handled by checking the embedded date/fetch-time against today.

- **`aircraft_info:{hex_id}`** — Keyed by hex_id because an aircraft (hex) has a fixed registration + type for its lifetime. No TTL.

- **`live_flights` — 30s TTL** — Position data becomes stale quickly. If a flight disappears from ADS-B, its position should vanish from the radar within 30s.

- **`flight_enrichment` — PubSub** — Real-time notification without polling. The consumer debounces for 5s to batch by airline code before making any FR24 API calls.

### Phone Prefix Convention

- `live_*` — Ephemeral position data (short TTL)
- `flight_route:*` — Route/airline reference data (persistent)
- `aircraft_info:*` — Aircraft reference data (persistent)

---

## Data Flow

### Startup Sequence

```
1. main() → creates pool, redis, FlightMonitor
2. FlightMonitor.run()
   ├─► reset_system_state() → clear stale Redis/Tracked flights
   ├─► load_airports_to_redis() → GEOADD india_airports
   ├─► download_static_data()
   ├─► load_static_data() → load airports.csv, airlines.csv, routes.csv
   ├─► load_aircraft_info_cache() → load ALL aircraft_info rows into memory dict
   │     self.aircraft_info_cache = {hex_id: (reg, type, airline_icao)}
   │
   ├─► Task: radar_producer()         → fetches ADSBExchange every 10s
   ├─► Task: radar_consumer()         → processes aircraft, updates tracking
   ├─► Task: fr24_enrichment_consumer()  → listens on Redis pubsub
   ├─► Task: janitor_worker()         → periodic cleanup
   └─► Task: websocket_broadcaster()  → (if enabled)
```

### Steady-State Flow (every 10s)

```
1. radar_producer()
   ├─► get_aircraft_data()
   │     ├─► ADSBExchange BinCraft (primary) → decode → return aircraft list
   │     └─► fallbacks if primary fails
   │
   ├─► sync_valid_flights_to_db(aircraft_data)
   │     ├─► Filter: valid hex, altitude, position
   │     ├─► _cache_flights_to_redis(valid_flights)
   │     │     ├─► DELETE live_flights
   │     │     ├─► HSET live_flights for each flight
   │     │     ├─► EXPIRE live_flights 30s
   │     │     └─► For each flight:
   │     │           PUBLISH flight_enrichment "{hex_id}|{callsign}"
   │     │           [This triggers FR24 enrichment if route not cached]
   │     │
   │     └─► [REMOVED] bulk_upsert_flights_in_air() — NOT called
   │
   ├─► Pass data to radar_queue
   │
   └─► [REMOVED] gap_filler_worker(), route_enrichment_worker() — NOT started

2. radar_consumer()
   ├─► process_new_candidates(data) → start tracking new hexes
   ├─► update_tracked_flights(data)
   │     ├─► For each tracked flight:
   │     │     ├─► Update position in tracked_flights dict
   │     │     ├─► log_telemetry() → InfluxDB (callsign_iata from CALLSIGN_CACHE)
   │     │     ├─► Check ground ops → handle takeoff/landing events
   │     │     └─► [REMOVED] Return upsert_tuple — no flights_in_air update
   │     │
   │     └─► [REMOVED] bulk_upsert_flights_in_air() — NOT called
   │
   └─► [REMOVED] cleanup_stale_flights() — NOT called (Redis TTL handles this)

3. fr24_enrichment_consumer()  (listening on flight_enrichment Redis PubSub)
   │
   ├─► On message: "{hex_id}|{callsign}"
   │     ├─► airline = callsign[:3]
   │     ├─► cached = EXISTS flight_route:{callsign}
   │     ├─► if cached → skip (route already known)
   │     └─► if not cached → pending_airlines.add(airline)
   │
   └─► Every 5s (debounce):
         for airline in pending_airlines:
           │
           ├─► GET https://data-cloud.flightradar24.com/zones/fcgi/data.js
           │      ?airline={airline}&bounds=...
           │
           ├─► Parse response JSON
           │
           ├─► For each (key, array) in response (excluding full_count, version, stats):
           │     ├─► Extract fields from array indices
           │     ├─► Convert origin IATA → ICAO via airport_map
           │     ├─► Convert dest IATA → ICAO via airport_map
           │     ├─► Look up origin lat/lon
           │     ├─► Look up dest lat/lon
           │     ├─► airline_iata: first alpha prefix of flight_number_iata
           │     │   (e.g., "AI173" → "AI") or from airlines.csv ICAO→IATA map
           │     │
           │     ├─► Redis HSET flight_route:{callsign_icao}
           │     │     callsign_icao → array[16]
           │     │     flight_number_iata → array[13]
           │     │     airline_icao → array[18]
           │     │     airline_iata → resolved from airlines.csv
           │     │     origin_icao → resolved from airport_map
           │     │     origin_iata → array[11]
           │     │     origin_lat → resolved from TARGET_AIRPORTS/airports.csv
           │     │     origin_lon → resolved from TARGET_AIRPORTS/airports.csv
           │     │     dest_icao → resolved from airport_map
           │     │     dest_iata → array[12]
           │     │     dest_lat → resolved from TARGET_AIRPORTS/airports.csv
           │     │     dest_lon → resolved from TARGET_AIRPORTS/airports.csv
           │     │     hex_id → array[0]
           │     │     ac_type → array[8]
           │     │     reg → array[9]
           │     │
           │     ├─► If hex_id NOT in self.aircraft_info_cache:
           │     │     self.aircraft_info_cache[hex_id] = (reg, ac_type, airline_icao)
           │     │     DB UPSERT aircraft_info (hex_id, reg, ac_type, airline_icao)
           │     │
           │     └─► Check flight_schedules for today:
           │           SELECT FROM flight_schedules
           │           WHERE callsign = array[16] AND scheduled_time >= today
           │           if not found:
           │             INSERT into flight_schedules
           │             (airport=origin_icao, direction='DEPARTURES',
           │              callsign=array[16], flight_number=array[13],
           │              route_airport=dest_icao, ...)
           │           elif route differs:
           │             UPDATE flight_schedules SET route_airport=dest_icao, ...
           │
           └─► await asyncio.sleep(1)  # Rate limit between airlines
```

---

## Code Changes

### Files Modified

| File | Change Type | Details |
|------|-------------|---------|
| `build/flight-tracker/fr24_data.py` | **NEW** | FR24 data fetching, parsing, enrichment |
| `build/flight-tracker/main.py` | Modify | Add FR24 consumer, remove DB writes, remove enrichment workers |
| `build/flight-tracker/db.py` | Modify | Add aircraft_info methods, no-op flights_in_air methods |
| `build/flight-tracker/config.py` | Modify | Add FR24 config vars |
| `build/flight-tracker/utils.py` | Modify (minor) | Export CALLSIGN_CACHE for callsign_iata in telemetry |
| `build/cortex-webapp/web_app.py` | Modify | Remove PostgreSQL fallback in /api/aircraft/radar |
| `build/cortex-webapp/web_app_db.py` | Modify (minor) | Update telemetry query |
| `scripts/db/postgres/schema.sql` | Modify | Add airline_icao to aircraft_info |
| `docs/route_caching_approach.md` | **NEW** | This document |

### 1. `build/flight-tracker/fr24_data.py` (NEW)

```python
"""
FR24 Route Enrichment Module
Fetches and caches route data from Flightradar24 data.js endpoint.
"""
import logging
import re
import csv
import time
import os
from config import Config

logger = logging.getLogger(__name__)

# Airport IATA → ICAO map loaded at startup
AIRPORT_IATA_MAP = {}
AIRPORT_COORDS = {}       # ICAO → {lat, lon}

# Airline ICAO → IATA map loaded at startup
AIRLINE_IATA_MAP = {}

# In-memory cache of hex_ids already written to aircraft_info table
# Prevents redundant DB writes. Populated at startup.
SEEN_AIRCRAFT_INFO = set()


def extract_bounds():
    """Extract FR24 bounds from ADSB_EXCHANGE_BINCRAFT_URL box parameter."""
    url = getattr(Config, 'ADSB_EXCHANGE_BINCRAFT_URL', '')
    match = re.search(r'box=([\d.]+),([\d.]+),([\d.]+),([\d.]+)', url)
    if match:
        min_lat, max_lat, min_lon, max_lon = match.groups()
        return f"{max_lat},{min_lat},{max_lon},{min_lon}"
    return None


def load_airport_maps():
    """Load IATA→ICAO + coordinates from airports.csv into global dicts."""
    path = Config.AIRPORTS_CSV_FILE
    if not os.path.exists(path):
        logger.warning(f"airports.csv not found at {path}")
        return
        
    with open(path, mode='r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            iata = row.get('IATA', '').strip().upper()
            icao = row.get('ICAO', '').strip().upper()
            if iata and icao:
                AIRPORT_IATA_MAP[iata] = icao
                if icao not in AIRPORT_COORDS:
                    try:
                        AIRPORT_COORDS[icao] = {
                            'lat': float(row.get('lat', 0)),
                            'lon': float(row.get('lon', 0))
                        }
                    except (ValueError, TypeError):
                        pass
    
    # Also populate from Config.TARGET_AIRPORTS (which has more complete data)
    for icao, data in Config.TARGET_AIRPORTS.items():
        icao_upper = icao.upper()
        if data.get('iata'):
            AIRPORT_IATA_MAP[data['iata'].upper()] = icao_upper
        if icao_upper not in AIRPORT_COORDS and data.get('lat'):
            AIRPORT_COORDS[icao_upper] = {
                'lat': float(data['lat']),
                'lon': float(data['lon'])
            }
    
    logger.info(f"✅ Loaded {len(AIRPORT_IATA_MAP)} airport IATA→ICAO mappings")


def load_airline_iata_map():
    """Load ICAO→IATA airline codes from airlines.csv."""
    path = Config.AIRLINES_FILE
    if not os.path.exists(path):
        logger.warning(f"airlines.csv not found at {path}")
        return
        
    with open(path, mode='r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if str(row.get('Active', 'Y')).strip().upper() != 'N':
                icao = row.get('ICAO', '').strip().upper()
                iata = row.get('IATA', '').strip().upper()
                if icao and iata:
                    AIRLINE_IATA_MAP[icao] = iata
    
    logger.info(f"✅ Loaded {len(AIRLINE_IATA_MAP)} airline ICAO→IATA mappings")


def get_airline_iata(airline_icao):
    """Resolve airline IATA code from ICAO code (e.g., 'AIC' → 'AI')."""
    return AIRLINE_IATA_MAP.get(airline_icao.upper())


def resolve_airport(iata_code):
    """Convert IATA airport code to ICAO + coordinates."""
    iata = iata_code.upper() if iata_code else ''
    if not iata:
        return None, None, None
    icao = AIRPORT_IATA_MAP.get(iata)
    coords = AIRPORT_COORDS.get(icao, {}) if icao else {}
    return icao, coords.get('lat'), coords.get('lon')


def parse_fr24_aircraft(key, arr):
    """Parse a single FR24 aircraft array into a dict."""
    return {
        'hex_id': arr[0],
        'lat': arr[1],
        'lon': arr[2],
        'heading': arr[3],
        'alt': arr[4],
        'speed': arr[5],
        'ac_type': arr[8] if len(arr) > 8 else None,
        'registration': arr[9] if len(arr) > 9 else None,
        'origin_iata': arr[11] if len(arr) > 11 else None,
        'dest_iata': arr[12] if len(arr) > 12 else None,
        'flight_number_iata': arr[13] if len(arr) > 13 else None,
        'callsign_icao': arr[16] if len(arr) > 16 else None,
        'airline_icao': arr[18] if len(arr) > 18 else None,
    }


async def fetch_airline_data(session, airline_code, bounds):
    """Fetch FR24 data.js for a single airline code."""
    url = f"{Config.FR24_DATA_URL}?airline={airline_code}&bounds={bounds}&air=1"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Origin': 'https://www.flightradar24.com',
        'Referer': 'https://www.flightradar24.com/',
    }
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = []
                for key, value in data.items():
                    if key in ('full_count', 'version', 'stats'):
                        continue
                    if isinstance(value, list) and len(value) >= 14:
                        results.append(parse_fr24_aircraft(key, value))
                logger.debug(f"📡 FR24 {airline_code}: {len(results)} aircraft")
                return results
            else:
                logger.warning(f"⚠️ FR24 {airline_code}: HTTP {resp.status}")
                return []
    except Exception as e:
        logger.warning(f"⚠️ FR24 {airline_code} fetch failed: {e}")
        return []
```

### 2. `build/flight-tracker/main.py` Changes

#### 2a. Add FR24 Consumer Task

```python
async def fr24_enrichment_consumer(self):
    """Listen to Redis pubsub for new flights, batch by airline, fetch routes from FR24."""
    logger.info("🗺️ FR24 Enrichment Consumer Started")
    
    pubsub = self.redis.pubsub()
    await pubsub.subscribe("flight_enrichment")
    
    pending_airlines = set()
    last_process = time.time()
    debounce = getattr(Config, 'FR24_ENRICHMENT_DEBOUNCE_SEC', 5)
    
    # Load airport + airline maps
    load_airport_maps()
    load_airline_iata_map()
    
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        
        try:
            hex_id, callsign = message["data"].split("|", 1)
            if len(callsign) < 3:
                continue
            
            airline = callsign[:3].upper()
            
            # Check if this callsign's route is already cached
            cached = await self.redis.exists(f"flight_route:{callsign}")
            if not cached:
                pending_airlines.add(airline)
            
            # Debounce: process batch every N seconds
            now = time.time()
            if pending_airlines and (now - last_process) >= debounce:
                for airline_code in sorted(pending_airlines):
                    await self._process_airline_batch(airline_code)
                    await asyncio.sleep(1)  # Rate limit between airlines
                pending_airlines.clear()
                last_process = now
                
        except Exception as e:
            logger.error(f"FR24 Consumer error: {e}")
```

#### 2b. Add Airline Batch Processor

```python
async def _process_airline_batch(self, airline_code):
    """Fetch FR24 data for one airline, cache routes, update aircraft_info & schedules."""
    bounds = extract_bounds()
    if not bounds:
        logger.warning("⚠️ No bounds configured for FR24 enrichment")
        return
    
    aircraft_list = await fetch_airline_data(self.session, airline_code, bounds)
    if not aircraft_list:
        return
    
    now = time.time()
    
    for ac in aircraft_list:
        callsign = ac['callsign_icao']
        hex_id = ac['hex_id']
        if not callsign:
            continue
        
        # 1. Resolve origin/dest IATA → ICAO + coordinates
        orig_icao, orig_lat, orig_lon = resolve_airport(ac['origin_iata'])
        dest_icao, dest_lat, dest_lon = resolve_airport(ac['dest_iata'])
        
        # 2. Derive airline_iata
        airline_iata = get_airline_iata(ac['airline_icao'])
        if not airline_iata and ac['flight_number_iata']:
            # Fallback: extract alpha prefix from flight number
            match = re.match(r'^([A-Z]+)', ac['flight_number_iata'])
            if match:
                airline_iata = match.group(1)
        
        # 3. Store route in Redis (persistent, no TTL)
        route_data = {
            'callsign_icao': callsign,
            'flight_number_iata': ac['flight_number_iata'] or '',
            'airline_icao': ac['airline_icao'] or '',
            'airline_iata': airline_iata or '',
            'origin_icao': orig_icao or '',
            'origin_iata': ac['origin_iata'] or '',
            'origin_lat': str(orig_lat or ''),
            'origin_lon': str(orig_lon or ''),
            'dest_icao': dest_icao or '',
            'dest_iata': ac['dest_iata'] or '',
            'dest_lat': str(dest_lat or ''),
            'dest_lon': str(dest_lon or ''),
            'hex_id': hex_id,
            'ac_type': ac['ac_type'] or '',
            'reg': ac['registration'] or '',
            'fetched_at': str(now),
            'fetched_date': time.strftime('%Y-%m-%d', time.gmtime(now)),
        }
        
        await self.redis.hset(f"flight_route:{callsign}", mapping=route_data)
        
        # 4. Update aircraft_info (in-memory cache, write to DB only for new hexes)
        if hex_id and ac['registration'] and hex_id not in SEEN_AIRCRAFT_INFO:
            SEEN_AIRCRAFT_INFO.add(hex_id)
            await self.db.upsert_aircraft_info(
                hex_id=hex_id,
                registration=ac['registration'],
                ac_type=ac['ac_type'],
                airline_icao=ac['airline_icao']
            )
        
        # 5. Check/update flight_schedules for today
        await self.db.check_and_update_schedule(
            callsign=callsign,
            flight_number=ac['flight_number_iata'],
            origin_icao=orig_icao,
            dest_icao=dest_icao
        )
    
    logger.info(f"✅ Cached {len(aircraft_list)} routes for airline {airline_code}")
```

#### 2c. Modify `_cache_flights_to_redis()` — Add PUBLISH

Add at end of `_cache_flights_to_redis()`:

```python
# Publish each flight for FR24 enrichment
for fl in flights_list:
    hex_id, callsign, *_ = fl
    if callsign and callsign != hex_id:
        await self.redis.publish(
            "flight_enrichment",
            f"{hex_id}|{callsign}"
        )
```

#### 2d. Modify `sync_valid_flights_to_db()` — Remove `bulk_upsert_flights_in_air`

```python
async def sync_valid_flights_to_db(self, aircraft_list):
    # ... validation logic unchanged ...
    if valid_flights:
        try:
            # ✅ Redis cache only — no PostgreSQL flights_in_air write
            await self._cache_flights_to_redis(valid_flights)
        except Exception as e:
            logger.error(f"❌ SYNC ERROR: {e}")
```

#### 2e. Modify `update_tracked_flights()` — Remove `bulk_upsert_flights_in_air`

```python
async def update_tracked_flights(self, current_aircraft):
    # ... task gathering unchanged ...
    if tasks:
        results = await asyncio.gather(*tasks)
        # [REMOVED] bulk_upsert_flights_in_air — route data comes from FR24 cache
    # [REMOVED] cleanup_stale_flights — Redis TTL handles this
```

Note: The `_update_single_flight` method still handles takeoff/landing events which write to `flight_events`, `arrivals_log`, `departures_log`, `ground_ops` — those are **event logs**, not position data, and are kept.

#### 2f. Modify `run()` — Replace enrichment workers

```python
tasks = [
    asyncio.create_task(self.radar_producer()),
    asyncio.create_task(self.radar_consumer()),
    asyncio.create_task(self.fr24_enrichment_consumer()),  # NEW
    asyncio.create_task(self.janitor_worker()),
    # [REMOVED] gap_filler_worker()
    # [REMOVED] route_enrichment_worker()
    # [REMOVED] scheduled_data_updater()  # NOTE: verify if still needed
]
```

#### 2g. Add `callsign_iata` to `log_telemetry` call

Before `log_telemetry` call in `_update_single_flight` (line ~1237):

```python
# Resolve callsign_iata from CALLSIGN_CACHE if available
callsign_iata = None
cs = tracked_flight.get('callsign', '')
if cs and len(cs) >= 3:
    cache_key = f"fr24_iata_{cs.strip().upper()}"
    cached = CALLSIGN_CACHE.get(cache_key)
    if cached and cached.get('iata_code'):
        callsign_iata = cached['iata_code']

await self.db.log_telemetry(
    hex_id, tracked_flight['callsign'],
    c_lat, c_lon, c_alt, c_speed, c_heading,
    callsign_iata
)
```

### 3. `build/flight-tracker/db.py` Changes

#### 3a. Add aircraft_info methods

```python
async def upsert_aircraft_info(self, hex_id, registration=None, ac_type=None, airline_icao=None):
    """Upsert aircraft_info — only called for NEW hexes not in memory cache."""
    try:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO aircraft_info (hex_id, registration, type, airline_icao, updated_at)
                VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC')
                ON CONFLICT (hex_id) DO UPDATE
                SET registration = COALESCE($2, aircraft_info.registration),
                    type = COALESCE($3, aircraft_info.type),
                    airline_icao = COALESCE($4, aircraft_info.airline_icao),
                    updated_at = NOW() AT TIME ZONE 'UTC'
            """, hex_id.upper(), registration, ac_type, airline_icao)
    except Exception as e:
        logger.warning(f"Failed to upsert aircraft_info {hex_id}: {e}")


async def load_all_aircraft_info(self):
    """Load all aircraft_info rows into memory (called at startup)."""
    try:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT hex_id, registration, type, airline_icao FROM aircraft_info"
            )
            seen = set()
            for r in rows:
                seen.add(r['hex_id'].upper())
            logger.info(f"✅ Loaded {len(seen)} aircraft_info entries into memory")
            return seen
    except Exception as e:
        logger.error(f"Failed to load aircraft_info: {e}")
        return set()
```

#### 3b. Add schedule check method

```python
async def check_and_update_schedule(self, callsign, flight_number, origin_icao, dest_icao):
    """Check flight_schedules for today and update if missing or mismatched."""
    if not callsign or not origin_icao or not dest_icao:
        return
    try:
        async with self.pool.acquire() as conn:
            today = datetime.now(timezone.utc).date()
            row = await conn.fetchrow("""
                SELECT id, route_airport FROM flight_schedules
                WHERE callsign = $1
                  AND scheduled_time::date = $2
                LIMIT 1
            """, callsign, today)
            
            if row:
                # Entry exists — check if route matches
                if row['route_airport'] != dest_icao:
                    await conn.execute("""
                        UPDATE flight_schedules
                        SET route_airport = $1, updated_at = NOW(),
                            updated_from = 'fr24_enrichment'
                        WHERE id = $2
                    """, dest_icao, row['id'])
                    logger.info(f"📝 Updated schedule {callsign}: {origin_icao}→{dest_icao}")
            else:
                # No entry — insert new schedule
                await conn.execute("""
                    INSERT INTO flight_schedules
                        (airport_code, direction, flight_number, callsign,
                         route_airport, scheduled_time, created_from)
                    VALUES ($1, 'DEPARTURES', $2, $3, $4, NOW(), 'fr24_enrichment')
                    ON CONFLICT DO NOTHING
                """, origin_icao, flight_number or callsign, callsign, dest_icao)
                logger.info(f"📝 Inserted new schedule {callsign}: {origin_icao}→{dest_icao}")
    except Exception as e:
        logger.warning(f"Failed to check/update schedule for {callsign}: {e}")
```

#### 3c. No-op flights_in_air methods (or keep with deprecation log)

```python
async def bulk_upsert_flights_in_air(self, flights_list):
    """DEPRECATED: Route data now goes through FR24 → Redis cache."""
    pass  # No-op — flights_in_air is no longer used

async def update_flight_in_air_route(self, *args, **kwargs):
    """DEPRECATED: Route enrichment via Redis cache."""
    pass  # No-op
```

### 4. `build/flight-tracker/config.py` Additions

```python
# FR24 Route Enrichment
FR24_DATA_URL = _env("FR24_DATA_URL", "https://data-cloud.flightradar24.com/zones/fcgi/data.js")
FR24_ENRICHMENT_DEBOUNCE_SEC = int(_env("FR24_ENRICHMENT_DEBOUNCE_SEC", "5"))
REDIS_FLIGHT_ROUTE_PREFIX = "flight_route"
```

### 5. `build/cortex-webapp/web_app.py` Changes

#### 5a. Remove PostgreSQL fallback from `/api/aircraft/radar`

Lines 960-1039 become:

```python
@app.get("/api/aircraft/radar")
async def api_aircraft_radar(
    lat: float = Query(...),
    lon: float = Query(...),
    radius: float = Query(default=100.0)
):
    """Return aircraft within radius miles of given lat/lon — Redis only."""
    try:
        r = await web_app_db.get_redis_client()
        exists = await r.exists(Config.REDIS_LIVE_FLIGHTS_KEY)
        if not exists:
            return []
        
        all_flights = await r.hgetall(Config.REDIS_LIVE_FLIGHTS_KEY)
        if not all_flights:
            return []
        
        result = []
        lat_rad = radians(lat)
        lon_rad = radians(lon)
        
        for hex_id, data in all_flights.items():
            try:
                ac = json.loads(data)
            except:
                continue
            if not ac.get('lat') or not ac.get('lon'):
                continue
            
            ac_lat = ac['lat']
            ac_lon = ac['lon']
            if abs(ac_lat) > 60 and abs(ac_lon) < 60:
                ac_lat, ac_lon = ac_lon, ac_lat
            
            dist = haversine(lat, lon, ac_lat, ac_lon)
            if dist <= radius:
                # Try to get route enrichment from Redis
                callsign = ac.get('callsign', '')
                route = {}
                if callsign:
                    route_data = await r.hgetall(f"flight_route:{callsign}")
                    if route_data:
                        route = {
                            'origin_icao': route_data.get('origin_icao', ''),
                            'dest_icao': route_data.get('dest_icao', ''),
                            'origin_iata': route_data.get('origin_iata', ''),
                            'dest_iata': route_data.get('dest_iata', ''),
                            'ac_type': route_data.get('ac_type', ''),
                            'reg': route_data.get('reg', ''),
                            'airline_icao': route_data.get('airline_icao', ''),
                            'airline_iata': route_data.get('airline_iata', ''),
                        }
                
                result.append({
                    "hexid": hex_id,
                    "callsign": callsign,
                    "lat": ac_lat,
                    "lon": ac_lon,
                    "alt": ac.get('alt', 0),
                    "speed": ac.get('speed', 0) or ac.get('gs', 0),
                    "heading": ac.get('heading', 0),
                    **route,
                })
        
        result.sort(key=lambda x: x.get('alt', 0), reverse=True)
        return _enrich_flights(result[:500])
        
    except Exception as e:
        logger.error(f"aircraft/radar error: {e}")
        return []
```

Helper function:

```python
def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance in miles between two points."""
    R = 3959  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c
```

### 6. Schema Update

```sql
ALTER TABLE aircraft_info
ADD COLUMN IF NOT EXISTS airline_icao VARCHAR(5);
```

### What Stays the Same

- **InfluxDB telemetry writes** — unchanged (still writes `flight_path` every 5s per aircraft)
- **Event logs** — `arrivals_log`, `departures_log`, `flight_events`, `ground_ops` — still written during takeoff/landing
- **`sync_valid_flights_to_db()`** — still called, but only does Redis cache (no DB write)
- **`_update_single_flight()`** — still handles status transitions (airborne → landing → grounded → pre_flight → airborne)
- **`log_telemetry()`** — still writes to InfluxDB (plus `callsign_iata` tag added)
- **`_cache_flights_to_redis()`** — still writes position data to `live_flights` hash (unchanged)
- **Websocket broadcast** — unchanged
- **Janitor worker** — unchanged
- **`schedule-downloader` CronJob** — unchanged (still downloads schedules at 10pm daily)

### What Changes

| Before | After |
|--------|-------|
| `flights_in_air` written every 10s for all 400+ aircraft | `flights_in_air` not written at all (Redis only) |
| Enrichment workers poll DB, call FR24 search + FlightAware + adsbdb | FR24 consumer listens on Redis pubsub, batches by airline, calls `data.js` endpoint |
| Each flight enriched one-by-one (N calls for N flights) | Each airline enriched once (50 calls for 50 airlines serves thousands of flights) |
| Route data in PostgreSQL only | Route data in Redis `flight_route:{callsign}` (persistent, no TTL) |
| `/api/aircraft/radar` falls back to PostgreSQL | `/api/aircraft/radar` reads Redis only (returns `[]` if empty) |
| `aircraft_info` has no `airline_icao` | `aircraft_info` has `airline_icao` (populated from FR24) |
| `callsign_iata` always None | `callsign_iata` populated from `CALLSIGN_CACHE` for InfluxDB telemetry |

---

## Deployment

### 1. Schema Migration

```bash
kubectl exec deploy/postgres -n bharatradar -- psql -U flight_db_user -d flight_db \
  -c "ALTER TABLE aircraft_info ADD COLUMN IF NOT EXISTS airline_icao VARCHAR(5);"
```

### 2. Build & Push Images

```bash
# flight-tracker (main changes)
docker buildx build --platform linux/amd64 \
  -t ghcr.io/bharatradar/flight-tracker:latest \
  -t ghcr.io/bharatradar/flight-tracker:20260512-fr24-enrichment \
  --push /Users/Shared/bharatradar/infra/build/flight-tracker/

# cortex-webapp (API changes)
docker buildx build --platform linux/amd64 \
  -t ghcr.io/bharatradar/cortex-webapp:latest \
  -t ghcr.io/bharatradar/cortex-webapp:20260512-fr24-enrichment \
  --push /Users/Shared/bharatradar/infra/build/cortex-webapp/
```

### 3. Redeploy

```bash
kubectl delete pod -n bharatradar -l app=flight-tracker
kubectl delete pod -n bharatradar -l app=cortex-webapp
```

### 4. Verification

```bash
# Check logs for FR24 enrichment
kubectl logs -n bharatradar deployment/flight-tracker | grep "FR24"

# Check Redis route cache
kubectl exec -n bharatradar deployment/redis -- redis-cli KEYS "flight_route:*"

# Test API
curl "https://cortex.bharatradar.com/api/aircraft/radar?lat=28.6&lon=77.2&radius=50"

# Check aircraft_info table has airline_icao
kubectl exec deploy/postgres -n bharatradar -- psql -U flight_db_user -d flight_db \
  -c "SELECT airline_icao, COUNT(*) FROM aircraft_info WHERE airline_icao IS NOT NULL GROUP BY airline_icao;"
```

---

## Installation Script Updates

### `scripts/db/postgres/schema.sql`

Add `airline_icao` column to existing `aircraft_info` table:

```sql
-- Aircraft info (registration, type, airline lookup by hex_id)
CREATE TABLE IF NOT EXISTS aircraft_info (
    hex_id VARCHAR(10) PRIMARY KEY,
    registration VARCHAR(20),
    type VARCHAR(20),
    airline_icao VARCHAR(5),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `scripts/helpers/templating.sh`

No changes needed — no new env vars that require placeholder substitution. The FR24 configuration and new Redis keys are all self-contained in the flight-tracker code. The `FR24_DATA_URL` and `FR24_ENRICHMENT_DEBOUNCE_SEC` have sensible defaults in `config.py` (`_env()` pattern).

### `scripts/db/init.sh`

Add migration step:

```bash
# Migrate aircraft_info to add airline_icao
psql -U "$DB_USER" -d "$DB_NAME" -c "
  ALTER TABLE aircraft_info ADD COLUMN IF NOT EXISTS airline_icao VARCHAR(5);
" 2>/dev/null || true
```

---

## FAQ & Decisions

### Q: Why debounce for 5s instead of processing immediately?

Grouping by airline code means one FR24 call serves all flights of that airline. With 5s debounce, if 10 new AIC flights appear in 5 seconds, we make 1 FR24 call instead of 10. FR24 rate-limits aggressively, so minimizing API calls is critical.

### Q: Why no TTL on `flight_route:{callsign}`?

Route data for a callsign doesn't change during a flight. The same callsign flies the same route daily. With the `fetched_date` field, we can detect stale entries (yesterday's data) and re-fetch. No TTL avoids unnecessary cache evictions.

### Q: How do we handle route changes mid-day?

If a callsign's route changes (e.g., equipment swap, re-routing), the `flight_schedules` check in `_process_airline_batch()` handles it. The schedule comparison detects mismatches and updates. The schedule-downloader also refreshes all schedules daily at 10pm.

### Q: Why remove `flights_in_air` writes entirely?

- **Write volume**: 400+ aircraft × 6 writes/min × fields = high write load on PostgreSQL for data that's already in Redis
- **Read pattern**: `/api/aircraft/radar` reads position data every 5s (frontend polling). Redis is faster and already has the data.
- **Cost**: PostgreSQL writes are significantly more expensive than Redis writes. The only PostgreSQL writes we keep are event logs and reference data, which are low volume.
- **The table still exists** for any existing queries, but no new data is written.

### Q: What if Redis goes down?

If Redis is down, the flight-tracker continues operating but:
- Position data is lost (not cached)
- FR24 enrichment doesn't trigger (pubsub fails)
- InfluxDB telemetry continues (direct write, no Redis dependency)
- Event logs continue (PostgreSQL direct write)
- `/api/aircraft/radar` returns `[]`

This is acceptable because Redis is the canonical store for live data. When Redis recovers, data starts flowing again within one polling cycle (10s).

### Q: What about `fetch_flight_details()` in event handlers?

The landing/takeoff event handlers in `_update_single_flight()` call `fetch_flight_details()` to determine origin/dest for event logging (e.g., "Flight AIC173 landed at VIDP from VABB"). This code path is **kept** since it's used for event enrichment, not for the live radar display. The route data from the FR24 cache supplements this but doesn't replace it.

### Q: FR24 `airline` parameter vs `bounds` only?

Using `?airline={code}&bounds=...` is preferred over `?bounds=...` alone because:
- Without `airline`, FR24 returns ALL aircraft in the bounds (20K+ aircraft globally). Processing all of them is wasteful.
- With `airline`, we get only aircraft for one Indian airline. We batch by airline, making one call per airline code. Total calls = number of unique airlines seen (typically 50-100 for India).

### Q: How many FR24 API calls per day?

- First cycle at startup: ~50-100 calls (one per unique airline code, with 1s delay = ~50-100s)
- Subsequent cycles: depends on new airlines appearing. Most airlines are cached after the first cycle, so subsequent calls are rare (~10-20/day)
- **Total**: ~100-150 calls/day maximum. FR24's rate limit is generous enough for this.

### Q: Why use Redis PubSub instead of an in-process asyncio.Queue?

- **Persistence**: If the FR24 consumer crashes, messages in the queue are lost with asyncio.Queue. Redis PubSub survives pod restarts (as long as Redis is up).
- **Observability**: External tools can subscribe to `flight_enrichment` and monitor the flow.
- **Separation**: The producer (ADSB sync) and consumer (FR24 enrichment) are decoupled via Redis.

### Q: What about the `scheduled_data_updater()` task?

Currently runs in the flight-tracker to periodically download static data (airports.csv, routes.csv, airlines.csv). This task is **independent** of enrichment and should be kept. It updates files that the FR24 enrichment consumer reads.

### Q: What if FR24 blocks us?

FR24 uses aggressive rate-limiting. Our approach minimizes calls (one per airline, with 1s delay between calls). If we get rate-limited:
- The `_process_airline_batch()` catches the HTTP error and logs a warning
- Route data stays cached (no TTL, so existing routes remain available)
- New airlines won't get enriched until the next successful call
- Consider adding a rotating proxy pool if rate-limiting becomes an issue

### Conversation History

This document was created from the following design discussion:

- **Initial problem**: `/api/telemetry/track` returned `[]` because `aiocsv` module was missing from the webapp image. Fixed by adding `aiocsv` to requirements.txt, rebuilding the image, and installing it in the running pod.

- **Separate issue discovered**: `ai_enrichment_audit` table had only 11 records total. The AI enrichment pipeline was barely running.

- **callsign_iata**: Identified that `callsign_iata` was always `None` in all flight-tracker code paths. Only the telegram-bot occasionally populated it via adsbdb API.

- **FR24 data.js endpoint discovered**: The `data-cloud.flightradar24.com/zones/fcgi/data.js` endpoint provides rich aircraft data including origin/dest IATA codes, aircraft type, registration, airline ICAO code, and flight number — far more than the ADSBExchange ADS-B data provides.

- **Architecture shift**: Instead of per-flight enrichment (pulling one flight at a time via FR24 search API), we batch by airline ICAO code (pushing via Redis PubSub, calling FR24 `data.js?airline={code}&bounds=...` once per airline code).

- **Removal of flights_in_air**: Live position data served exclusively from Redis. PostgreSQL `flights_in_air` table no longer written to. Event logs (arrivals, departures, events, ground_ops) still written to PostgreSQL.

- **Removal of enrichment workers**: `gap_filler_worker()` and `route_enrichment_worker()` become obsolete. Replaced by the FR24 enrichment consumer.

- **aircraft_info enhancement**: Added `airline_icao` column. Populated from FR24 data with in-memory cache to avoid redundant DB writes.

- **Redis route cache**: `flight_route:{callsign}` hash stores origin/dest/airline/aircraft data persistently (no TTL). Separate from `live_flights` (30s TTL for position data only).

- **FR24 refresh cadence**: 15s was discussed but deemed too aggressive for per-airline API calls. Debounce-based approach (5s batch window, triggered by new flight sightings) is more efficient. FR24 is only called when genuinely new data is needed (airline never seen before, or route from yesterday needs refresh).

- **Rate limiting**: 1s delay between FR24 API calls to different airlines. Bounds parameter ensures we only fetch aircraft over India, minimizing payload size.
