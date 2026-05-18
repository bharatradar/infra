import os
import re
import shutil
import time
import hashlib
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import send_telegram_message

RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"
AERODATABOX_LOG_DIR = "/data/aerodatabox"
RAW_LOG_RETENTION_HOURS = 72

logger = logging.getLogger(__name__)

_key_last_limits = {}
_key_failures = set()

FREE_TIER_DELAY = 10
PRO_TIER_DELAY = 5


def _load_rapidapi_keys():
    keys = []
    legacy = os.environ.get("RAPIDAPI_KEY", "")
    if legacy:
        keys.append({
            "key": legacy,
            "hash": hashlib.sha256(legacy.encode()).hexdigest(),
            "tier": "unknown",
            "key_name": "rapidapi_key_0",
        })
        logger.info(f"Loaded RAPIDAPI_KEY (legacy) — hash={keys[0]['hash'][:12]}...")
    i = 1
    while True:
        key = os.environ.get(f"RAPIDAPI_KEY_{i}")
        if not key:
            break
        if not any(k["key"] == key for k in keys):
            keys.append({
                "key": key,
                "hash": hashlib.sha256(key.encode()).hexdigest(),
                "tier": "unknown",
                "key_name": f"rapidapi_key_{i}",
            })
            logger.info(f"Loaded RAPIDAPI_KEY_{i} — hash={keys[-1]['hash'][:12]}...")
        i += 1
    logger.info(f"Total RapidAPI keys loaded: {len(keys)}")
    return keys


RAPIDAPI_KEYS = _load_rapidapi_keys()


def _get_active_keys():
    return [k for k in RAPIDAPI_KEYS if k["hash"] not in _key_failures]


def _get_current_key():
    active = _get_active_keys()
    if not active:
        logger.warning("All RapidAPI keys exhausted — resetting failures and retrying from start")
        _key_failures.clear()
        active = RAPIDAPI_KEYS
    return active[0]


def _mark_key_failed(key_hash):
    key_info = next((k for k in RAPIDAPI_KEYS if k["hash"] == key_hash), None)
    tier = "unknown"
    if key_info:
        limits = _key_last_limits.get(key_hash, {})
        tier = limits.get("tier", "unknown")
    logger.warning(f"Key {key_hash[:12]}... ({tier}) marked as failed this run")
    _key_failures.add(key_hash)


def _get_delay():
    key = _get_current_key()
    if not key:
        return PRO_TIER_DELAY
    limits = _key_last_limits.get(key["hash"], {})
    tier = limits.get("tier", "unknown")
    return FREE_TIER_DELAY if tier == "free" else PRO_TIER_DELAY


def _rotate_on_status(resp_status, key_hash):
    if resp_status in (401, 403, 429):
        _mark_key_failed(key_hash)
        return True
    return False


def _capture_rate_limits(resp, key_hash):
    limits = {}
    limits["tier"] = (resp.headers.get("x-tier") or "unknown").lower()
    raw = resp.headers.get("x-ratelimit-api-units-limit")
    if raw:
        limits["units_limit"] = int(raw)
    raw = resp.headers.get("x-ratelimit-api-units-remaining")
    if raw:
        limits["units_remaining"] = int(raw)
    raw = resp.headers.get("x-ratelimit-api-units-reset")
    if raw:
        limits["units_reset"] = int(raw)
    raw = resp.headers.get("x-ratelimit-requests-limit")
    if raw:
        limits["requests_limit"] = int(raw)
    raw = resp.headers.get("x-ratelimit-requests-remaining")
    if raw:
        limits["requests_remaining"] = int(raw)
    _key_last_limits[key_hash] = limits
    return limits


STATUS_INT_MAP = {
    0: "UNKNOWN",
    1: "EXPECTED",
    2: "EN_ROUTE",
    3: "CHECK_IN",
    4: "BOARDING",
    5: "GATE_CLOSED",
    6: "DEPARTED",
    7: "DELAYED",
    8: "APPROACHING",
    9: "ARRIVED",
    10: "CANCELED",
    11: "DIVERTED",
    12: "CANCELED_UNCERTAIN",
}

STATUS_STR_MAP = {
    "unknown": "UNKNOWN",
    "expected": "EXPECTED",
    "enroute": "EN_ROUTE",
    "en_route": "EN_ROUTE",
    "checkin": "CHECK_IN",
    "check_in": "CHECK_IN",
    "boarding": "BOARDING",
    "gateclosed": "GATE_CLOSED",
    "gate_closed": "GATE_CLOSED",
    "departed": "DEPARTED",
    "delayed": "DELAYED",
    "approaching": "APPROACHING",
    "arrived": "ARRIVED",
    "canceled": "CANCELED",
    "cancelled": "CANCELED",
    "diverted": "DIVERTED",
    "canceleduncertain": "CANCELED_UNCERTAIN",
    "canceled_uncertain": "CANCELED_UNCERTAIN",
}


def _resolve_status(raw_status):
    if raw_status is None:
        return None
    if isinstance(raw_status, int):
        return STATUS_INT_MAP.get(raw_status)
    if isinstance(raw_status, str):
        s = raw_status.strip().lower().replace(" ", "_")
        return STATUS_STR_MAP.get(s)
    return None


def _cleanup_old_logs():
    if not os.path.exists(AERODATABOX_LOG_DIR):
        return
    now = time.time()
    cutoff = now - RAW_LOG_RETENTION_HOURS * 3600
    for entry in os.listdir(AERODATABOX_LOG_DIR):
        path = os.path.join(AERODATABOX_LOG_DIR, entry)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            logger.info(f"🗑️ Cleaned up old raw log: {entry}")


def _normalize_number(number):
    return "".join(number.upper().split())


def _derive_callsign(number, airline_icao):
    icao = (airline_icao or "").upper()
    number = (number or "").upper()
    return f"{icao}{number[2:]}" if len(number) > 2 else number


def _extract_utc_dt(obj, key="utc"):
    if not obj:
        return None
    t = obj.get(key)
    if t:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            return None
    return None


def _parse_flights(raw, airport_icao, direction):
    flights = raw.get(direction, [])
    if not flights:
        return []

    rows = []
    for f in flights:
        try:
            number = f.get("number")
            if not number:
                continue
            number = _normalize_number(number)

            callsign_raw = (f.get("callSign") or "").upper()
            airline_icao = ((f.get("airline") or {}).get("icao") or "").upper()
            if not callsign_raw:
                callsign_raw = _derive_callsign(number, airline_icao)

            if direction == "arrivals":
                leg = f.get("departure") or {}
                local_leg = f.get("arrival") or {}
            else:
                leg = f.get("arrival") or {}
                local_leg = f.get("departure") or {}

            leg_airport = (leg.get("airport") or {}).get("icao") or ""
            route = leg_airport.upper()

            movement = f.get("movement") or {}
            scheduled = _extract_utc_dt(local_leg.get("scheduledTime"))
            revised = _extract_utc_dt(local_leg.get("revisedTime"))
            runway_t = _extract_utc_dt(local_leg.get("runwayTime"))

            status = _resolve_status(f.get("status"))

            aircraft = f.get("aircraft") or {}
            mode_s = (aircraft.get("modeS") or "").upper()

            anomaly = None
            if status in ("CANCELED", "CANCELED_UNCERTAIN"):
                anomaly = "CANCELLED"
            elif status == "DIVERTED":
                anomaly = "DIVERTED"
            elif not mode_s:
                anomaly = "MISSING_MODE_S"

            airline = f.get("airline") or {}

            rows.append((
                airport_icao.upper(),
                direction.upper(),
                number,
                callsign_raw,
                mode_s,
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


async def _health_check(session, api_key, key_hash):
    url = f"https://{RAPIDAPI_HOST}/health/services/feeds/FlightSchedules"
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            _capture_rate_limits(resp, key_hash)
            if resp.status == 403:
                logger.warning(f"Health check HTTP 403 for {key_hash[:12]}... (key may still work for data)")
                return True
            if resp.status != 200:
                logger.warning(f"Health check returned HTTP {resp.status}")
                return False
            data = await resp.json()
            ok = data.get("status") == "OK"
            if ok:
                limits = _key_last_limits.get(key_hash, {})
                logger.info(f"AeroDataBox health check: OK (tier={limits.get('tier', '?')}, "
                            f"units={limits.get('units_remaining', '?')}/{limits.get('units_limit', '?')})")
            return ok
    except Exception as e:
        logger.warning(f"Health check failed: {e}")
        return False


async def fetch_airport(session, iata, api_key, key_hash):
    url = f"https://{RAPIDAPI_HOST}/flights/airports/iata/{iata}"
    params = {
        "offsetMinutes": "0",
        "durationMinutes": "720",
        "withLeg": "true",
        "direction": "Both",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "false",
        "withPrivate": "false",
        "withLocation": "false",
    }
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }
    prev_limits = _key_last_limits.get(key_hash, {})
    prev_remaining = prev_limits.get("units_remaining")
    async with session.get(url, params=params, headers=headers, timeout=30) as resp:
        _capture_rate_limits(resp, key_hash)
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:300]}")
        data = await resp.json()
    new_limits = _key_last_limits.get(key_hash, {})
    new_remaining = new_limits.get("units_remaining")
    units_used = max(0, (prev_remaining or 0) - (new_remaining or 0)) if prev_remaining is not None else 0
    return data, units_used


async def aerodatabox_download(db_pool, session, target_airports):
    if not RAPIDAPI_KEYS:
        logger.warning("No RapidAPI keys configured, skipping AeroDataBox download")
        return 0

    if not await _validate_any_key(session):
        logger.warning("All RapidAPI keys failed health check, skipping run")
        return 0

    logger.info("=" * 50)
    logger.info("AeroDataBox Schedule Download Starting")
    logger.info("=" * 50)

    _cleanup_old_logs()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_log_dir = os.path.join(AERODATABOX_LOG_DIR, ts)
    os.makedirs(run_log_dir, exist_ok=True)

    all_rows = []
    usage_logs = []
    api_errors = 0
    airports_ok = 0
    total_airports = len(target_airports)

    for idx, (icao, data) in enumerate(target_airports.items(), 1):
        iata = (data.get("iata") or "").upper()
        if not iata:
            continue

        logger.info(f"[{idx}/{total_airports}] Fetching {icao} ({iata})...")

        airport_has_rows = False
        max_attempts = max(len(RAPIDAPI_KEYS), 1) * 2
        for attempt in range(max_attempts):
            key = _get_current_key()
            try:
                raw, units_used = await fetch_airport(session, iata, key["key"], key["hash"])
                with open(os.path.join(run_log_dir, f"{icao}.json"), "w") as f:
                    json.dump(raw, f, indent=2)
                usage_logs.append({
                    "endpoint": f"/flights/airports/iata/{iata}",
                    "airport_code": icao.upper(),
                    "key_name": key["key_name"],
                    "units_used": units_used,
                    "status_code": 200,
                })
                for direction in ("arrivals", "departures"):
                    rows = _parse_flights(raw, icao, direction)
                    all_rows.extend(rows)
                    if rows:
                        airport_has_rows = True
                    logger.info(f"  {direction}: {len(rows)} flights")
                if airport_has_rows:
                    airports_ok += 1
                break
            except RuntimeError as e:
                err_str = str(e)
                if any(code in err_str for code in ("401", "403", "429")):
                    _mark_key_failed(key["hash"])
                    logger.warning(f"Key {key['hash'][:12]}... failed ({err_str[:60]}), rotating key")
                    continue
                api_errors += 1
                logger.error(f"[AERODATABOX] Failed {icao} ({iata}): {e}")
                break
            except Exception as e:
                api_errors += 1
                logger.error(f"[AERODATABOX] Failed {icao} ({iata}): {e}")
                break

        if idx < total_airports:
            delay = _get_delay()
            await asyncio.sleep(delay)

    logger.info(f"Total flights parsed: {len(all_rows)} "
                f"(airports_ok={airports_ok}, api_errors={api_errors})")

    if not all_rows:
        logger.warning("No flights to upsert")
        return airports_ok

    async with db_pool.acquire() as conn:
        await conn.executemany("""
            INSERT INTO flight_schedules
                (airport_code, direction, flight_number, callsign, hex_id,
                 route_airport, scheduled_time, estimated_time, actual_time,
                 status, terminal, gate, runway,
                 airline_iata, airline_icao, airline_name,
                 aircraft_reg, aircraft_model, is_cargo, is_codeshare,
                 anomaly_flag, created_from, updated_from)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15, $16,
                    $17, $18, $19, $20, $21, 'AERODATABOX', 'AERODATABOX')
            ON CONFLICT (airport_code, direction, flight_number, route_airport, (COALESCE(scheduled_time, 'epoch'::timestamp)))
            DO UPDATE SET
                status = COALESCE(EXCLUDED.status, flight_schedules.status),
                estimated_time = COALESCE(EXCLUDED.estimated_time, flight_schedules.estimated_time),
                actual_time = COALESCE(EXCLUDED.actual_time, flight_schedules.actual_time),
                terminal = COALESCE(EXCLUDED.terminal, flight_schedules.terminal),
                gate = COALESCE(EXCLUDED.gate, flight_schedules.gate),
                runway = COALESCE(EXCLUDED.runway, flight_schedules.runway),
                airline_iata = COALESCE(EXCLUDED.airline_iata, flight_schedules.airline_iata),
                airline_icao = COALESCE(EXCLUDED.airline_icao, flight_schedules.airline_icao),
                airline_name = COALESCE(EXCLUDED.airline_name, flight_schedules.airline_name),
                aircraft_reg = COALESCE(EXCLUDED.aircraft_reg, flight_schedules.aircraft_reg),
                aircraft_model = COALESCE(EXCLUDED.aircraft_model, flight_schedules.aircraft_model),
                is_cargo = COALESCE(EXCLUDED.is_cargo, flight_schedules.is_cargo),
                is_codeshare = COALESCE(EXCLUDED.is_codeshare, flight_schedules.is_codeshare),
                anomaly_flag = COALESCE(EXCLUDED.anomaly_flag, flight_schedules.anomaly_flag),
                updated_from = 'AERODATABOX'
        """, all_rows)

    logger.info(f"AeroDataBox download complete: {len(all_rows)} rows upserted")

    if usage_logs:
        async with db_pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO api_usage_log (endpoint, airport_code, key_name, units_used, status_code)
                VALUES ($1, $2, $3, $4, $5)
            """, [(r["endpoint"], r["airport_code"], r["key_name"], r["units_used"], r["status_code"])
                  for r in usage_logs])
        total_units = sum(r["units_used"] for r in usage_logs)
        logger.info(f"Logged {len(usage_logs)} API calls, {total_units} units consumed")

    await _update_usage_tracking(db_pool, session, airports_ok, api_errors)

    return airports_ok


async def _validate_any_key(session):
    for key in RAPIDAPI_KEYS:
        if key["hash"] in _key_failures:
            continue
        if await _health_check(session, key["key"], key["hash"]):
            limits = _key_last_limits.get(key["hash"], {})
            logger.info(f"Key {key['hash'][:12]}... ({limits.get('tier', '?')}) passed health check")
            return True
        _mark_key_failed(key["hash"])
        logger.warning(f"Key {key['hash'][:12]}... failed health check")
    return False


async def _update_usage_tracking(db_pool, session, airports_ok, api_errors):
    airports_queried = airports_ok + api_errors
    units_cost = int(os.environ.get("RAPIDAPI_UNIT_COST", "2"))
    units_used_run = airports_queried * units_cost

    current_key_hashes = [k["hash"] for k in RAPIDAPI_KEYS]
    current_hashes_set = set(current_key_hashes)

    key_metadata = []
    for k in RAPIDAPI_KEYS:
        limits = _key_last_limits.get(k["hash"], {})
        entry = {
            "key_name": k["key_name"],
            "hash": k["hash"],
            "tier": limits.get("tier", "unknown"),
            "active": True,
        }
        for field in ("units_limit", "units_remaining", "units_reset", "requests_limit", "requests_remaining"):
            if field in limits:
                entry[field] = limits[field]
        key_metadata.append(entry)

    total_units_limit = sum(e.get("units_limit", 0) for e in key_metadata if "units_limit" in e)
    total_units_remaining = sum(e.get("units_remaining", 0) for e in key_metadata if "units_remaining" in e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE download_config SET "
            "rapidapi_keys = $1::jsonb, "
            "rapidapi_key_hash = $2, "
            "rapidapi_units_limit = $3, "
            "rapidapi_units_used = GREATEST(rapidapi_units_used, $4), "
            "updated_at = NOW() "
            "WHERE id = 1",
            json.dumps(key_metadata),
            current_key_hashes[0] if current_key_hashes else "",
            total_units_limit,
            total_units_limit - total_units_remaining,
        )

        remaining = total_units_limit - total_units_remaining - units_used_run
        daily_burn = int(os.environ.get("RAPIDAPI_DAILY_BURN", "280"))
        alert_days = int(os.environ.get("RAPIDAPI_ALERT_DAYS", "23"))
        days_remaining = remaining / max(daily_burn, 1)
        now = datetime.now(timezone.utc)
        cooldown = timedelta(hours=12)
        should_alert = remaining <= 0 or days_remaining < alert_days

        row = await conn.fetchrow(
            "SELECT rapidapi_last_alert_at FROM download_config WHERE id = 1"
        )
        last_alert = row["rapidapi_last_alert_at"] if row else None

        if should_alert and (last_alert is None or (now - last_alert) > cooldown):
            pct = remaining / max(total_units_limit, 1) * 100
            msg = (
                "\u26a0\ufe0f *AeroDataBox API Limit Alert*\n"
                f"Used: ~{total_units_limit - remaining}/{total_units_limit} units\n"
                f"Remaining: ~{remaining} ({pct:.0f}%) — ~{days_remaining:.0f} days left\n"
                f"Threshold: Alert when < {alert_days} days remaining\n\n"
                f"Please add more RapidAPI keys to avoid interruption."
            )
            logger.warning(f"Low API units: ~{days_remaining:.0f} days remaining. Alert sent.")
            await send_telegram_message(session, msg)
            await conn.execute(
                "UPDATE download_config SET rapidapi_last_alert_at = NOW() "
                "WHERE id = 1"
            )
        elif not should_alert:
            pct = (total_units_limit - total_units_remaining) / max(total_units_limit, 1) * 100
            logger.info(f"RapidAPI usage: ~{total_units_limit - total_units_remaining}/{total_units_limit} "
                        f"({pct:.0f}%, ~{days_remaining:.0f} days left)")
        else:
            logger.warning(f"Low API units (~{days_remaining:.0f} days left), alert suppressed (cooldown)")


async def test_single_airport(db_pool, session, icao, iata):
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST MODE: Fetching single airport {icao} ({iata})")
    logger.info(f"{'='*60}")

    if not RAPIDAPI_KEYS:
        logger.warning("No RapidAPI keys configured")
        return 0

    key = _get_current_key()
    raw, _ = await fetch_airport(session, iata, key["key"], key["hash"])

    total = 0
    for direction in ("arrivals", "departures"):
        rows = _parse_flights(raw, icao, direction)
        total += len(rows)
        logger.info(f"\n{direction.upper()}: {len(rows)} flights")
        for r in rows[:5]:
            logger.info(
                f"  {r[3]:8s} | {r[0]:4s} {r[1]:10s} | "
                f"flight={r[2]:7s} route={r[5]:4s} | "
                f"sched={r[6]} status={r[9]} gate={r[11]}"
            )
        if len(rows) > 5:
            logger.info(f"  ... and {len(rows) - 5} more")

    logger.info(f"\nTotal flights: {total}")

    if total > 0:
        logger.info("\nInserting into database...")
        async with db_pool.acquire() as conn:
            for direction in ("arrivals", "departures"):
                rows = _parse_flights(raw, icao, direction)
                if not rows:
                    continue
                await conn.executemany("""
                    INSERT INTO flight_schedules
                        (airport_code, direction, flight_number, callsign, hex_id,
                         route_airport, scheduled_time, estimated_time, actual_time,
                         status, terminal, gate, runway,
                         airline_iata, airline_icao, airline_name,
                         aircraft_reg, aircraft_model, is_cargo, is_codeshare,
                         anomaly_flag, created_from, updated_from)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13, $14, $15, $16,
                            $17, $18, $19, $20, $21, 'AERODATABOX', 'AERODATABOX')
                    ON CONFLICT (airport_code, direction, flight_number, route_airport, (COALESCE(scheduled_time, 'epoch'::timestamp)))
                    DO UPDATE SET
                        status = COALESCE(EXCLUDED.status, flight_schedules.status),
                        estimated_time = COALESCE(EXCLUDED.estimated_time, flight_schedules.estimated_time),
                        terminal = COALESCE(EXCLUDED.terminal, flight_schedules.terminal),
                        gate = COALESCE(EXCLUDED.gate, flight_schedules.gate),
                        updated_from = 'AERODATABOX'
                """, rows)
        logger.info("Database insert complete")
    else:
        logger.warning("No flights found — nothing to insert")

    return total


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s IST - %(levelname)s - %(message)s",
    )

    import asyncpg
    import aiohttp

    async def main():
        pool = await asyncpg.create_pool(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            database=os.environ.get("DB_NAME", "flight_db"),
            user=os.environ.get("DB_USER", "flight_db_user"),
            password=os.environ.get("DB_PASSWORD", "raga@098"),
        )

        async with aiohttp.ClientSession() as session:
            await test_single_airport(pool, session, "VIDP", "DEL")

        await pool.close()

    asyncio.run(main())
