# AeroDataBox RapidAPI Integration

## Overview

Integrate the [AeroDataBox](https://www.aerodatabox.com/) API (via RapidAPI) as the **primary** flight schedule data source for 35 Indian airports. AeroDataBox provides high-precision, real-time flight status for the ~next 12 hours. The existing FR24/Avionio scrapers are kept as a **fallback** — only used if AeroDataBox fails completely.

---

## Why AeroDataBox?

| Source | Horizon | Precision | Status Updates | Hex/Reg | Gate/Terminal |
|--------|---------|-----------|----------------|---------|---------------|
| FR24 | Today + Tomorrow | Medium | No | Partial | No |
| Avionio | Today + Tomorrow | Medium | No | No | No |
| **AeroDataBox** | Next 12 hours | **High** | **13 statuses** | **Yes** | **Yes** |

---

## API Details

### Selected Endpoint (API #2 — Relative Time)

```
GET https://aerodatabox.p.rapidapi.com/flights/airports/iata/{IATA_CODE}
```

**Query Parameters:**
| Param | Value | Notes |
|-------|-------|-------|
| `offsetMinutes` | `0` | Start from now |
| `durationMinutes` | `720` | 12-hour window (hard limit) |
| `direction` | `Both` | Arrivals + departures |
| `withCancelled` | `true` | Include cancelled flights |
| `withLeg` | `true` | Return full leg objects (required for route info) |
| `withCodeshared` | `true` | Include codeshare flights |
| `withCargo` | `false` | Exclude cargo |
| `withPrivate` | `false` | Exclude private |
| `withLocation` | `false` | Exclude live aircraft position (handled by radar) |

### Rate Limits

| Plan | Units/month | Our usage (35 airports, 10h cycle) |
|------|-------------|------------------------------------|
| **Free** | 600 | 35 × 2.4 runs/day × 30d × 2 units = **5,040** ❌ exceeds |
| **Pro** | 6,000 | **5,040 units/month** ✅ |

However, for a **2-day test** on the free plan: 35 × 5 runs × 2 units = **~350 units** ✅

---

## Complete Response Schema

Based on the [AeroDataBox OpenAPI spec (v1.6.0.0)](https://doc.aerodatabox.com/), the endpoint returns:

### `AirportFidsContract`
```json
{
  "arrivals": [ /* AirportFlightContract[] */ ],
  "departures": [ /* AirportFlightContract[] */ ]
}
```

### `AirportFlightContract` — Per flight object

```json
{
  "number": "AF 3139",
  "status": "Departed",
  "codeshareStatus": "IsCodeshared",
  "isCargo": false,
  "callSign": null,

  "departure": {
    "airport": {
      "icao": "OTHH",
      "iata": "DOH",
      "name": "Doha",
      "countryCode": "qa",
      "timeZone": "Asia/Qatar"
    },
    "scheduledTime": { "utc": "2026-05-16 16:15Z", "local": "2026-05-16 19:15+03:00" },
    "revisedTime": { "utc": "2026-05-16 16:11Z", "local": "2026-05-16 19:11+03:00" },
    "runwayTime": { "utc": "2026-05-16 16:29Z", "local": "2026-05-16 19:29+03:00" },
    "terminal": "1",
    "checkInDesk": "6-8",
    "gate": "B20",
    "quality": ["Basic", "Live"]
  },

  "arrival": {
    "scheduledTime": { "utc": "2026-05-16 19:45Z", "local": "2026-05-17 01:15+05:30" },
    "revisedTime": { "utc": "2026-05-16 19:45Z", "local": "2026-05-17 01:15+05:30" },
    "terminal": "3",
    "baggageBelt": "13",
    "quality": ["Basic", "Live"]
  },

  "aircraft": {
    "reg": "PH-EXV",
    "modeS": "485871",
    "model": "Embraer EMB 190"
  },

  "airline": {
    "name": "Air France",
    "iata": "AF",
    "icao": "AFR"
  }
}
```

### `FlightStatus` Enum

| Code | String Value | Meaning |
|------|-------------|---------|
| `0` | `UNKNOWN` | Information is not provided |
| `1` | `EXPECTED` | Expected |
| `2` | `EN_ROUTE` | En route |
| `3` | `CHECK_IN` | Check-in is open |
| `4` | `BOARDING` | Boarding in progress / Last call |
| `5` | `GATE_CLOSED` | Gate closed |
| `6` | `DEPARTED` | Departed |
| `7` | `DELAYED` | Delayed |
| `8` | `APPROACHING` | On approach to destination |
| `9` | `ARRIVED` | Arrived |
| `10` | `CANCELED` | Cancelled |
| `11` | `DIVERTED` | Diverted to another destination |
| `12` | `CANCELED_UNCERTAIN` | Probably cancelled, no expected updates |

---

## Flight Number Handling

### The problem

AeroDataBox returns `callSign` (ICAO format like `"AFR3139"`) only **sometimes** — it's an optional field. However, these fields are **always** present:

- `number: "AF 3139"` — IATA flight number (with a space)
- `airline.icao: "AFR"` — ICAO airline code
- `airline.iata: "AF"` — IATA airline code

### The solution

| Source | Stored in | Example | Logic |
|--------|-----------|---------|-------|
| `number` | `flight_number` | `"AF3139"` | Strip space from `"AF 3139"` |
| `callSign` (if present) | `callsign` | `"AFR3139"` | Use as-is |
| `airline.icao` + digits of `number` (if `callSign` absent) | `callsign` | `"AFR3139"` | `"AFR"` + `"3139"` = `"AFR3139"` |
| `aircraft.modeS` | `hex_id` | `"485871"` | Use as-is (when present) |
| `aircraft.reg` | `aircraft_reg` | `"PH-EXV"` | Use as-is (when present) |

### Self-correction via ADS-B

When the radar tracker later spots this flight by hex ID or callsign, the existing `link_actual_flight_to_schedule()` in `db.py` matches the schedule row and overwrites `callsign` with the exact value from ADS-B. So any derived callsign gets corrected automatically.

---

## What AeroDataBox Gives vs What We Store

### Currently stored in `flight_schedules`

| Column | AeroDataBox Source | Compatible? |
|--------|--------------------|-------------|
| `airport_code` | Known from query (the airport we're fetching for) | ✅ |
| `direction` | `arrivals` vs `departures` array membership | ✅ |
| `flight_number` | `number` (strip space: `"AF 3139"` → `"AF3139"`) | ✅ |
| `callsign` | `callSign` if present, else derived from `airline.icao` + digits | ✅ |
| `hex_id` | `aircraft.modeS` | ✅ |
| `route_airport` | `departure.airport.icao` (arrivals) / `arrival.airport.icao` (departures) | ✅ |
| `scheduled_time` | `arrival.scheduledTime.utc` (arrivals) / `departure.scheduledTime.utc` (departures) | ✅ |
| `actual_time` | `arrival.runwayTime.utc` / `departure.runwayTime.utc` (if available) | ✅ |

### NOT stored (but AeroDataBox gives) — NOW ALL ADDED

| Data | Column | Why Store It |
|------|--------|--------------|
| `status` (enum) | `status` | Core flight status — delayed, cancelled, boarding, etc. |
| `revisedTime` | `estimated_time` | Estimated time after delay |
| `terminal` | `terminal` | Ground ops display |
| `gate` | `gate` | Boarding gate info |
| `runway` | `runway` | Correlate with radar |
| `airline.iata` | `airline_iata` | Airline code for display |
| `airline.icao` | `airline_icao` | ICAO airline code |
| `airline.name` | `airline_name` | Display airline name |
| `aircraft.reg` | `aircraft_reg` | Tail number |
| `aircraft.model` | `aircraft_model` | Aircraft type |
| `is_cargo` | `is_cargo` | Filter cargo flights |
| `codeshareStatus` | `is_codeshare` | Codeshare flag |

### What AeroDataBox does NOT provide

- Nothing critical — covers schedules, status, times, aircraft, airline, and ground ops

---

## Schema Changes

### Migration: `scripts/db/postgres/migration-v3.sql`

```sql
ALTER TABLE flight_schedules
  ADD COLUMN IF NOT EXISTS status VARCHAR(20),
  ADD COLUMN IF NOT EXISTS estimated_time TIMESTAMP,
  ADD COLUMN IF NOT EXISTS terminal VARCHAR(20),
  ADD COLUMN IF NOT EXISTS gate VARCHAR(10),
  ADD COLUMN IF NOT EXISTS runway VARCHAR(10),
  ADD COLUMN IF NOT EXISTS airline_iata VARCHAR(3),
  ADD COLUMN IF NOT EXISTS airline_icao VARCHAR(3),
  ADD COLUMN IF NOT EXISTS airline_name VARCHAR(100),
  ADD COLUMN IF NOT EXISTS aircraft_reg VARCHAR(20),
  ADD COLUMN IF NOT EXISTS aircraft_model VARCHAR(50),
  ADD COLUMN IF NOT EXISTS is_cargo BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_codeshare BOOLEAN DEFAULT FALSE;
```

---

## Scheduling Strategy

### 10-Hour Cycle for 2-Hour Overlap

AeroDataBox enforces a hard **720-minute (12-hour) max window**. Run every **10 hours**:

| Run # | Time | Window Covered | Overlap |
|-------|------|----------------|---------|
| 1 | 00:00 | 00:00 → 12:00 | — |
| 2 | 10:00 | 10:00 → 22:00 | 10:00→12:00 (2h) |
| 3 | 20:00 | 20:00 → 08:00 | 20:00→22:00 (2h) |

Each moment is covered by at least **two runs**. If a run fails, the CronJob retries every 30 min.

### Implementation

The existing `download_config.next_run` mechanism supports this: set `next_run = now + 10 hours`. The CronJob (`*/30 * * * *`) stays unchanged — fires every 30 min but exits early if `next_run` hasn't been reached.

---

## Architecture

```
┌─────────────────────────────┐
│   CronJob (every 30 min)   │
│   Check next_run            │
│   ⤵ if due                  │
├─────────────────────────────┤
│   run.py                    │
│   ──────────                 │
│   1. aerodatabox.py (PRIMARY)│ ←── RapidAPI (AeroDataBox)
│   2. IF aero failed entirely │
│      → download_schedules() │ ←── FR24 + Avionio (FALLBACK)
│   3. Set next_run = now+10h │
└─────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│   flight_schedules (enriched with 12 new cols)  │
│   UPSERT on (airport_code, direction,           │
│             flight_number, route_airport,       │
│             scheduled_time)                     │
└─────────────────────────────────────────────────┘
```

---

## New Module: `build/schedule-downloader/aerodatabox.py`

### Key Logic

```python
def _derive_callsign(number, airline_icao):
    """Derive ICAO callsign when callSign field is absent.

    Example:
        number = "AF 3139", airline_icao = "AFR"
        → "AFR3139"
    """
    digits = "".join(filter(str.isdigit, number.replace(" ", "")))
    icao = (airline_icao or "").upper()
    return f"{icao}{digits}" if icao and digits else (number or "").replace(" ", "").upper()


def _normalize_number(number):
    """Strip space from flight number.

    Example: "AF 3139" → "AF3139"
    """
    return (number or "").replace(" ", "").upper()


def _parse_flights(raw, airport_icao, direction):
    flights = raw.get(direction, [])
    rows = []
    for f in flights:
        try:
            raw_number = f.get("number")
            if not raw_number:
                continue

            flight_number = _normalize_number(raw_number)

            if direction == "arrivals":
                leg = f.get("departure") or {}
                local_leg = f.get("arrival") or {}
            else:
                leg = f.get("arrival") or {}
                local_leg = f.get("departure") or {}

            leg_airport = (leg.get("airport") or {}).get("icao") or ""
            route = leg_airport.upper()

            scheduled = _extract_utc_dt(local_leg.get("scheduledTime"))
            revised = _extract_utc_dt(local_leg.get("revisedTime"))
            runway_t = _extract_utc_dt(local_leg.get("runwayTime"))

            status = _resolve_status(f.get("status"))

            anomaly = None
            if status in ("CANCELED", "CANCELED_UNCERTAIN"):
                anomaly = "CANCELLED"
            elif status == "DIVERTED":
                anomaly = "DIVERTED"

            aircraft = f.get("aircraft") or {}
            airline = f.get("airline") or {}

            # Use callSign if present, else derive from airline.icao + digits
            raw_callsign = f.get("callSign") or ""
            callsign = raw_callsign.upper() if raw_callsign else _derive_callsign(raw_number, airline.get("icao"))

            rows.append((
                airport_icao.upper(),
                direction.upper(),
                flight_number,
                callsign,
                (aircraft.get("modeS") or "").upper(),
                route,
                scheduled,
                revised,
                runway_t,
                status,
                (local_leg.get("terminal") or ""),
                (local_leg.get("gate") or ""),
                (local_leg.get("runway") or ""),
                (airline.get("iata") or "").upper(),
                (airline.get("icao") or "").upper(),
                airline.get("name") or "",
                (aircraft.get("reg") or "").upper(),
                aircraft.get("model") or "",
                bool(f.get("isCargo", False)),
                bool(f.get("codeshareStatus") == 2),
                anomaly,
            ))
        except Exception as e:
            logger.warning(f"Parse error in {airport_icao} {direction}: {e}")
            continue
    return rows
```

### `aerodatabox_download()` returns success count

```python
async def aerodatabox_download(db_pool, session, target_airports):
    """Returns number of airports successfully processed (0 = complete failure)."""
    ...
    return successful_count
```

---

## Integration Points

### 1. `run.py` — AeroDataBox primary, FR24/Avionio fallback

```python
from aerodatabox import aerodatabox_download

async def main():
    pool = await asyncpg.create_pool(...)
    db = AsyncDatabaseManager(pool)

    # Pre-check: skip if now < next_run
    next_run = await db.get_next_run()
    now_naive = datetime.now(IST).replace(tzinfo=None)
    if next_run is not None and now_naive < next_run.replace(tzinfo=None):
        logger.info(f"⏭️ Skipping: next_run at {next_run.isoformat()}")
        await db_pool.close()
        return

    async with aiohttp.ClientSession() as session:
        # 1. AeroDataBox (next 12 hours, high precision)
        aero_ok = await aerodatabox_download(pool, session, Config.TARGET_AIRPORTS)

        # 2. FR24 + Avionio (fallback only if AeroDataBox failed completely)
        if aero_ok == 0:
            logger.warning("AeroDataBox failed, falling back to FR24/Avionio")
            await download_schedules(db, session, {}, {})

    # 3. Schedule next run in 10 hours
    next_run_time = now_naive + timedelta(hours=10)
    await db.set_next_run(next_run_time, "SUCCESS")
    logger.info(f"📅 Next run scheduled at {next_run_time.isoformat()}")
    await db_pool.close()
```

### 2. `schedule-downloader.yaml` — Add RapidAPI key

```yaml
env:
  - name: RAPIDAPI_KEY
    valueFrom:
      secretKeyRef:
        name: aerodatabox-credentials
        key: rapidapi_key
```

### 3. `.env`

```
RAPIDAPI_KEY=184368c976msh0b1643fd198cb5dp1910abjsn2aafed71104c
```

### 4. `Dockerfile`

```dockerfile
COPY run.py aerodatabox.py route_schedule_downloader.py backfill_schedule.py config.py db.py ./
```

---

## Required Secret

```bash
kubectl create secret generic aerodatabox-credentials \
  -n bharatradar \
  --from-literal=rapidapi_key="184368c976msh0b1643fd198cb5dp1910abjsn2aafed71104c"
```

---

## Deployment Plan (2-Day Test)

### Phase 1: Apply + Start

1. ✅ Migration already run on production DB (12 new columns added)
2. ✅ Secret already created
3. ✅ Image built and pushed
4. ⬜ Apply updated CronJob manifest → next cron tick starts the test

### Phase 2: Monitor

- First run: ~3 min for 35 airports (5s delay between each)
- Check logs: `kubectl logs -n bharatradar -l job-name=schedule-downloader-XXXX`
- Verify data in DB: `SELECT status, count(*) FROM flight_schedules WHERE created_from = 'AERODATABOX' GROUP BY status;`
- Estimated usage: 35 × 2 units = **70 units per run**
- Free plan budget: **600 units** → ~**8 runs** (~3.3 days) before Pro needed

### Phase 3: Upgrade to Pro

- Update the API key in `.env` and K8s Secret
- No code changes needed

---

## Files Modified

| Action | File | Description |
|--------|------|-------------|
| **Create** | `build/schedule-downloader/aerodatabox.py` | AeroDataBox API client module |
| **Modify** | `build/schedule-downloader/run.py` | AeroDataBox primary, FR24/Avionio fallback, +10h next_run |
| **Modify** | `build/schedule-downloader/Dockerfile` | Add aerodatabox.py to COPY |
| **Modify** | `manifests/default/schedule-downloader.yaml` | Add `RAPIDAPI_KEY` env var |
| **Modify** | `.env` | Add `RAPIDAPI_KEY` |
| **Create** | `scripts/db/postgres/migration-v3.sql` | 12 new columns |
| **Modify** | `scripts/db/postgres/schema.sql` | Add columns to CREATE TABLE |

---

## API Unit Budget (2-Day Test on Free Plan)

| Metric | Value |
|--------|-------|
| Airports | 35 |
| Units per run | 70 (35 × 2) |
| Runs in 2 days (10h cycle) | ~5 |
| **Total units** | **~350** |
| Free plan limit | 600 |
| **Buffer** | **250 units** |

---

## Error Handling Strategy

| Scenario | Action |
|----------|--------|
| Airport A times out | Skip, log error, continue with B (5s delay unaffected) |
| RapidAPI 429 (rate limit) | Log error, skip remaining airports this run |
| Response parse error | Log raw response snippet, skip single flight |
| DB connection lost | Retry connection, fail run cleanly |
| All 35 fail | Run falls back to FR24/Avionio |
| CronJob retry on failure | Cron fires every 30min, `next_run` not updated → auto-retries |
