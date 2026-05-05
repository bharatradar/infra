#!/bin/bash
# BharatRadar Redis Setup Script
# Creates geo-index for airports from PostgreSQL database

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../database.env" 2>/dev/null || true

# Default values if not set
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_DB="${REDIS_DB:-0}"

# PostgreSQL defaults
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-flight_db_user}"
DB_PASS="${DB_PASS:-raga@098}"
DB_NAME="${DB_NAME:-flight_db}"

echo "=========================================="
echo "BharatRadar Redis Setup"
echo "=========================================="
echo "Redis Host: $REDIS_HOST"
echo "Redis Port: $REDIS_PORT"
echo "Redis DB: $REDIS_DB"
echo ""

# Check if redis-cli is available
if ! command -v redis-cli &> /dev/null; then
    echo "ERROR: redis-cli not found. Please install redis-tools."
    exit 1
fi

# Build redis connection string
REDIS_CMD="redis-cli -h $REDIS_HOST -p $REDIS_PORT -n $REDIS_DB"

echo "Setting up Redis geo-index for airports..."

# Delete existing india_airports index if it exists
$REDIS_CMD DEL india_airports 2>/dev/null || true

# Check if PostgreSQL is available for airport data
if command -v psql &> /dev/null && PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" &>/dev/null; then
    echo "Loading airports from PostgreSQL database..."
    
    # Get all airports from database
    PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT lon, lat, icao FROM airports ORDER BY icao;" 2>/dev/null | while read line; do
        lon=$(echo "$line" | awk '{print $1}' | tr -d ' ')
        lat=$(echo "$line" | awk '{print $2}' | tr -d ' ')
        icao=$(echo "$line" | awk '{print $3}' | tr -d ' ')
        
        if [ -n "$lon" ] && [ -n "$lat" ] && [ -n "$icao" ]; then
            $REDIS_CMD GEOADD india_airports "$lon" "$lat" "$icao" > /dev/null 2>&1 || true
        fi
    done
    
    AIRPORT_COUNT=$($REDIS_CMD ZCARD india_airports 2>/dev/null || echo "0")
    echo "Loaded $AIRPORT_COUNT airports from database"
else
    echo "WARNING: PostgreSQL not available, using basic airport list"
    
    # Fallback: Basic airports if DB not available
    AIRPORTS=(
        "77.103,28.566:DEL"
        "72.868,19.089:BOM"
        "77.706,13.198:BLR"
    )
    
    for airport in "${AIRPORTS[@]}"; do
        coord="${airport%%:*}"
        name="${airport##*:}"
        lon="${coord%%,*}"
        lat="${coord##*,}"
        
        $REDIS_CMD GEOADD india_airports "$lon" "$lat" "$name" > /dev/null 2>&1 || true
    done
    
    AIRPORT_COUNT=${#AIRPORTS[@]}
fi

echo ""
echo "Setting up Redis key prefixes..."

# Set key prefixes for flight data
APP_NAME="${APP_NAME:-raga_flight_status}"

# Live flights key (will be populated by enricher)
$REDIS_CMD SET "${APP_NAME}:setup:created" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" NX > /dev/null || true

echo ""
echo "=========================================="
echo "Redis setup complete!"
echo "=========================================="
echo ""
AIRPORT_COUNT=$($REDIS_CMD ZCARD india_airports 2>/dev/null || echo "0")
echo "Geo-index 'india_airports' created with $AIRPORT_COUNT airports"
echo "Use: GEOSEARCH india_airports FROMLONLAT 78 20 DISTANCE 500 KM"
echo ""

# Verify setup
KEY_COUNT=$($REDIS_CMD DBSIZE)
echo "Total keys in database: $KEY_COUNT"