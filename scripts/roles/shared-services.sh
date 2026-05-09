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

    # Use pre-set values from config file or prompt
    if [ -z "${DB_LISTEN_IP:-}" ]; then
        local detected_ip
        detected_ip=$(detect_local_ip || echo "192.168.200.1")
        prompt_input "Database listen IP" "$detected_ip" DB_LISTEN_IP
        while ! validate_ip "$DB_LISTEN_IP"; do
            log_error "Invalid IP address"
            prompt_input "Database listen IP" "$DB_LISTEN_IP" DB_LISTEN_IP
        done
    fi

    if [ -z "${DB_PORT:-}" ]; then
        prompt_input "PostgreSQL port" "5432" DB_PORT
    fi

    # Use pre-set passwords from config file or generate
    DB_PASSWORD="${DB_PASSWORD:-$(generate_secret)}"
    DB_USER="${DB_USER:-k3s}"
    DB_NAME="${DB_NAME:-k3s}"
    REDIS_PASSWORD="${REDIS_PASSWORD:-$(generate_secret)}"
    INFLUXDB_ADMIN_TOKEN="${INFLUXDB_ADMIN_TOKEN:-$(generate_secret)}"
    MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
    MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-$(generate_secret)}"

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

    local os pg_version
    os=$(detect_os)

    if command -v psql &>/dev/null && pg_lsclusters 2>/dev/null | grep -q online; then
        log_info "PostgreSQL already installed: $(psql --version 2>/dev/null || echo 'unknown')"
    else
        # Remove stale binary if exists but no cluster (from incomplete uninstall)
        if command -v psql &>/dev/null; then
            log_warn "psql found but PostgreSQL cluster not running - reinstalling"
            apt-get remove -y -qq postgresql postgresql-client 2>/dev/null || true
            rm -f /usr/bin/psql /usr/bin/pg_config /usr/bin/pg_dump /usr/bin/pg_isready /usr/bin/pg_basebackup 2>/dev/null || true
        fi
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
                # Verify actual server was installed (metapackage may skip due to broken dpkg state)
                pg_version=$(ls /etc/postgresql/ 2>/dev/null | head -1)
                if [ -z "$pg_version" ]; then
                    log_warn "Server not found after metapackage install - purging broken dpkg state"
                    # First pass: --force-all to remove broken packages from dpkg
                    for p in $(dpkg -l 2>/dev/null | awk '/postgresql/{print $2}' || true); do
                        [ -n "$p" ] && dpkg --purge --force-all "$p" 2>/dev/null || true
                    done
                    # Second pass: if dpkg database still has postgresql entries, edit status file directly
                    if dpkg -l 2>/dev/null | grep -q postgresql 2>/dev/null; then
                        log_warn "dpkg purge insufficient - directly cleaning dpkg status file"
                        for p in $(dpkg -l 2>/dev/null | awk '/postgresql/{print $2}' || true); do
                            [ -z "$p" ] && continue
                            sed -i "/^Package: $p$/,/^$/d" /var/lib/dpkg/status 2>/dev/null || true
                        done
                    fi
                    apt-get install -y -qq postgresql postgresql-client || {
                        log_error "Failed to install PostgreSQL despite retry"
                        return 1
                    }
                    pg_version=$(ls /etc/postgresql/ 2>/dev/null | head -1)
                    if [ -z "$pg_version" ]; then
                        log_error "PostgreSQL server still missing after purge+reinstall"
                        return 1
                    fi
                    log_info "PostgreSQL properly installed after dpkg cleanup"
                fi
                ;;
        esac
        log_success "PostgreSQL installed"
        # Fix dpkg and ensure psql is in PATH (alternatives may not fire when dpkg is broken)
        dpkg --configure -a --force-all 2>/dev/null || true
        apt-get install -f -y 2>/dev/null || true
        if ! command -v psql &>/dev/null; then
            local pg_bin
            pg_bin=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | head -1)
            if [ -n "$pg_bin" ] && [ -x "$pg_bin/psql" ]; then
                ln -sf "$pg_bin/psql" /usr/bin/psql
                ln -sf "$pg_bin/pg_isready" /usr/bin/pg_isready
                ln -sf "$pg_bin/pg_dump" /usr/bin/pg_dump
                ln -sf "$pg_bin/pg_config" /usr/bin/pg_config
                hash -r 2>/dev/null || true
            fi
        fi
    fi

    # Ensure the cluster actually exists and has a data directory
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

    # Set default timezone to UTC - affects all databases
    if grep -q "^timezone" "$pg_conf"; then
        sed -i "/^#*timezone/c\\timezone = 'Etc/UTC'" "$pg_conf"
    else
        echo "timezone = 'Etc/UTC'" >> "$pg_conf"
    fi

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
    local db_result
    
    # First create user if not exists
    if ! sudo -u postgres psql -t -c "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '${DB_USER}';" | grep -q 1; then
        log_info "Creating user ${DB_USER}..."
        db_result=$(sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';" 2>&1)
        if echo "$db_result" | grep -qi error; then
            log_error "Failed to create user: $db_result"
        fi
    else
        log_info "User ${DB_USER} exists, updating password..."
        sudo -u postgres psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';" 2>&1
    fi
    
    # Create database if not exists
    if ! sudo -u postgres psql -t -c "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}';" | grep -q 1; then
        log_info "Creating database ${DB_NAME}..."
        db_result=$(sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" 2>&1)
        if echo "$db_result" | grep -qi error; then
            log_error "Failed to create database: $db_result"
        fi
    else
        log_info "Database ${DB_NAME} exists"
    fi
    
    # Set timezone
    sudo -u postgres psql -c "ALTER DATABASE ${DB_NAME} SET timezone TO 'UTC';" 2>&1 || true
    
    log_success "Database '${DB_NAME}' created with user '${DB_USER}'"
}

role_shared_services_create_flight_db() {
    log_step "Creating BharatRadar Database (flight_db)"
    
    # Auto-generate password if not provided
    FLIGHT_DB_PASSWORD="${FLIGHT_DB_PASSWORD:-$(generate_secret)}"
    
    log_info "flight_db password: ${FLIGHT_DB_PASSWORD}"
    
    # Step 1: Create user
    log_info "[1/4] Creating user flight_db_user..."
    if sudo -u postgres psql -t -c "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'flight_db_user';" | grep -q 1; then
        log_info "User flight_db_user exists, updating password..."
        sudo -u postgres psql -c "ALTER USER flight_db_user WITH PASSWORD '${FLIGHT_DB_PASSWORD}';" 2>&1 || log_warn "Could not alter user"
    else
        log_info "Creating new user flight_db_user..."
        sudo -u postgres psql -c "CREATE ROLE flight_db_user LOGIN CREATEDB PASSWORD '${FLIGHT_DB_PASSWORD}';" 2>&1 || log_error "Failed to create user"
    fi
    log_success "[1/4] User created"
    
    # Step 2: Create database
    log_info "[2/4] Creating database flight_db..."
    if sudo -u postgres psql -t -c "SELECT 1 FROM pg_database WHERE datname = 'flight_db';" | grep -q 1; then
        log_info "Database flight_db already exists"
    else
        sudo -u postgres psql -c "CREATE DATABASE flight_db OWNER flight_db_user;" 2>&1 || log_error "Failed to create database"
    fi
    log_success "[2/4] Database created"
    
    # Step 3: Grant privileges
    log_info "[3/4] Granting privileges..."
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE flight_db TO flight_db_user;" 2>&1 || log_warn "Grant failed"
    sudo -u postgres psql -d flight_db -c "GRANT ALL ON SCHEMA public TO flight_db_user;" 2>&1 || log_warn "Schema grant failed"
    log_success "[3/4] Privileges granted"
    
    # Step 4: Set timezone
    log_info "[4/4] Setting timezone..."
    sudo -u postgres psql -c "ALTER DATABASE flight_db SET timezone TO 'UTC';" 2>&1 || log_warn "Timezone set failed"
    log_success "[4/4] Timezone set"
    
    # FINAL VERIFICATION
    log_step "Verifying database setup..."
    log_info "Checking databases..."
    sudo -u postgres psql -c "SELECT datname FROM pg_database WHERE datname IN ('${DB_NAME}', 'flight_db');" 2>&1 | while read line; do
        log_info "  $line"
    done
    
    log_info "Checking users..."
    sudo -u postgres psql -c "SELECT usename FROM pg_user WHERE usename IN ('${DB_USER}', 'flight_db_user');" 2>&1 | while read line; do
        log_info "  $line"
    done
    
    log_success "Database creation complete!"
    log_success "  Database: flight_db"
    log_success "  User: flight_db_user"
    log_success "  Password: ${FLIGHT_DB_PASSWORD}"
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
    log_step "Installing InfluxDB (Optional)"

    # Skip if influxdb is already working (binary exists AND service is active)
    if command -v influxd &>/dev/null && systemctl is-active --quiet influxdb 2>/dev/null; then
        log_info "InfluxDB already installed: $(influxd version 2>/dev/null || echo 'unknown')"
        return 0
    fi

    # If binary exists but service isn't running, remove stale installation
    if command -v influxd &>/dev/null; then
        log_warn "influxd found but service not running - removing stale installation"
        systemctl stop influxdb 2>/dev/null || true
        rm -f /usr/bin/influxd /usr/bin/influx 2>/dev/null || true
    fi
    
    # Purge any broken influxdb2 packages from dpkg before install
    log_info "Checking for broken InfluxDB packages in dpkg..."
    for p in influxdb2 influxdb2-cli influxdb; do
        if dpkg -l "$p" 2>/dev/null | grep -q "^.i\|^iU\|^iF" 2>/dev/null; then
            log_warn "Purging broken package: $p"
            dpkg --purge --force-all "$p" 2>/dev/null || true
            sed -i "/^Package: $p$/,/^$/d" /var/lib/dpkg/status 2>/dev/null || true
        fi
    done
    
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

    # Stop and wipe old data to ensure clean setup with new token
    systemctl stop influxdb 2>/dev/null || true
    rm -rf /var/lib/influxdb2/engine /var/lib/influxdb2/influxd.bolt 2>/dev/null || true
    systemctl reset-failed influxdb 2>/dev/null || true
    systemctl enable influxdb 2>/dev/null || true
    systemctl start influxdb 2>/dev/null || true

    sleep 5

    if ! systemctl is-active --quiet influxdb 2>/dev/null; then
        log_warn "InfluxDB service not available (may not be installed for this OS/arch)"
        return 0
    fi

    log_info "Setting up InfluxDB initial config..."

    local onboard_ok=false

    # Check if already onboarded
    local setup_status
    setup_status=$(curl -s http://localhost:8086/api/v2/setup 2>/dev/null)
    if echo "$setup_status" | grep -q '"allowed":true'; then
        log_info "InfluxDB needs initial setup, onboarding via API..."
        local api_result
        api_result=$(curl -s -X POST http://localhost:8086/api/v2/setup \
            -H "Content-Type: application/json" \
            -d "$(cat <<EOJSON
{
    "username": "admin",
    "password": "${INFLUXDB_ADMIN_TOKEN}",
    "org": "bharatradar",
    "bucket": "metrics",
    "token": "${INFLUXDB_ADMIN_TOKEN}"
}
EOJSON
)" 2>&1)
        if echo "$api_result" | grep -q '"code"\|"error"'; then
            log_error "InfluxDB API setup failed: $(echo "$api_result" | head -c 200)"
        else
            onboard_ok=true
        fi
    else
        log_warn "InfluxDB already onboarded (keeping existing config - token may differ)"
        onboard_ok=true
    fi

    if [ "$onboard_ok" = true ]; then
        log_success "InfluxDB configured"
    else
        log_warn "InfluxDB setup failed - verify manually"
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
    
    # Start MinIO - try with IP first, fallback to default
    if ! systemctl start minio 2>/dev/null; then
        log_warn "MinIO failed to start with specific IP, trying default..."
        # Fallback: remove IP binding and use default
        sed -i "s/--address \"\${DB_LISTEN_IP}:9000\"/--address \":9000\"/" /etc/systemd/system/minio.service
        sed -i "s/--console-address \"\${DB_LISTEN_IP}:9001\"/--console-address \":9001\"/" /etc/systemd/system/minio.service
        systemctl daemon-reload
        systemctl start minio
    fi

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

    # First verify flight_db exists
    log_info "Verifying flight_db exists..."
    local db_check
    db_check=$(sudo -u postgres psql -t -c "SELECT 1 FROM pg_database WHERE datname='flight_db';" 2>&1)
    
    if [ -z "$db_check" ] || echo "$db_check" | grep -q "0 rows"; then
        log_error "flight_db does not exist! Create it first with: sudo -u postgres psql -c 'CREATE DATABASE flight_db;'"
        return 1
    fi
    
    log_info "flight_db exists, checking tables..."

    # Check if flight_db already has data
    local existing_count
    existing_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM airports;" 2>/dev/null || echo "0")

    if [ "$existing_count" -gt 0 ] 2>/dev/null; then
        log_info "flight_db already has $existing_count airports, skipping initialization"
        return 0
    fi

    log_info "flight_db is empty, will initialize schema and seed data..."
    log_info "Downloading BharatRadar schema and seed data from GitHub..."

    # Download the schema and seed files from GitHub
    local scripts_dir="/tmp/bharatradar-db-scripts"
    mkdir -p "$scripts_dir"
    
    local schema_url="https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/schema.sql"
    local airports_url="https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/seed-airports.sql"
    local runways_url="https://raw.githubusercontent.com/bharatradar/infra/main/scripts/db/postgres/seed-runways.sql"
    
    log_info "Downloading schema.sql..."
    curl -sL "$schema_url" -o "$scripts_dir/schema.sql" || {
        log_error "Failed to download schema.sql"
        return 1
    }
    
    log_info "Downloading seed-airports.sql..."
    curl -sL "$airports_url" -o "$scripts_dir/seed-airports.sql" || {
        log_error "Failed to download seed-airports.sql"
        return 1
    }
    
    log_info "Downloading seed-runways.sql..."
    curl -sL "$runways_url" -o "$scripts_dir/seed-runways.sql" || {
        log_error "Failed to download seed-runways.sql"
        return 1
    }

    log_info "Creating schema..."
    local schema_result
    schema_result=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/schema.sql" 2>&1)
    if echo "$schema_result" | grep -qi "error"; then
        log_error "Schema creation failed: $schema_result"
    else
        log_success "Schema created"
    fi

    log_info "Seeding airports (this may take a minute)..."
    local airports_result
    airports_result=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/seed-airports.sql" 2>&1)
    log_success "Airports seeded"

    log_info "Seeding runways..."
    PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -f "$scripts_dir/seed-runways.sql" 2>&1 || true
    log_success "Runways seeded"

    # Verify counts
    local airport_count runway_count
    airport_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM airports;" 2>/dev/null | tr -d ' ')
    runway_count=$(PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h localhost -U flight_db_user -d flight_db -t -c "SELECT COUNT(*) FROM runways;" 2>/dev/null | tr -d ' ')

    log_success "BharatRadar database initialized!"
    log_success "  Airports: ${airport_count:-0}"
    log_success "  Runways: ${runway_count:-0}"

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
    echo "    Database: flight_db"
    echo "    User:     flight_db_user"
    echo "    Password: ${FLIGHT_DB_PASSWORD}"
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
    echo "    psql -h 127.0.0.1 -U ${DB_USER} -d ${DB_NAME} -W"
    echo "    psql -h 127.0.0.1 -U flight_db_user -d flight_db -W"
    echo "    redis-cli -h 127.0.0.1 -a '${REDIS_PASSWORD}' PING"
    echo "    mc alias set local http://${DB_LISTEN_IP}:9000 ${MINIO_ROOT_USER} ${MINIO_ROOT_PASSWORD}"
    echo "    mc ls local/history"
    echo ""
    echo -e "  ${CYAN}Credentials file:${NC} /etc/bharatradar/credentials/shared-services-*.txt"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_shared_services_verify() {
    log_step "Verifying All Services"
    local all_ok=true

    log_info "Testing PostgreSQL (user=${DB_USER})..."
    if PGPASSWORD="$DB_PASSWORD" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1;" 2>/dev/null | grep -q 1; then
        log_success "PostgreSQL: OK (password auth works)"
    else
        log_error "PostgreSQL: FAILED"
        all_ok=false
    fi

    log_info "Testing flight_db (user=flight_db_user)..."
    if PGPASSWORD="$FLIGHT_DB_PASSWORD" psql -h 127.0.0.1 -U flight_db_user -d flight_db -c "SELECT COUNT(*) FROM airports;" 2>/dev/null | grep -q "[0-9]"; then
        log_success "flight_db: OK (schema and seed data present)"
    else
        log_error "flight_db: FAILED"
        all_ok=false
    fi

    log_info "Testing Redis..."
    if redis-cli -a "$REDIS_PASSWORD" PING 2>/dev/null | grep -q PONG; then
        log_success "Redis: OK (password auth works)"
    else
        log_error "Redis: FAILED"
        all_ok=false
    fi

    log_info "Testing InfluxDB..."
    local influx_ok
    influx_ok=$(curl -s "http://localhost:8086/api/v2/query?org=bharatradar" \
        -H "Authorization: Token ${INFLUXDB_ADMIN_TOKEN}" \
        -H "Content-Type: application/vnd.flux" \
        -d "buckets()" 2>/dev/null)
    if echo "$influx_ok" | grep -q "name\|_value\|result"; then
        log_success "InfluxDB: OK (token auth works)"
    else
        log_error "InfluxDB: FAILED (response: $(echo "$influx_ok" | head -c 100))"
        all_ok=false
    fi

    log_info "Testing MinIO..."
    if curl -s http://localhost:9000/minio/health/live 2>/dev/null | grep -q "ok\|health"; then
        log_success "MinIO: OK"
    else
        if curl -s -o /dev/null -w "%{http_code}" http://localhost:9000/ 2>/dev/null | grep -q "200\|403"; then
            log_success "MinIO: OK (responding on port 9000)"
        else
            log_error "MinIO: FAILED"
            all_ok=false
        fi
    fi

    echo ""
    if [ "$all_ok" = true ]; then
        log_success "All services verified successfully!"
    else
        log_warn "Some services failed verification - check logs above"
    fi
    echo ""
}

role_shared_services_run() {
    require_root

    local install_failed=false
    local phases=(config packages postgresql flight_db redis influxdb minio flight_db_init verify save)

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

    # Phase: flight_db (create BharatRadar database)
    if ! checkpoint_completed "flight_db"; then
        role_shared_services_create_flight_db || log_warn "Flight DB creation failed"
        checkpoint_mark "flight_db"
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

    # Phase: verify (test all services with generated credentials)
    if ! checkpoint_completed "verify"; then
        role_shared_services_verify
        checkpoint_mark "verify"
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
