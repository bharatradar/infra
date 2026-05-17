"""One-time migration: convert 3-letter IATA codes to 4-letter ICAO in flight_schedules.

Uses airports.csv as the mapping source. Updates all relevant columns across tables.
"""
import csv
import os
import sys

UPDATE_PAIRS = [
    ("flight_schedules", "route_airport"),
    ("flight_schedules", "airport_code"),
    ("arrivals_log", "origin"),
    ("arrivals_log", "airport"),
    ("departures_log", "destination"),
    ("departures_log", "airport"),
    ("flight_events", "airport"),
    ("flight_events", "origin"),
    ("flight_events", "destination"),
    ("ground_ops", "airport"),
    ("ground_ops", "origin"),
]

def load_iata_to_icao(csv_path: str) -> dict[str, str]:
    mapping = {}
    try:
        with open(csv_path, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                iata = row.get('IATA', '').strip().upper()
                icao = row.get('ICAO', '').strip().upper()
                if iata and icao and len(iata) == 3 and len(icao) == 4:
                    mapping[iata] = icao
    except Exception as e:
        print(f"Error reading {csv_path}: {e}", file=sys.stderr)
        sys.exit(1)
    return mapping


import asyncio

async def main():
    csv_path = os.environ.get("AIRPORTS_CSV", "build/cortex-webapp/data/airports.csv")
    mapping = load_iata_to_icao(csv_path)
    print(f"Loaded {len(mapping)} IATA->ICAO mappings from {csv_path}")

    import asyncpg
    try:
        pool = await asyncpg.create_pool(
            host=os.environ.get("DB_HOST", "45.88.189.38"),
            port=int(os.environ.get("DB_PORT", 5432)),
            database=os.environ.get("DB_NAME", "flight_db"),
            user=os.environ.get("DB_USER", "flight_db_user"),
            password=os.environ.get("DB_PASSWORD", ""),
            min_size=1,
            max_size=1,
        )
    except Exception as e:
        print(f"DB connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    async with pool.acquire() as conn:
        for table, column in UPDATE_PAIRS:
            total = 0
            for iata, icao in mapping.items():
                r = await conn.execute(
                    f"UPDATE {table} SET {column} = $1 "
                    f"WHERE {column} = $2",
                    icao, iata,
                )
                affected = int(r.split()[-1])
                total += affected
            if total > 0:
                print(f"{table}.{column}: {total} rows updated")

    await pool.close()
    print("Done")

if __name__ == "__main__":
    asyncio.run(main())
