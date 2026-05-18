import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from route_schedule_downloader import download_schedules
from aerodatabox import aerodatabox_download
import config as sched_config

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import aiohttp
    import asyncpg
    from db import AsyncDatabaseManager

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    async def main():
        db_pool = await asyncpg.create_pool(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            database=os.environ.get("DB_NAME", "flight_db"),
            user=os.environ.get("DB_USER", "flight_db_user"),
            password=os.environ.get("DB_PASSWORD", "flight_db_password")
        )
        db = AsyncDatabaseManager(db_pool)

        # Check if scheduler is enabled
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT scheduler_enabled FROM download_config WHERE id = 1")
            if not row or not row["scheduler_enabled"]:
                logger.info("⏸️ Scheduler disabled (scheduler_enabled = FALSE), exiting")
                db_pool.terminate()
                return

        # Pre-check: skip if now < next_run
        next_run = await db.get_next_run()
        now_utc = datetime.now(timezone.utc)
        if next_run is not None:
            next_run_naive = next_run.replace(tzinfo=None) if next_run.tzinfo else next_run
            if now_utc.replace(tzinfo=None) < next_run_naive:
                logger.info(f"⏭️ Skipping: next_run at {next_run_naive.isoformat()}, current UTC {now_utc.isoformat()}")
                db_pool.terminate()
                return

        # Always fetch both today and tomorrow to catch incomplete same-day data
        sched_config.Config.GET_SCHEDULES_FOR = ['TODAY', 'TOMORROW']
        logger.info("📅 Fetching schedules for: TODAY, TOMORROW")

        async with aiohttp.ClientSession() as session:
            # 1. AeroDataBox (next 12 hours, high precision) — PRIMARY
            aero_ok = await aerodatabox_download(db_pool, session, sched_config.Config.TARGET_AIRPORTS)

            # 2. FR24 + Avionio — FALLBACK only if AeroDataBox failed completely
            if aero_ok == 0:
                logger.warning("AeroDataBox failed entirely, falling back to FR24/Avionio")
                await download_schedules(db, session, {}, {})

        # Post-download: update last_run, schedule next run in 10 hours
        next_run_time = now_utc + timedelta(hours=10)
        await db.set_next_run(next_run_time, "SUCCESS")
        last_run_naive = now_utc.replace(tzinfo=None)
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE download_config SET last_run = $1 WHERE id = 1", last_run_naive)
        logger.info(f"📅 Last run updated, next run at {next_run_time.isoformat()}")
        db_pool.terminate()

    asyncio.run(main())
