import os
import logging
from datetime import datetime, timedelta, timezone
from route_schedule_downloader import download_schedules, compute_next_run_time
import config as sched_config

IST = timezone(timedelta(hours=5, minutes=30))
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import asyncio
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

        # Pre-check: skip if now < next_run
        next_run = await db.get_next_run()
        now_naive = datetime.now(IST).replace(tzinfo=None)
        if next_run is not None and now_naive < next_run.replace(tzinfo=None):
            logger.info(f"⏭️ Skipping: next_run at {next_run.isoformat()}, current time {now_naive.isoformat()}")
            await db_pool.close()
            return

        # Always fetch both today and tomorrow to catch incomplete same-day data
        sched_config.Config.GET_SCHEDULES_FOR = ['TODAY', 'TOMORROW']
        logger.info("📅 Fetching schedules for: TODAY, TOMORROW")

        async with aiohttp.ClientSession() as session:
            await download_schedules(db, session, {}, {})

        # Post-download: compute and store next_run
        next_run_time = await compute_next_run_time(db)
        await db.set_next_run(next_run_time, "SUCCESS")
        logger.info(f"📅 Next run scheduled at {next_run_time.isoformat()}")
        await db_pool.close()
    
    asyncio.run(main())