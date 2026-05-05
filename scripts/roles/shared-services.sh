#!/bin/bash
# Role: Shared Services (PostgreSQL + Redis + InfluxDB on Pi)
# Installs shared data services used by K3s servers and BharatRadar applications.

set -euo pipefail

role_shared_services_collect_config() {
    log_step "Shared Services Configuration"

    echo ""

    # If called via CLI (no SUB_ROLE set), prompt for sub-mode
    if [ -z "${SUB_ROLE:-}" ]; then
        echo "  1) Primary DB Setup     - Fresh PostgreSQL/Redis/InfluxDB/MinIO install"
        echo "  2) Join as DB Standby   - Streaming replica for failover"
        echo ""

        local choice
        while true; do
            read -rp "Select [1-2]: " choice < /dev/tty
            case "$choice" in
                1) SUB_ROLE="primary"; break ;;
                2)
                    echo ""
                    log_info "Redirecting to DB Standby setup..."
                    source "${SCRIPT_DIR}/roles/db-standby.sh"
                    role_db_standby_run
                    exit 0
                    ;;
                *) echo "Invalid selection. Please enter 1 or 2." ;;
            esac
        done
        echo ""
    fi

    if [ "${SUB_ROLE:-}" = "replica" ]; then
        source "${SCRIPT_DIR}/roles/db-standby.sh"
        role_db_standby_run
        exit 0
    fi

    echo "  This installs PostgreSQL, Redis, InfluxDB, and MinIO on this machine."
    echo "  K3s servers will use PostgreSQL as their external datastore."
    echo "  History service will use MinIO as its S3-compatible storage backend."
    echo ""

    # Detect local IP for smart default
    local detected_ip
    detected_ip=$(detect_local_ip || echo "192.168.200.1")

    prompt_input "Database listen IP" "$detected_ip" DB_LISTEN_IP

    while ! validate_ip "$DB_LISTEN_IP"; do
        log_error "Invalid IP address"
        prompt_input "Database listen IP" "$DB_LISTEN_IP" DB_LISTEN_IP
    done

    # PostgreSQL port
    prompt_input "PostgreSQL port" "5432" DB_PORT

    # Generate passwords
    DB_PASSWORD=$(generate_secret)
    DB_USER="k3s"
    DB_NAME="k3s"

    # Redis password
    REDIS_PASSWORD=$(generate_secret)

    # InfluxDB
    INFLUXDB_ADMIN_TOKEN=$(generate_secret)

    # MinIO
    MINIO_ROOT_USER="minioadmin"
    MINIO_ROOT_PASSWORD=$(generate_secret)

    echo ""
    log_step "Generated Credentials"
    echo -e "  ${YELLOW}PostgreSQL password: ${DB_PASSWORD}${NC}"
    echo -e "  ${YELLOW}Redis password:      ${REDIS_PASSWORD}${NC}"
    echo -e "  ${YELLOW}InfluxDB admin token: ${INFLUXDB_ADMIN_TOKEN}${NC}"
    echo -e "  ${YELLOW}MinIO password:      ${MINIO_ROOT_PASSWORD}${NC}"
    echo ""
    echo -e "  ${CYAN}Save these! They will be shown again after installation.${NC}"
    echo ""

    if ! prompt_confirm "Proceed with Shared Services installation?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_shared_services_install_packages() {
    log_step "Installing Packages"

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get update -qq
            apt-get install -y -qq wget gnupg2 lsb-release apt-transport-https ca-certificates
            ;;
        *)
            log_error "Unsupported OS: $os"
            exit 1
            ;;
    esac

    log_success "Base packages installed"
}

role_shared_services_install_postgresql() {
    log_step "Installing PostgreSQL"

    local os
    os=$(detect_os)

    if ! command -v psql &>/dev/null; then
        case "$os" in
            debian|ubuntu|raspbian)
                local codename
                codename=$(. /etc/os-release && echo "$VERSION_CODENAME")

                apt-get install -y -qq postgresql postgresql-client || {
                    echo "deb http://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" > /etc/apt/sources.list.d/pgdg.list
                    wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
                    apt-get update -qq
                    apt-get install -y -qq postgresql postgresql-client
                }
                ;;
        esac
        log_success "PostgreSQL installed"
    else
        log_info "PostgreSQL already installed: $(psql --version 2>/dev/null || echo 'unknown')"
    fi

    # Ensure the cluster actually exists and has a data directory
    local pg_version
    pg_version=$(ls /etc/postgresql/ 2>/dev/null | head -1)
    if [ -n "$pg_version" ] && [ ! -d "/var/lib/postgresql/${pg_version}/main" ]; then
        log_warn "PostgreSQL data directory missing, recreating cluster..."
        pg_dropcluster --stop "$pg_version" main 2>/dev/null || true
        pg_createcluster "$pg_version" main 2>/dev/null
        log_success "PostgreSQL cluster recreated"
    fi
}

role_shared_services_configure_postgresql() {
    log_step "Configuring PostgreSQL"

    # Find the PostgreSQL version and cluster
    local pg_version
    pg_version=$(ls /etc/postgresql/ 2>/dev/null | head -1)
    if [ -z "$pg_version" ]; then
        log_error "No PostgreSQL version found"
        return 1
    fi

    local pg_conf="/etc/postgresql/${pg_version}/main/postgresql.conf"
    local pg_hba="/etc/postgresql/${pg_version}/main/pg_hba.conf"
    local data_dir="/var/lib/postgresql/${pg_version}/main"
    local pg_service="postgresql@${pg_version}-main"

    # Fix stale cluster: config exists but data directory doesn't
    if [ ! -d "$data_dir" ]; then
        log_warn "PostgreSQL data directory missing, recreating cluster..."
        pg_dropcluster --stop "$pg_version" main 2>/dev/null || true
        pg_createcluster "$pg_version" main 2>/dev/null
    fi

    if [ ! -f "$pg_conf" ] || [ ! -f "$pg_hba" ]; then
        log_error "PostgreSQL config files not found"
        return 1
    fi

    # Configure listen addresses - use * to accept from all local IPs
    # Access control is handled by pg_hba.conf
    sed -i "/^#*listen_addresses/c\\listen_addresses = '*'" "$pg_conf"

    # Add connection permission for K3s servers on the local subnet
    local subnet
    subnet=$(echo "$DB_LISTEN_IP" | sed 's/\.[0-9]*$/\.0\/24/')
    if ! grep -q "${subnet}.*md5" "$pg_hba"; then
        echo "host    all             all             ${subnet}            md5" >> "$pg_hba"
    fi

    # Start/restart PostgreSQL
    systemctl enable "$pg_service" 2>/dev/null || true
    systemctl restart "$pg_service"

    for i in $(seq 1 30); do
        if systemctl is-active --quiet "$pg_service"; then
            break
        fi
        sleep 1
    done

    if ! systemctl is-active --quiet "$pg_service"; then
        log_error "PostgreSQL failed to start"
        log_info "Check: journalctl -u ${pg_service} -f"
        return 1
    fi

    # Verify listen_addresses took effect
    if grep -q "^listen_addresses = '\*'" "$pg_conf"; then
        log_success "PostgreSQL listening on all interfaces"
    else
        log_warn "listen_addresses may not be correct, check: grep listen_addresses $pg_conf"
    fi

    # Verify network binding
    if ss -tlnp | grep -q ":${DB_PORT}"; then
        log_success "PostgreSQL listening on port ${DB_PORT}"
    else
        log_error "PostgreSQL not listening on port ${DB_PORT}"
        return 1
    fi

    # Create database and user
    log_info "Creating database and user..."
    sudo -u postgres psql <<EOF
CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';
CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
ALTER DATABASE ${DB_NAME} SET timezone TO 'UTC';
EOF

    # Create BharatRadar database (flight_db)
    log_info "Creating BharatRadar database..."
    
    # Default password for flight_db_user
    FLIGHT_DB_PASSWORD="${FLIGHT_DB_PASSWORD:-raga@098}"
    
    # Create user if not exists, or alter password
    sudo -u postgres psql <<EOF
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'flight_db_user') THEN
        CREATE ROLE flight_db_user LOGIN CREATEDB PASSWORD '${FLIGHT_DB_PASSWORD}';
    ELSE
        ALTER USER flight_db_user WITH PASSWORD '${FLIGHT_DB_PASSWORD}';
    END IF;
END
\$\$;
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'flight_db') THEN
        CREATE DATABASE flight_db OWNER flight_db_user;
        GRANT ALL PRIVILEGES ON DATABASE flight_db TO flight_db_user;
        ALTER DATABASE flight_db SET timezone TO 'UTC';
    END IF;
END
\$\$;
EOF

    log_success "Database '${DB_NAME}' and user '${DB_USER}' created"
    log_success "Database 'flight_db' and user 'flight_db_user' ready"
}

role_shared_services_install_redis() {
    log_step "Installing Redis"

    if command -v redis-server &>/dev/null; then
        log_info "Redis already installed: $(redis-server --version 2>/dev/null || echo 'unknown')"
        return 0
    fi

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get install -y -qq redis-server
            ;;
    esac

    log_success "Redis installed"
}

role_shared_services_configure_redis() {
    log_step "Configuring Redis"

    local redis_conf="/etc/redis/redis.conf"

    if [ ! -f "$redis_conf" ]; then
        redis_conf="/etc/redis.conf"
    fi

    if [ ! -f "$redis_conf" ]; then
        log_warn "Redis config not found, skipping configuration"
        return 0
    fi

    # Bind to LAN IP (use /c to replace entire line, not partial substitution)
    sed -i "/^bind /c\\bind 127.0.0.1 ${DB_LISTEN_IP}" "$redis_conf"

    # Set password - always use single delimiter to avoid special char conflicts
    # Delete ALL requirepass lines first, then append one clean entry
    sed -i '/^requirepass /d' "$redis_conf"
    printf '%s\n' "requirepass ${REDIS_PASSWORD}" >> "$redis_conf"

    # Disable protected mode for LAN access
    sed -i "s/^protected-mode yes/protected-mode no/" "$redis_conf"

    # Kill any stale Redis process before restart (port may still be bound)
    local redis_pid
    redis_pid=$(ss -tlnp 2>/dev/null | grep ':6379' | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$redis_pid" ]; then
        kill "$redis_pid" 2>/dev/null || true
        sleep 1
    fi

    # Restart
    systemctl reset-failed redis-server 2>/dev/null || true
    systemctl restart redis-server 2>/dev/null || systemctl restart redis 2>/dev/null || true
    systemctl enable redis-server 2>/dev/null || systemctl enable redis 2>/dev/null || true

    sleep 2

    if systemctl is-active --quiet redis-server 2>/dev/null || systemctl is-active --quiet redis 2>/dev/null; then
        log_success "Redis configured"
    else
        log_warn "Redis may need manual restart: sudo systemctl restart redis-server"
    fi
}

role_shared_services_install_influxdb() {
    log_step "Installing InfluxDB"

    if command -v influxd &>/dev/null; then
        log_info "InfluxDB already installed"
        return 0
    fi

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            # Clean stale repo files from prior failed attempts
            sudo rm -f /etc/apt/sources.list.d/influxdata.list
            sudo rm -rf /usr/share/keyrings/influxdb-archive-keyring.gpg
            sudo rm -rf /usr/share/influxdata-archive-keyring

            # Fetch signing key
            curl -s "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xDA61C26A0585BD3B" | gpg --dearmor | sudo tee /usr/share/keyrings/influxdb-archive-keyring.gpg > /dev/null

            # Add repo
            echo "deb [signed-by=/usr/share/keyrings/influxdb-archive-keyring.gpg] https://repos.influxdata.com/debian stable main" | sudo tee /etc/apt/sources.list.d/influxdata.list > /dev/null

            sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq

            # UCF_FORCE_CONFF_NEW prevents the influxdata-archive-keyring package
            # from showing a debconf dialog about modified influxdata.list
            # (the package uses ucf, not dpkg, for conffile management)
            sudo DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFF_NEW=1 apt-get install -y influxdb2 || {
                log_warn "InfluxDB 2.x not available in repo, skipping..."
                return 0
            }
            ;;
    esac

    log_success "InfluxDB installed"
}

role_shared_services_configure_influxdb() {
    log_step "Configuring InfluxDB"

    # Start InfluxDB if not running
    systemctl enable influxdb 2>/dev/null || true
    systemctl start influxdb 2>/dev/null || true

    sleep 3

    if systemctl is-active --quiet influxdb 2>/dev/null; then
        log_info "Setting up InfluxDB initial config..."

        # Setup via CLI (InfluxDB 2.x)
        if command -v influx &>/dev/null; then
            influx setup \
                --username admin \
                --password "${INFLUXDB_ADMIN_TOKEN}" \
                --org bharatradar \
                --bucket metrics \
                --token "${INFLUXDB_ADMIN_TOKEN}" \
                --force 2>/dev/null || {
                    log_warn "InfluxDB setup failed (may already be configured)"
                }
        fi

        log_success "InfluxDB configured"
    else
        log_warn "InfluxDB service not available (may not be installed for this OS/arch)"
    fi
}

role_shared_services_install_minio() {
    log_step "Installing MinIO"

    if command -v minio &>/dev/null; then
        log_info "MinIO already installed: $(minio --version 2>/dev/null || echo 'unknown')"
        return 0
    fi

    local arch
    arch=$(uname -m)
    local minio_arch="amd64"
    case "$arch" in
        aarch64|arm64) minio_arch="arm64" ;;
        x86_64) minio_arch="amd64" ;;
        *) log_error "Unsupported architecture: $arch"; return 1 ;;
    esac

    log_info "Downloading MinIO for ${minio_arch}..."
    curl -sL "https://dl.min.io/server/minio/release/linux-${minio_arch}/minio" -o /usr/local/bin/minio
    chmod +x /usr/local/bin/minio

    log_success "MinIO installed: $(minio --version 2>/dev/null)"
}

role_shared_services_configure_minio() {
    log_step "Configuring MinIO"

    # Generate credentials if not set
    if [ -z "${MINIO_ROOT_USER:-}" ]; then
        MINIO_ROOT_USER="minioadmin"
    fi
    if [ -z "${MINIO_ROOT_PASSWORD:-}" ]; then
        MINIO_ROOT_PASSWORD=$(generate_secret)
    fi

    MINIO_DATA_DIR="/data/minio"
    mkdir -p "$MINIO_DATA_DIR"

    # Create systemd service
    cat > /etc/systemd/system/minio.service <<EOF
[Unit]
Description=MinIO Object Storage
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/minio server \
    --address "${DB_LISTEN_IP}:9000" \
    --console-address "${DB_LISTEN_IP}:9001" \
    ${MINIO_DATA_DIR}
Environment="MINIO_ROOT_USER=${MINIO_ROOT_USER}"
Environment="MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}"
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable minio
    systemctl start minio

    # Wait for MinIO to be ready
    for i in $(seq 1 30); do
        if curl -sf "http://${DB_LISTEN_IP}:9000/minio/health/live" &>/dev/null; then
            log_success "MinIO is running"
            break
        fi
        sleep 2
    done

    if ! curl -sf "http://${DB_LISTEN_IP}:9000/minio/health/live" &>/dev/null; then
        log_error "MinIO failed to start"
        log_info "Check: journalctl -u minio -f"
        return 1
    fi

    # Install mc (MinIO Client) for bucket creation
    if ! command -v mc &>/dev/null; then
        local arch
        arch=$(uname -m)
        local mc_arch="amd64"
        case "$arch" in
            aarch64|arm64) mc_arch="arm64" ;;
        esac
        curl -sL "https://dl.min.io/client/mc/release/linux-${mc_arch}/mc" -o /usr/local/bin/mc
        chmod +x /usr/local/bin/mc
    fi

    # Configure mc alias and create bucket
    sleep 2
    mc alias set local "http://${DB_LISTEN_IP}:9000" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" 2>/dev/null
    mc mb local/history --ignore-existing 2>/dev/null

    log_success "MinIO bucket 'history' created"
}

role_shared_services_init_flight_db() {
    log_step "Initializing BharatRadar Database (flight_db)"

    # Default credentials
    FLIGHT_DB_PASSWORD="${FLIGHT_DB_PASSWORD:-raga@098}"

    # Check if flight_db already has data
    local existing_count
    existing_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM airports;" 2>/dev/null || echo "0")

    if [ "$existing_count" -gt 0 ] 2>/dev/null; then
        log_info "flight_db already has $existing_count airports, skipping initialization"
        return 0
    fi

    log_info "Downloading BharatRadar schema and seed data..."

    # Download the schema and seed files from GitHub
    local scripts_dir="/tmp/bharatradar-db-scripts"
    mkdir -p "$scripts_dir"

    # Download schema.sql
    curl -sL "https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/schema.sql" \
        -o "$scripts_dir/schema.sql" || {
        log_warn "Failed to download schema.sql, skipping initialization"
        return 0
    }

    # Download seed-airports.sql
    curl -sL "https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/seed-airports.sql" \
        -o "$scripts_dir/seed-airports.sql" || {
        log_warn "Failed to download seed-airports.sql, skipping initialization"
        return 0
    }

    # Download seed-runways.sql
    curl -sL "https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/seed-runways.sql" \
        -o "$scripts_dir/seed-runways.sql" || {
        log_warn "Failed to download seed-runways.sql, skipping initialization"
        return 0
    }

    log_info "Creating schema and seeding data..."

    # Create schema
    PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/schema.sql" >/dev/null 2>&1 || {
        log_warn "Schema creation had warnings (continuing anyway)"
    }

    # Seed airports
    PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/seed-airports.sql" >/dev/null 2>&1 || true

    # Seed runways
    PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/seed-runways.sql" >/dev/null 2>&1 || true

    # Verify
    local airport_count runway_count
    airport_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM airports;" 2>/dev/null || echo "0")
    runway_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM runways;" 2>/dev/null || echo "0")

    log_success "BharatRadar database initialized: $airport_count airports, $runway_count runways"

    # Cleanup
    rm -rf "$scripts_dir"

    # Set up Redis geo-index with ALL airports
    log_info "Setting up Redis geo-index with all airports..."

    # Delete existing index
    redis-cli -a "$REDIS_PASSWORD" DEL india_airports >/dev/null 2>&1 || true

    # Add ALL airports from database to Redis geo-index
    local geo_count=0
    while IFS='|' read -r icao lat lon; do
        icao=$(echo "$icao" | tr -d ' ')
        lat=$(echo "$lat" | tr -d ' ')
        lon=$(echo "$lon" | tr -d ' ')
        
        if [ -n "$icao" ] && [ -n "$lat" ] && [ -n "$lon" ]; then
            redis-cli -a "$REDIS_PASSWORD" GEOADD india_airports "$lon" "$lat" "$icao" >/dev/null 2>&1 && geo_count=$((geo_count + 1))
        fi
    done < <(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT icao, lat, lon FROM airports;" 2>/dev/null)

    log_success "Redis geo-index created: $geo_count airports"

    log_success "BharatRadar database initialization complete"
}

role_shared_services_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    local conn_string="postgres://${DB_USER}:${DB_PASSWORD}@${DB_LISTEN_IP}:${DB_PORT}/${DB_NAME}"
    MINIO_ENDPOINT="${MINIO_ENDPOINT:-${DB_LISTEN_IP}:9000}"

    cat > /etc/bharatradar/db-config.env <<EOF
# Shared Services Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 4.0.0

ROLE=shared-services
DB_LISTEN_IP="${DB_LISTEN_IP}"
DB_PORT="${DB_PORT}"
DB_USER="${DB_USER}"
DB_PASSWORD="${DB_PASSWORD}"
DB_NAME="${DB_NAME}"
REDIS_PASSWORD="${REDIS_PASSWORD}"
INFLUXDB_ADMIN_TOKEN="${INFLUXDB_ADMIN_TOKEN}"
MINIO_ROOT_USER="${MINIO_ROOT_USER}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}"
MINIO_ENDPOINT="${MINIO_ENDPOINT}"

# Connection string for K3s servers
DB_CONNECTION_STRING="${conn_string}"
EOF

    chmod 600 /etc/bharatradar/db-config.env

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar Shared Services Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

ROLE=shared-services
SUB_ROLE="${SUB_ROLE:-primary}"

# PostgreSQL
DB_LISTEN_IP="${DB_LISTEN_IP}"
DB_PORT="${DB_PORT}"
DB_USER="${DB_USER}"
DB_NAME="${DB_NAME}"
DB_PASSWORD="${DB_PASSWORD}"
DB_CONNECTION_STRING="${conn_string}"

# Redis
REDIS_HOST="${DB_LISTEN_IP}"
REDIS_PORT="6379"
REDIS_PASSWORD="${REDIS_PASSWORD}"

# InfluxDB
INFLUXDB_HOST="${DB_LISTEN_IP}"
INFLUXDB_PORT="8086"
INFLUXDB_ADMIN_TOKEN="${INFLUXDB_ADMIN_TOKEN}"

# MinIO
MINIO_ENDPOINT="${MINIO_ENDPOINT}"
MINIO_ROOT_USER="${MINIO_ROOT_USER}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}"
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/"

    # Save credentials file for reference
    local creds_dir="/etc/bharatradar/credentials"
    local creds_file="${creds_dir}/shared-services-$(date -u +"%Y%m%d-%H%M%S").txt"
    mkdir -p "$creds_dir"

    cat > "$creds_file" <<EOF
============================================================
  Shared Services Credentials
  Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
  Host: $(hostname)
  IP: ${DB_LISTEN_IP}
============================================================

  PostgreSQL:
    Host:     ${DB_LISTEN_IP}
    Port:     ${DB_PORT}
    Database: ${DB_NAME}
    User:     ${DB_USER}
    Password: ${DB_PASSWORD}
    URL:      ${conn_string}

  Redis:
    Host:     ${DB_LISTEN_IP}
    Port:     6379
    Password: ${REDIS_PASSWORD}

  InfluxDB:
    Host:     ${DB_LISTEN_IP}
    Port:     8086
    Token:    ${INFLUXDB_ADMIN_TOKEN}

  MinIO:
    Host:     ${DB_LISTEN_IP}
    API Port: 9000
    Console:  ${DB_LISTEN_IP}:9001
    User:     ${MINIO_ROOT_USER}
    Password: ${MINIO_ROOT_PASSWORD}

============================================================
EOF

    chmod 600 "$creds_file"
    log_success "Credentials saved to ${creds_file}"
}

role_shared_services_post_install() {
    local conn_string="postgres://${DB_USER}:${DB_PASSWORD}@${DB_LISTEN_IP}:${DB_PORT}/${DB_NAME}"

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Shared Services Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}Services:${NC}"
    echo "    PostgreSQL: ${DB_LISTEN_IP}:${DB_PORT}"
    echo "    Redis:      ${DB_LISTEN_IP}:6379"
    echo "    InfluxDB:   ${DB_LISTEN_IP}:8086"
    echo "    MinIO:      ${DB_LISTEN_IP}:9000 (console: ${DB_LISTEN_IP}:9001)"
    echo ""
    echo -e "  ${CYAN}Credentials (save these!):${NC}"
    echo "    PostgreSQL:  user=${DB_USER}  password=${DB_PASSWORD}"
    echo "    Redis:       password=${REDIS_PASSWORD}"
    echo "    InfluxDB:    token=${INFLUXDB_ADMIN_TOKEN}"
    echo "    MinIO:       user=${MINIO_ROOT_USER}  password=${MINIO_ROOT_PASSWORD}"
    echo ""
    echo -e "  ${CYAN}K3s Connection String:${NC}"
    echo "    ${conn_string}"
    echo ""
    echo -e "  ${CYAN}BharatRadar Database (flight_db):${NC}"
    echo "    user=flight_db_user  password=${FLIGHT_DB_PASSWORD:-raga@098}"
    echo ""
    echo -e "  ${CYAN}MinIO Rclone Config:${NC}"
    echo "    endpoint = http://${DB_LISTEN_IP}:9000"
    echo ""
    echo -e "  ${CYAN}Next step: Install Primary Hub with this connection string:${NC}"
    echo "    curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash"
    echo "    Select: 2) Primary Hub"
    echo ""
    echo -e "  ${CYAN}Useful commands:${NC}"
    echo "    sudo systemctl status postgresql"
    echo "    sudo systemctl status redis-server"
    echo "    sudo systemctl status influxdb"
    echo "    sudo systemctl status minio"
    echo "    sudo -u postgres psql -d k3s"
    echo "    mc alias set local http://${DB_LISTEN_IP}:9000 ${MINIO_ROOT_USER} ${MINIO_ROOT_PASSWORD}"
    echo "    mc ls local/history"
    echo ""
    echo -e "  ${CYAN}Credentials file:${NC} /etc/bharatradar/credentials/shared-services-*.txt"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_shared_services_run() {
    require_root

    local install_failed=false
    local phases=(config packages postgresql redis influxdb minio flight_db_init save)

    show_resume_banner "${phases[@]}"

    # Load existing config if present
    if [ -f /etc/bharatradar/db-config.env ]; then
        source /etc/bharatradar/db-config.env
        DB_LISTEN_IP="${DB_LISTEN_IP:-}"
        DB_PORT="${DB_PORT:-5432}"
    fi

    # Phase: config
    if ! checkpoint_completed "config"; then
        role_shared_services_collect_config

        # Save all answers for resume support
        save_config_value "ROLE" "shared-services"
        save_config_value "SUB_ROLE" "${SUB_ROLE:-primary}"
        save_config_value "DB_LISTEN_IP" "${DB_LISTEN_IP}"
        save_config_value "DB_PORT" "${DB_PORT:-5432}"
        save_config_value "DB_PASSWORD" "${DB_PASSWORD}"
        save_config_value "DB_USER" "${DB_USER:-k3s}"
        save_config_value "DB_NAME" "${DB_NAME:-k3s}"
        save_config_value "REDIS_PASSWORD" "${REDIS_PASSWORD}"
        save_config_value "INFLUXDB_ADMIN_TOKEN" "${INFLUXDB_ADMIN_TOKEN}"
        save_config_value "MINIO_ROOT_USER" "${MINIO_ROOT_USER:-minioadmin}"
        save_config_value "MINIO_ROOT_PASSWORD" "${MINIO_ROOT_PASSWORD}"

        checkpoint_mark "config"
    else
        load_partial_config || {
            log_error "Saved config not found. To restart from scratch, run:"
            echo "  sudo rm /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial"
            exit 1
        }
    fi

    # Phase: packages
    if ! checkpoint_completed "packages"; then
        role_shared_services_install_packages || install_failed=true
        if [ "$install_failed" = true ]; then
            log_error "Package installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install shared-services"
            exit 1
        fi
        checkpoint_mark "packages"
    fi

    # Phase: postgresql
    if ! checkpoint_completed "postgresql"; then
        role_shared_services_install_postgresql || install_failed=true
        role_shared_services_configure_postgresql || install_failed=true
        if [ "$install_failed" = true ]; then
            log_error "PostgreSQL setup failed. To retry, run:"
            echo "  sudo ./bharatradar-install shared-services"
            exit 1
        fi
        checkpoint_mark "postgresql"
    fi

    # Phase: redis
    if ! checkpoint_completed "redis"; then
        role_shared_services_install_redis || install_failed=true
        role_shared_services_configure_redis || install_failed=true
        if [ "$install_failed" = true ]; then
            log_error "Redis setup failed. To retry, run:"
            echo "  sudo ./bharatradar-install shared-services"
            exit 1
        fi
        checkpoint_mark "redis"
    fi

    # Phase: influxdb (optional)
    if ! checkpoint_completed "influxdb"; then
        role_shared_services_install_influxdb || log_warn "InfluxDB installation skipped/failed"
        role_shared_services_configure_influxdb || log_warn "InfluxDB configuration skipped/failed"
        checkpoint_mark "influxdb"
    fi

    # Phase: minio (optional)
    if ! checkpoint_completed "minio"; then
        role_shared_services_install_minio || log_warn "MinIO installation skipped/failed"
        role_shared_services_configure_minio || log_warn "MinIO configuration skipped/failed"
        checkpoint_mark "minio"
    fi

    # Phase: flight_db_init (BharatRadar database schema and seed data)
    if ! checkpoint_completed "flight_db_init"; then
        role_shared_services_init_flight_db || log_warn "Flight DB initialization skipped/failed"
        checkpoint_mark "flight_db_init"
    fi

    # Phase: save
    if ! checkpoint_completed "save"; then
        role_shared_services_save_config
        role_shared_services_post_install
        checkpoint_mark "save"
    fi

    checkpoint_clear
    log_success "Shared Services installation complete!"
}
