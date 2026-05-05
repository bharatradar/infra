# BharatRadar Database Initialization

This directory contains scripts to initialize the PostgreSQL, Redis, and InfluxDB databases for the BharatRadar platform.

> **Note:** These scripts are automatically invoked by the main [bharatradar-install](../scripts/bharatradar-install) script during `shared-services` role installation. You typically only need these for manual re-initialization or reset.

## Quick Start

```bash
# 1. Copy the example config
cp database.env.example database.env

# 2. Edit with your credentials
vim database.env

# 3. Run initialization
./init.sh
```

## Directory Structure

```
db/
├── init.sh                    # Main entry point
├── database.env.example       # Configuration template
├── database.env              # Your credentials (gitignored)
├── postgres/
│   ├── schema.sql           # Table definitions
│   ├── seed-airports.sql   # Airport data (~130 airports)
│   └── seed-runways.sql    # Runway data
└── redis/
    └── redis-setup.sh     # Redis geo-index setup
```

## Configuration

Edit `database.env` with your credentials:

```bash
# PostgreSQL
POSTGRES_HOST=192.168.200.15
POSTGRES_PORT=5432
POSTGRES_DB=flight_db
POSTGRES_USER=flight_db_user
POSTGRES_PASSWORD=your_password

# Redis
REDIS_HOST=192.168.200.15
REDIS_PORT=6379

# InfluxDB (optional)
INFLUXDB_URL=http://192.168.200.15:8086
INFLUXDB_TOKEN=your_token
INFLUXDB_ORG=Vellur
INFLUXDB_BUCKET=flight_radar_telemetry
```

## Usage

```bash
# Initialize everything (default)
./init.sh

# Force reset (drop and recreate)
./init.sh --force

# PostgreSQL only
./init.sh --postgres-only

# Redis only
./init.sh --redis-only

# Skip InfluxDB
./init.sh --skip-influxdb
```

## What Gets Created

### PostgreSQL Tables

| Table | Description |
|-------|-------------|
| airports | ~130 Indian airports |
| runways | Runway coordinates |
| flights_in_air | Live airborne aircraft |
| arrivals_log | Arrival history |
| departures_log | Departure history |
| flight_events | Takeoff/landing events |
| ground_ops | Aircraft on ground |
| flight_schedules | Scheduled flights |
| api_users | User accounts |
| api_keys | API keys |
| feeders | Community feeders |
| feeder_daily_stats | Daily stats |
| feeder_achievements | Achievements |
| coverage_gaps | Coverage gaps |
| user_alerts | Alert configurations |
| web_subscriptions | Push subscriptions |
| ai_enrichment_audit | AI enrichment logs |
| ai_insights_log | AI insights |

### Redis

- Geo-index `india_airports` for geospatial queries
- Key prefixes for live flight data

### InfluxDB (Optional)

- Bucket `flight_radar_telemetry` for time-series data

## Security

- **NEVER commit `database.env`** - it contains passwords
- The file is gitignored automatically
- Use `.env.example` as a template

## Requirements

- PostgreSQL client (`psql`)
- Redis CLI (`redis-cli`)
- InfluxDB CLI (optional)

## Troubleshooting

### PostgreSQL connection fails

```bash
# Test connection
psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB
```

### Redis connection fails

```bash
# Test connection
redis-cli -h $REDIS_HOST -p $REDIS_PORT -n $REDIS_DB PING
```

### Permission denied

```bash
chmod +x init.sh redis/redis-setup.sh
```

---

## Schedule Downloader

K3s CronJob that downloads flight schedules from FlightRadar24.

### Configuration

The download schedule time and enabled status are stored in the `download_config` table:

```sql
-- View current configuration
SELECT * FROM download_config;

-- Update schedule time (HH:MM:SS in UTC)
UPDATE download_config SET schedule_time = '22:00:00', updated_at = NOW() WHERE id = 1;

-- Disable automatic scheduled runs
UPDATE download_config SET enabled = FALSE, updated_at = NOW() WHERE id = 1;
```

### Manual Trigger

```bash
# From the infra directory
./scripts/triggers/trigger-downloader.sh

# Or manually
kubectl create job schedule-downloader-manual --from=cronjob/schedule-downloader -n bharatradar

# Check logs
kubectl logs -n bharatradar job/schedule-downloader-manual

# Delete after completion
kubectl delete job schedule-downloader-manual -n bharatradar
```

### Files

| File | Description |
|------|-------------|
| `downloader/Dockerfile` | Container image definition |
| `downloader/route_schedule_downloader.py` | Main downloader script |
| `downloader/requirements.txt` | Python dependencies |
| `../../manifests/default/schedule-downloader-cronjob.yaml` | K3s CronJob |
| `../../scripts/triggers/trigger-downloader.sh` | Manual trigger script |
