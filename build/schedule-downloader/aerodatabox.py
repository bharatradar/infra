import os
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import send_telegram_message

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"

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

REQUEST_DELAY_SEC = 5

logger = logging.getLogger(__name__)


def _normalize_number(number):
    digits = "".join(filter(str.isdigit, number))
    alpha = "".join(filter(str.isalpha, number))
    return f"{alpha}{digits}".upper()


def _derive_callsign(number, airline_icao):
    digits = "".join(filter(str.isdigit, number))
    icao = (airline_icao or "").upper()
    return f"{icao}{digits}" if icao and digits else _normalize_number(number)


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

            anomaly = None
            if status in ("CANCELED", "CANCELED_UNCERTAIN"):
                anomaly = "CANCELLED"
            elif status == "DIVERTED":
                anomaly = "DIVERTED"

            aircraft = f.get("aircraft") or {}
            airline = f.get("airline") or {}

            rows.append((
                airport_icao.upper(),
                direction.upper(),
                number,
                callsign_raw,
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


async def _health_check(session):
    url = f"https://{RAPIDAPI_HOST}/health/services/feeds/FlightSchedules"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
            ok = data.get("status") == "OK"
            if ok:
                logger.info("AeroDataBox health check: OK")
            return ok
    except Exception as e:
        logger.warning(f"Health check failed: {e}")
        return False


async def fetch_airport(session, iata):
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
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }
    async with session.get(url, params=params, headers=headers, timeout=30) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:300]}")
        return await resp.json()


async def aerodatabox_download(db_pool, session, target_airports):
    if not RAPIDAPI_KEY:
        logger.warning("RAPIDAPI_KEY not set, skipping AeroDataBox download")
        return 0

    if not await _health_check(session):
        logger.warning("AeroDataBox FlightSchedules feed unhealthy, skipping run")
        return 0

    logger.info("=" * 50)
    logger.info("AeroDataBox Schedule Download Starting")
    logger.info("=" * 50)

    all_rows = []
    api_errors = 0
    airports_ok = 0
    total_airports = len(target_airports)

    for idx, (icao, data) in enumerate(target_airports.items(), 1):
        iata = (data.get("iata") or "").upper()
        if not iata:
            continue

        logger.info(f"[{idx}/{total_airports}] Fetching {icao} ({iata})...")

        airport_has_rows = False
        try:
            raw = await fetch_airport(session, iata)
            for direction in ("arrivals", "departures"):
                rows = _parse_flights(raw, icao, direction)
                all_rows.extend(rows)
                if rows:
                    airport_has_rows = True
                logger.info(f"  {direction}: {len(rows)} flights")
            if airport_has_rows:
                airports_ok += 1
        except Exception as e:
            api_errors += 1
            logger.error(f"[AERODATABOX] Failed {icao} ({iata}): {e}")
            if idx < total_airports:
                await asyncio.sleep(REQUEST_DELAY_SEC)
            continue

        if idx < total_airports:
            await asyncio.sleep(REQUEST_DELAY_SEC)

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
            ON CONFLICT (airport_code, direction, flight_number, route_airport, scheduled_time)
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

    airports_queried = airports_ok + api_errors
    units_cost = int(os.environ.get("RAPIDAPI_UNIT_COST", "2"))
    units_used_run = airports_queried * units_cost
    current_key_hash = hashlib.sha256(RAPIDAPI_KEY.encode()).hexdigest() if RAPIDAPI_KEY else ""

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rapidapi_units_used, rapidapi_units_limit, "
            "rapidapi_daily_burn, rapidapi_alert_days, "
            "rapidapi_key_hash, rapidapi_last_alert_at "
            "FROM download_config WHERE id = 1"
        )
        if not row:
            return airports_ok

        prev_used = row["rapidapi_units_used"] or 0
        prev_limit = row["rapidapi_units_limit"] or int(os.environ.get("RAPIDAPI_UNITS_LIMIT", "600"))
        daily_burn = row["rapidapi_daily_burn"] or int(os.environ.get("RAPIDAPI_DAILY_BURN", "280"))
        alert_days = row["rapidapi_alert_days"] or int(os.environ.get("RAPIDAPI_ALERT_DAYS", "23"))
        stored_key_hash = row["rapidapi_key_hash"] or ""
        last_alert = row["rapidapi_last_alert_at"]

        if current_key_hash and stored_key_hash and current_key_hash != stored_key_hash:
            logger.info("New API key detected — resetting usage tracking")
            prev_used = 0
            await conn.execute(
                "UPDATE download_config SET rapidapi_units_used = 0, "
                "rapidapi_key_hash = $1, updated_at = NOW() WHERE id = 1",
                current_key_hash,
            )

        new_used = prev_used + units_used_run
        await conn.execute(
            "UPDATE download_config SET rapidapi_units_used = $1, "
            "updated_at = NOW() WHERE id = 1",
            new_used,
        )

        remaining = prev_limit - new_used
        days_remaining = remaining / max(daily_burn, 1)
        now = datetime.utcnow()
        cooldown = timedelta(hours=12)
        should_alert = remaining <= 0 or days_remaining < alert_days

        if should_alert and (last_alert is None or (now - last_alert) > cooldown):
            pct = remaining / max(prev_limit, 1) * 100
            msg = (
                "\u26a0\ufe0f *AeroDataBox API Limit Alert*\n"
                f"Used: ~{new_used}/{prev_limit} units\n"
                f"Remaining: ~{remaining} ({pct:.0f}%) — ~{days_remaining:.0f} days left\n"
                f"Threshold: Alert when < {alert_days} days remaining\n\n"
                f"Please upgrade the RapidAPI plan to avoid interruption."
            )
            logger.warning(f"Low API units: ~{days_remaining:.0f} days remaining. Alert sent.")
            await send_telegram_message(session, msg)
            await conn.execute(
                "UPDATE download_config SET rapidapi_last_alert_at = NOW() "
                "WHERE id = 1"
            )
        elif not should_alert:
            pct = new_used / max(prev_limit, 1) * 100
            logger.info(f"RapidAPI usage: ~{new_used}/{prev_limit} ({pct:.0f}%, ~{days_remaining:.0f} days left)")
        else:
            logger.warning(f"Low API units (~{days_remaining:.0f} days left), alert suppressed (cooldown)")

    return airports_ok


async def test_single_airport(db_pool, session, icao, iata):
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST MODE: Fetching single airport {icao} ({iata})")
    logger.info(f"{'='*60}")

    raw = await fetch_airport(session, iata)

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
                    ON CONFLICT (airport_code, direction, flight_number, route_airport, scheduled_time)
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
