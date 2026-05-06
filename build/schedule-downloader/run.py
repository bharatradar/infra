from route_schedule_downloader import download_schedules

if __name__ == "__main__":
    import asyncio
    import aiohttp
    import asyncpg
    from db import AsyncDatabaseManager
    
    async def main():
        db_pool = await asyncpg.create_pool(
            host="192.168.200.15",
            port=5432,
            database="flight_db",
            user="flight_db_user",
            password="flight_db_password"
        )
        db = AsyncDatabaseManager(db_pool)
        async with aiohttp.ClientSession() as session:
            await download_schedules(db, session, {}, {})
        await db_pool.close()
    
    asyncio.run(main())