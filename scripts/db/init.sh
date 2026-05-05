#!/bin/bash
# BharatRadar Database Initialization Script
# Initializes PostgreSQL, Redis, and optionally InfluxDB for BharatRadar platform

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default options
FORCE=false
POSTGRES=true
REDIS=true
INFLUXDB=true

# Usage function
usage() {
    cat << EOF
BharatRadar Database Initialization Script

Usage: $0 [OPTIONS]

OPTIONS:
    --help              Show this help message
    --force              Force reset (drop and recreate tables)
    --postgres-only      Initialize PostgreSQL only
    --redis-only         Initialize Redis only
    --skip-postgres     Skip PostgreSQL initialization
    --skip-redis        Skip Redis initialization
    --skip-influxdb     Skip InfluxDB initialization

EXAMPLES:
    $0                  # Initialize everything
    $0 --force          # Force reset (drop tables first)
    $0 --postgres-only  # PostgreSQL only
    $0 --skip-influxdb  # Skip InfluxDB

EOF
    exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --help)
            usage
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --postgres-only)
            REDIS=false
            INFLUXDB=false
            shift
            ;;
        --redis-only)
            POSTGRES=false
            INFLUXDB=false
            shift
            ;;
        --skip-postgres)
            POSTGRES=false
            shift
            ;;
        --skip-redis)
            REDIS=false
            shift
            ;;
        --skip-influxdb)
            INFLUXDB=false
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# Load configuration
echo -e "${YELLOW}Loading configuration...${NC}"
if [ -f "$SCRIPT_DIR/database.env" ]; then
    source "$SCRIPT_DIR/database.env"
    echo "  Loaded database.env"
else
    echo -e "${RED}ERROR: database.env not found!${NC}"
    echo "  Copy database.env.example to database.env and configure it."
    exit 1
fi

# Set defaults
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-flight_db}"
POSTGRES_USER="${POSTGRES_USER:-flight_db_user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"

REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_DB="${REDIS_DB:-0}"

INFLUXDB_URL="${INFLUXDB_URL:-http://localhost:8086}"
INFLUXDB_TOKEN="${INFLUXDB_TOKEN:-}"
INFLUXDB_ORG="${INFLUXDB_ORG:-Vellur}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-flight_radar_telemetry}"

APP_NAME="${APP_NAME:-raga_flight_status}"

# Print banner
echo ""
echo "=============================================="
echo "  BharatRadar Database Initialization"
echo "=============================================="
echo ""
echo "Configuration:"
echo "  PostgreSQL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB"
echo "  Redis:      $REDIS_HOST:$REDIS_PORT/$REDIS_DB"
echo "  InfluxDB:  $INFLUXDB_URL"
echo ""

# Confirmation for production
if [ "$FORCE" = false ]; then
    echo -e "${YELLOW}This will initialize/reset the database.${NC}"
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ============================================================================
# PostgreSQL Initialization
# ============================================================================
if [ "$POSTGRES" = true ]; then
    echo ""
    echo -e "${GREEN}=========================================="
    echo "  PostgreSQL Initialization"
    echo -e "==========================================${NC}"
    echo ""

    # Check if psql is available
    if ! command -v psql &> /dev/null; then
        echo -e "${RED}ERROR: psql not found. Please install postgresql-client.${NC}"
        exit 1
    fi

    # Export PGPASSWORD
    export PGPASSWORD="$POSTGRES_PASSWORD"

    echo "Creating schema and tables..."
    psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$SCRIPT_DIR/postgres/schema.sql" > /dev/null 2>&1 || {
        echo -e "${RED}ERROR: Failed to create schema. Check your database credentials.${NC}"
        exit 1
    }
    echo -e "  ${GREEN}✓${NC} Tables created"

    echo "Seeding airports..."
    psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$SCRIPT_DIR/postgres/seed-airports.sql" > /dev/null 2>&1 || true
    echo -e "  ${GREEN}✓${NC} Airports seeded"

    echo "Seeding runways..."
    psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$SCRIPT_DIR/postgres/seed-runways.sql" > /dev/null 2>&1 || true
    echo -e "  ${GREEN}✓${NC} Runways seeded"

    # Verify
    AIRPORT_COUNT=$(psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM airports;" 2>/dev/null || echo "0")
    RUNWAY_COUNT=$(psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM runways;" 2>/dev/null || echo "0")

    echo ""
    echo "PostgreSQL Status:"
    echo "  Airports: $AIRPORT_COUNT"
    echo "  Runways: $RUNWAY_COUNT"
fi

# ============================================================================
# Redis Initialization
# ============================================================================
if [ "$REDIS" = true ]; then
    echo ""
    echo -e "${GREEN}=========================================="
    echo "  Redis Initialization"
    echo -e "==========================================${NC}"
    echo ""

    # Make redis setup executable
    chmod +x "$SCRIPT_DIR/redis/redis-setup.sh"

    # Run redis setup
    if "$SCRIPT_DIR/redis/redis-setup.sh"; then
        echo -e "  ${GREEN}✓${NC} Redis setup complete"
    else
        echo -e "  ${YELLOW}⚠${NC} Redis setup had warnings (continuing anyway)"
    fi
fi

# ============================================================================
# InfluxDB Initialization (Optional)
# ============================================================================
if [ "$INFLUXDB" = true ]; then
    echo ""
    echo -e "${GREEN}=========================================="
    echo "  InfluxDB Initialization"
    echo -e "==========================================${NC}"
    echo ""

    if [ -z "$INFLUXDB_TOKEN" ]; then
        echo -e "${YELLOW}  ⚠${NC} InfluxDB token not configured. Skipping."
    else
        echo "Checking InfluxDB connection..."

        # Check if influx CLI is available
        if command -v influx &> /dev/null; then
            # Try to create bucket if it doesn't exist
            influx bucket create \
                -t "$INFLUXDB_TOKEN" \
                -o "$INFLUXDB_ORG" \
                --name "$INFLUXDB_BUCKET" 2>/dev/null || echo "  Bucket may already exist"

            echo -e "  ${GREEN}✓${NC} InfluxDB bucket ready"
        else
            echo -e "${YELLOW}  ⚠${NC} influx CLI not found. Skipping InfluxDB setup."
            echo "    Install influx-cli to enable InfluxDB initialization."
        fi
    fi
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "=============================================="
echo "  Initialization Complete!"
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Review the configuration in database.env"
echo "  2. Update your application config to use these databases"
echo "  3. Start the BharatRadar services"
echo ""
echo "Database details:"
echo "  PostgreSQL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB"
echo "  Redis:      $REDIS_HOST:$REDIS_PORT"
echo "  InfluxDB:   $INFLUXDB_URL (bucket: $INFLUXDB_BUCKET)"
echo ""