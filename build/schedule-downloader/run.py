import os
from route_schedule_downloader import download_schedules

if __name__ == "__main__":
    import asyncio
    import aiohttp
    import asyncpg
    from db import AsyncDatabaseManager
    
    async def main():
        db_pool = await asyncpg.create_pool(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            database=os.environ.get("DB_NAME", "flight_db"),
            user=os.environ.get("DB_USER", "flight_db_user"),
            password=os.environ.get("DB_PASSWORD", "flight_db_password")
        )
        db = AsyncDatabaseManager(db_pool)
        async with aiohttp.ClientSession() as session:
            await download_schedules(db, session, {}, {})
        await db_pool.close()
    
    asyncio.run(main())