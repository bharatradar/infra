import asyncio
import asyncpg
import logging
from config import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def resolve_airport_codes(conn, airport: str):
    """Helper to translate ICAO/IATA codes for accurate historical matching."""
    icao_code, iata_code = airport.upper(), airport.upper()
    
    # 1. Check local static target config
    for iata, data in getattr(Config, 'TARGET_AIRPORTS', {}).items():
        if airport.upper() == iata or airport.upper() == data.get('icao', ''):
            return data.get('icao', airport.upper()), iata
            
    # 2. Check Database Airports Table
    try:
        ap_row = await conn.fetchrow("SELECT icao, iata FROM airports WHERE icao = $1 OR iata = $1", airport.upper())
        if ap_row:
            return ap_row.get('icao') or icao_code, ap_row.get('iata') or iata_code
    except Exception: pass
    
    return icao_code, iata_code

async def run_backfill():
    logger.info("🚀 Starting Historical Schedule Data Enrichment...")
    
    try:
        pool = await asyncpg.create_pool(**Config.DB_PARAMS)
        
        async with pool.acquire() as conn:
            # 1. Find all schedule rows that haven't been enriched yet
            schedules = await conn.fetch("""
                SELECT id, airport_code, direction, callsign, scheduled_time 
                FROM flight_schedules 
                WHERE actual_time IS NULL
            """)
            
            logger.info(f"📋 Found {len(schedules)} schedule records missing actual_time/hex_id.")
            
            updated_count = 0
            
            for sched in schedules:
                sched_id = sched['id']
                ap_code = sched['airport_code']
                direction = sched['direction']
                callsign = sched['callsign']
                sched_time = sched['scheduled_time']
                
                # Determine which log table to check
                log_table = "arrivals_log" if direction == "ARRIVALS" else "departures_log"
                
                # Translate airport codes (e.g. PNQ -> VAPO and PNQ)
                icao, iata = await resolve_airport_codes(conn, ap_code)
                
                # Find the closest physical radar event in the log tables
                query = f"""
                    SELECT hex_id, timestamp 
                    FROM {log_table}
                    WHERE callsign = $1 
                      AND (airport = $2 OR airport = $3)
                      AND timestamp >= $4 - INTERVAL '12 hours'
                      AND timestamp <= $4 + INTERVAL '12 hours'
                    ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - $4))) ASC 
                    LIMIT 1
                """
                
                matched_log = await conn.fetchrow(query, callsign, icao, iata, sched_time)
                
                if matched_log:
                    # We found the historical radar event! Bind it to the schedule.
                    await conn.execute("""
                        UPDATE flight_schedules 
                        SET actual_time = $1, hex_id = $2 
                        WHERE id = $3
                    """, matched_log['timestamp'], matched_log['hex_id'], sched_id)
                    
                    updated_count += 1
                    if updated_count % 50 == 0:
                        logger.info(f"✅ Successfully enriched {updated_count} records so far...")

            logger.info(f"🎉 Backfill Complete! Successfully linked {updated_count} out of {len(schedules)} physical flights to the master schedule.")
            
    except Exception as e:
        logger.error(f"❌ Error during backfill: {e}")
    finally:
        if 'pool' in locals():
            await pool.close()

if __name__ == "__main__":
    asyncio.run(run_backfill())