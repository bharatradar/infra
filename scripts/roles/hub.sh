#!/bin/bash
# Role: Primary Hub (First K3s server, runs all services)
# Installs K3s server with optional external PostgreSQL datastore,
# creates secrets, deploys all BharatRadar services.

set -euo pipefail

role_hub_collect_config() {
    log_step "Primary Hub Configuration"

    echo ""
    echo "  This is the first K3s server in your cluster."
    echo "  It runs all BharatRadar services and serves as the control plane."
    echo ""

    prompt_input "Base domain" "bharatradar.com" BASE_DOMAIN

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    prompt_input "Receiver latitude" "18.480718" READSB_LAT
    prompt_input "Receiver longitude" "73.898235" READSB_LON

    TZ_DEFAULT="Etc/UTC"
    prompt_input "Timezone" "$TZ_DEFAULT" TIMEZONE

    echo ""
    log_step "External Redis Configuration"
    echo ""
    echo "  The API service connects to Redis on the shared services host."
    echo "  Enter the Redis server details."
    echo ""

    prompt_input "Redis host IP" "${REDIS_HOST:-}" REDIS_HOST
    while [ -z "$REDIS_HOST" ] || ! validate_ip "$REDIS_HOST"; do
        log_error "Invalid IP address"
        prompt_input "Redis host IP" "${REDIS_HOST:-}" REDIS_HOST
    done

    prompt_input "Redis port" "${REDIS_PORT:-6379}" REDIS_PORT
    prompt_input "Redis password" "${REDIS_PASSWORD:-}" REDIS_PASSWORD
    while [ -z "$REDIS_PASSWORD" ]; do
        log_error "Password cannot be empty"
        prompt_input "Redis password" "" REDIS_PASSWORD
    done

    echo ""
    log_step "Storage Backend Configuration (History Service)"
    echo ""
    echo "  How should the history service store ADS-B data?"
    echo ""
    echo "  1) MinIO          - S3-compatible storage on your Pi (recommended)"
    echo "  2) Cloud Storage - Use existing rclone.conf (OneDrive, GCS, S3, etc.)"
    echo ""

    local storage_choice
    prompt_select "Storage backend" "MinIO" "Cloud Storage" storage_choice

    if [ "$storage_choice" = "Cloud Storage" ]; then
        echo ""
        echo "  Enter path to your existing rclone.conf file"
        echo "  (e.g., ~/.config/rclone/rclone.conf or /home/pi/.config/rclone/rclone.conf)"
        echo ""
        prompt_input "Path to rclone.conf" "~/.config/rclone/rclone.conf" RCLONE_CONFIG_PATH
        RCLONE_CONFIG_PATH=$(eval echo "$RCLONE_CONFIG_PATH")
        while [ ! -f "$RCLONE_CONFIG_PATH" ]; do
            log_error "File not found: $RCLONE_CONFIG_PATH"
            prompt_input "Path to rclone.conf" "" RCLONE_CONFIG_PATH
            RCLONE_CONFIG_PATH=$(eval echo "$RCLONE_CONFIG_PATH")
        done
        log_success "Using rclone config from: $RCLONE_CONFIG_PATH"
        MINIO_ENDPOINT=""
        MINIO_ROOT_USER=""
        MINIO_ROOT_PASSWORD=""
    else
        echo ""
        echo "  Configure MinIO on your shared services host (Pi):"
        echo ""
        prompt_input "MinIO endpoint (host:port)" "${MINIO_ENDPOINT:-192.168.200.15:9000}" MINIO_ENDPOINT
        prompt_input "MinIO access key" "${MINIO_ROOT_USER:-minioadmin}" MINIO_ROOT_USER
        prompt_input "MinIO secret key" "${MINIO_ROOT_PASSWORD:-}" MINIO_ROOT_PASSWORD
        while [ -z "$MINIO_ROOT_PASSWORD" ]; do
            log_error "Secret key cannot be empty"
            prompt_input "MinIO secret key" "" MINIO_ROOT_PASSWORD
        done
        RCLONE_CONFIG_PATH=""
    fi

    echo ""
    log_step "GitHub Container Registry (GHCR) Authentication"
    echo ""
    echo "  BharatRadar images are hosted on GitHub Container Registry."
    echo "  You need a GitHub Personal Access Token (PAT) with 'read:packages' scope."
    echo "  Create one at: https://github.com/settings/tokens"
    echo ""

    prompt_input "GitHub username" "" GHCR_USERNAME
    prompt_input "GitHub PAT (read:packages)" "" GHCR_PASSWORD

    while [ -z "$GHCR_USERNAME" ] || [ -z "$GHCR_PASSWORD" ]; do
        log_error "Both username and PAT are required"
        prompt_input "GitHub username" "$GHCR_USERNAME" GHCR_USERNAME
        prompt_input "GitHub PAT (read:packages)" "$GHCR_PASSWORD" GHCR_PASSWORD
    done

    echo ""
    log_step "Datastore Configuration"
    echo ""
    echo "  K3s stores cluster state in a database. Choose one:"
    echo ""
    echo "  1) Embedded etcd (simple, single server)"
    echo "  2) External PostgreSQL (required for HA multi-server)"
    echo ""

    local ds_choice
    prompt_select "Datastore type" \
        "Embedded etcd" \
        "External PostgreSQL" ds_choice

    if [ "$ds_choice" = "Embedded etcd" ]; then
        USE_EXTERNAL_DB=false
        DB_CONNECTION_STRING=""
        unset K3S_DATASTORE_ENDPOINT 2>/dev/null || true
        log_info "Using embedded etcd datastore"
    else
USE_EXTERNAL_DB=true
        echo ""
        log_info "Enter your PostgreSQL server details:"
        collect_pg_config "DB"
        log_info "Using external PostgreSQL: ${DB_HOST}:${DB_PORT}/${DB_DBNAME}"
    fi
    
    # Flight Database (flight_db) - used by ai-agents, telegram-bot, etc.
    echo ""
    log_step "Flight Database (flight_db)"
    echo ""
    echo "  BharatRadar uses a separate PostgreSQL database (flight_db)"
    echo "  for ai-agents, telegram-bot, and other services."
    echo ""
    echo "  This is created automatically during shared-services installation."
    echo "  The credentials are:"
    echo "    Database: flight_db"
    echo "    User: flight_db_user"
    echo "    Password: (shown at end of shared-services install)"
    echo ""
    FLIGHT_DB_HOST="${DB_HOST:-127.0.0.1}"
    FLIGHT_DB_PORT="${DB_PORT:-5432}"
    FLIGHT_DB_NAME="flight_db"
    FLIGHT_DB_USER="flight_db_user"
    FLIGHT_DB_PASSWORD="${FLIGHT_DB_PASSWORD:-}"
    
    if [ -z "$FLIGHT_DB_PASSWORD" ]; then
        log_info "Using default flight_db password: raga@098"
        FLIGHT_DB_PASSWORD="raga@098"
    fi
    
    log_success "flight_db configured: ${FLIGHT_DB_HOST}:${FLIGHT_DB_PORT}/${FLIGHT_DB_NAME}"
    
    # Telegram Bot Configuration
    echo ""
    log_step "Telegram Bot Configuration (Optional)"
    echo ""
    echo "  The telegram-bot service sends alerts to Telegram."
    echo "  Create a bot at @BotFather on Telegram to get a token."
    echo "  Leave empty to skip."
    echo ""
    prompt_input "Telegram bot token (leave empty to skip)" "" TELEGRAM_BOT_TOKEN
    prompt_input "Telegram chat ID (for alerts)" "" TELEGRAM_CHAT_ID
    
    # Groq API Configuration (for LLM-powered features)
    echo ""
    log_step "Groq API Configuration (Optional)"
    echo ""
    echo "  Groq provides fast LLM inference for ai-agents."
    echo "  Get a free API key at: https://console.groq.com/keys"
    echo "  Leave empty to skip."
    echo ""
    prompt_input "Groq API key (leave empty to skip)" "" GPT_API_KEY
    
    # Cloudflare AI Analytics Keys (dynamic - add as many as needed)
    echo ""
    log_step "Cloudflare AI Analytics Configuration (Optional)"
    echo ""
    echo "  Add your Cloudflare AI Web Analytics keys for tracking."
    echo "  Get them from: https://www.cloudflare.com/products/analytics/"
    echo ""
    
    CF_KEYS_JSON="["
    local add_more=true
    local key_count=0
    
    while [ "$add_more" = true ]; do
        if prompt_confirm "Add a Cloudflare AI key?"; then
            key_count=$((key_count + 1))
            echo ""
            echo "  Key #${key_count}:"
            local key_id key_token
            prompt_input "  Key ID (from Cloudflare dashboard)" "" key_id
            prompt_input "  Key token" "" key_token
            
            if [ -n "$key_id" ] && [ -n "$key_token" ]; then
                if [ "$CF_KEYS_JSON" = "[" ]; then
                    CF_KEYS_JSON="${CF_KEYS_JSON}{\"id\": \"${key_id}\", \"token\": \"${key_token}\"}"
                else
                    CF_KEYS_JSON="${CF_KEYS_JSON}, {\"id\": \"${key_id}\", \"token\": \"${key_token}\"}"
                fi
                log_success "Added Cloudflare AI key #${key_count}"
            fi
        else
            add_more=false
        fi
    done
    
    CF_KEYS_JSON="${CF_KEYS_JSON}]"
    
    if [ "$CF_KEYS_JSON" != "[]" ]; then
        log_info "Added ${key_count} Cloudflare AI key(s)"
    fi
    
    echo ""
    log_step "FRP Tunnel (Optional)"
    echo ""
    echo "  If your hub is behind a NAT/router and needs public access,"
    echo "  set up an FRP tunnel to a cloud server."
    echo "  If your hub has a public IP, skip this."
    echo ""

    if prompt_confirm "Set up FRP tunnel for public access?"; then
        prompt_input "FRP server public IP" "" FRP_SERVER

        while ! validate_ip "$FRP_SERVER"; do
            log_error "Invalid IP address"
            prompt_input "FRP server public IP" "$FRP_SERVER" FRP_SERVER
        done

        prompt_input "FRP authentication token" "" FRP_TOKEN

        while [ -z "$FRP_TOKEN" ]; do
            log_error "Token cannot be empty"
            prompt_input "FRP authentication token" "" FRP_TOKEN
        done

        FRP_ENABLED=true
    else
        FRP_ENABLED=false
        FRP_SERVER=""
        FRP_TOKEN=""
        log_info "No FRP tunnel. Services accessible on local network only."
    fi

    echo ""
    log_step "Keepalived VIP (Optional)"
    echo ""
    echo "  Keepalived provides a floating IP for automatic failover"
    echo "  when you add a second K3s server later."
    echo ""

    if prompt_confirm "Set up Keepalived VIP now?"; then
        KEEPALIVED_ENABLED=true
        source "${SCRIPT_DIR}/roles/keepalived.sh"
        keepalived_collect_config
    else
        KEEPALIVED_ENABLED=false
        KEEPALIVED_VIP=""
    fi

    echo ""
    log_step "Configuration Summary"
    echo -e "  Domain:    ${CYAN}${BASE_DOMAIN}${NC}"
    echo -e "  Lat/Lon:   ${CYAN}${READSB_LAT}, ${READSB_LON}${NC}"
    echo -e "  Timezone:  ${CYAN}${TIMEZONE}${NC}"
    echo -e "  ${CYAN}GHCR User: ${CYAN}${GHCR_USERNAME}${NC}"
    echo -e "  ${CYAN}Redis:     ${CYAN}${REDIS_HOST}:${REDIS_PORT}${NC}"
    if [ "$USE_EXTERNAL_DB" = true ]; then
        echo -e "  Datastore: ${CYAN}External PostgreSQL${NC}"
    else
        echo -e "  Datastore: ${CYAN}Embedded etcd${NC}"
    fi
    if [ "$FRP_ENABLED" = true ]; then
        echo -e "  FRP:       ${CYAN}${FRP_SERVER}:7000${NC}"
    else
        echo -e "  FRP:       ${CYAN}disabled${NC}"
    fi
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        echo -e "  VIP:       ${CYAN}${KEEPALIVED_VIP}${NC}"
    fi
    echo ""

    if ! prompt_confirm "Proceed with Primary Hub setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

# Helper: clear stale K3s data from PostgreSQL using direct connection
clear_stale_k3s_data() {
    local db_connection_string="$1"
    local db_host db_dbname db_dbuser db_dbpass db_dbport
    db_host=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d":" -f1)
    db_dbname=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d"/" -f2)
    db_dbuser=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f1 | cut -d":" -f1)
    db_dbpass=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f1 | cut -d":" -f2- | cut -d"/" -f1)
    db_dbport=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d"/" -f1 | cut -d":" -f2)

    log_info "Connecting to PostgreSQL at ${db_host}:${db_dbport}/${db_dbname}..."

    # Install psql if not available
    if ! command -v psql &>/dev/null; then
        log_info "Installing postgresql-client..."
        local os
        os=$(detect_os)
        case "$os" in
            debian|ubuntu|raspbian)
                apt-get install -y -qq postgresql-client 2>/dev/null || true
                ;;
            fedora|centos|rhel)
                dnf install -y -qq postgresql 2>/dev/null || true
                ;;
        esac
    fi

    if command -v psql &>/dev/null; then
        if PGPASSWORD="$db_dbpass" psql -h "$db_host" -p "$db_dbport" -U "$db_dbuser" -d "$db_dbname" -c 'DROP TABLE IF EXISTS kine;' 2>/dev/null; then
            log_success "Cleared stale data from PostgreSQL"
            return 0
        fi
    fi

    # Fallback: show manual command
    log_warn "Could not connect to PostgreSQL automatically."
    log_info "Run this command on ${db_host}:"
    echo ""
    echo -e "  ${CYAN}sudo -u postgres psql -c 'DROP TABLE kine;' ${db_dbname}${NC}"
    echo ""
    read -rp "Press Enter after clearing the database..." < /dev/tty
    return 0
}

# Helper: check if PostgreSQL has stale K3s data
has_stale_k3s_data() {
    local db_connection_string="$1"
    local db_host db_dbname db_dbuser db_dbpass db_dbport
    db_host=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d":" -f1)
    db_dbname=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d"/" -f2)
    db_dbuser=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f1 | cut -d":" -f1)
    db_dbpass=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f1 | cut -d":" -f2- | cut -d"/" -f1)
    db_dbport=$(echo "$db_connection_string" | sed "s|postgres://||" | cut -d"@" -f2 | cut -d"/" -f1 | cut -d":" -f2)

    if ! command -v psql &>/dev/null; then
        return 1
    fi

    local result
    result=$(PGPASSWORD="$db_dbpass" psql -h "$db_host" -p "$db_dbport" -U "$db_dbuser" -d "$db_dbname" -t -c "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'kine');" 2>/dev/null | tr -d ' ')
    [ "$result" = "t" ]
}

# Helper: stop k3s, clear stale data, restart and wait
recover_from_stale_data() {
    local db_connection_string="$1"

    log_warn "K3s bootstrap failed: stale data in PostgreSQL."
    log_info "The database contains data from a previous installation with a different token."

    # In silent mode, auto-clear without prompting
    if [ -n "${SILENT_MODE:-}" ] || { [ -n "${BASE_DOMAIN:-}" ] && [ -n "${REDIS_HOST:-}" ] && [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_PASSWORD:-}" ]; }; then
        log_info "Silent mode: auto-clearing stale K3s data and restarting..."
        clear_stale_k3s_data "$db_connection_string"
    else
        echo ""
        if ! prompt_confirm "Clear stale data and restart K3s?"; then
            log_error "K3s cannot start with stale data"
            exit 1
        fi
        clear_stale_k3s_data "$db_connection_string"
    fi

    systemctl stop k3s 2>/dev/null || true
    sleep 2

    log_info "Restarting K3s..."
    systemctl restart k3s

    log_info "Waiting for K3s to be ready..."
    for i in $(seq 1 60); do
        if kubectl cluster-info &>/dev/null 2>&1; then
            log_success "K3s server is ready"
            return 0
        fi
        sleep 2
    done
    log_error "K3s still not ready after restart"
    log_info "Check logs: journalctl -u k3s.service -e"
    exit 1
}

role_hub_install_k3s() {

    local k3s_installed=false
    if command -v k3s &>/dev/null; then
        k3s_installed=true
    fi

    if [ "$k3s_installed" = true ] && kubectl cluster-info &>/dev/null 2>&1; then
        log_warn "K3s is already installed and running"
        # In silent mode, auto-reinstall
        if [ -n "${SILENT_MODE:-}" ] || { [ -n "${BASE_DOMAIN:-}" ] && [ -n "${REDIS_HOST:-}" ] && [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_PASSWORD:-}" ]; }; then
            log_info "Silent mode: auto-reinstalling K3s..."
            /usr/local/bin/k3s-uninstall.sh 2>/dev/null || true
            rm -rf /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/kubelet
            k3s_installed=false
        elif prompt_confirm "Reinstall K3s? This will UNINSTALL the existing cluster"; then
            log_info "Uninstalling existing K3s..."
            /usr/local/bin/k3s-uninstall.sh 2>/dev/null || true
            rm -rf /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/kubelet
            k3s_installed=false
        else
            log_info "Keeping existing K3s installation"
            return 0
        fi
    fi

    # If K3s is installed but not running, diagnose the failure
    if [ "$k3s_installed" = true ]; then
        local svc_status
        svc_status=$(systemctl is-active k3s 2>/dev/null || echo "inactive")
        if [ "$svc_status" = "activating" ] || [ "$svc_status" = "failed" ]; then
            local journal_output
            journal_output=$(journalctl -u k3s.service --no-pager -n 20 2>/dev/null || true)
            if echo "$journal_output" | grep -q "bootstrap data already found and encrypted with different token"; then
                recover_from_stale_data "$DB_CONNECTION_STRING"
            fi
        fi
    fi

    # Pre-install check: detect stale data in PostgreSQL before installing K3s
    if [ "$USE_EXTERNAL_DB" = true ]; then
        # Install psql if needed for the pre-flight check
        if ! command -v psql &>/dev/null; then
            log_info "Installing postgresql-client for pre-flight check..."
            local os
            os=$(detect_os)
            case "$os" in
                debian|ubuntu|raspbian)
                    apt-get install -y -qq postgresql-client 2>/dev/null || true
                    ;;
                fedora|centos|rhel)
                    dnf install -y -qq postgresql 2>/dev/null || true
                    ;;
            esac
        fi

        if command -v psql &>/dev/null && has_stale_k3s_data "$DB_CONNECTION_STRING"; then
            log_warn "Stale K3s data found in PostgreSQL database."
            log_info "The database contains data from a previous installation with a different token."

            # In silent mode, auto-clear stale data without prompting
            if [ -n "${SILENT_MODE:-}" ] || { [ -n "${BASE_DOMAIN:-}" ] && [ -n "${REDIS_HOST:-}" ] && [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_PASSWORD:-}" ]; }; then
                log_info "Silent mode: auto-clearing stale K3s data..."
                clear_stale_k3s_data "$DB_CONNECTION_STRING"
            else
                echo ""
                if prompt_confirm "Clear stale data before installing K3s?"; then
                    clear_stale_k3s_data "$DB_CONNECTION_STRING"
                else
                    log_error "K3s install will likely fail with stale data"
                    exit 1
                fi
            fi
        fi
    fi

    K3S_TOKEN=$(generate_token)
    export K3S_TOKEN

    local k3s_args=()

    if [ "$USE_EXTERNAL_DB" = true ]; then
        export K3S_DATASTORE_ENDPOINT="$DB_CONNECTION_STRING"
        k3s_args=("--datastore-endpoint=${DB_CONNECTION_STRING}")
        log_info "Installing K3s with external datastore..."
    else
        k3s_args=("--cluster-init")
        log_info "Installing K3s with embedded etcd..."
    fi

    # Pre-download k3s binary to avoid GitHub redirect issues
    # The official installer sometimes fails on GitHub->AWS S3 redirects
    log_info "Pre-downloading K3s binary..."
    local k3s_arch
    k3s_arch=$(uname -m)
    case "$k3s_arch" in
        x86_64) k3s_arch="amd64" ;;
        aarch64) k3s_arch="arm64" ;;
        armv7l) k3s_arch="arm" ;;
    esac
    
    local k3s_version="v1.35.4+k3s1"
    local k3s_url="https://github.com/k3s-io/k3s/releases/download/${k3s_version}/k3s"
    
    if [ ! -f /usr/local/bin/k3s ]; then
        if ! curl -fsSL -o /usr/local/bin/k3s "${k3s_url}"; then
            log_warn "Failed to download from GitHub, trying direct fallback..."
            # Fallback: try without -L first, then with
            if ! curl -fsS -o /usr/local/bin/k3s "${k3s_url}"; then
                log_error "K3s binary download failed. Check internet connectivity."
                exit 1
            fi
        fi
        chmod +x /usr/local/bin/k3s
        log_success "K3s binary pre-downloaded"
    fi

    # Use INSTALL_K3S_SKIP_START to prevent installer from auto-starting the service
    # Use INSTALL_K3S_SKIP_DOWNLOAD since we already have the binary
    # This lets us clear stale data if it appears, then start manually
    # Install K3s using official installer (binary already pre-downloaded)
    log_info "Running K3s installer..."
    INSTALL_K3S_SKIP_START=true INSTALL_K3S_SKIP_DOWNLOAD=true \
        retry_with_backoff 3 sh -c "curl -sfL https://get.k3s.io | sh -s - server ${k3s_args[*]}" || {
        log_error "K3s installer failed after 3 attempts"
        return 1
    }

    log_info "Starting K3s..."
    systemctl start k3s

    log_info "Waiting for K3s to be ready..."
    for i in $(seq 1 60); do
        if kubectl cluster-info &>/dev/null 2>&1; then
            log_success "K3s server is ready"
            return 0
        fi

        # Check for bootstrap token mismatch every 10 iterations
        if [ $(( i % 10 )) -eq 0 ] && [ "$USE_EXTERNAL_DB" = true ]; then
            local journal_output
            journal_output=$(journalctl -u k3s.service --no-pager -n 5 2>/dev/null || true)
            if echo "$journal_output" | grep -q "bootstrap data already found and encrypted with different token"; then
                recover_from_stale_data "$DB_CONNECTION_STRING"
                i=0
                continue
            fi
        fi

        sleep 2
    done

    log_error "K3s failed to become ready"
    log_info "Check logs: journalctl -u k3s.service -e"
    exit 1
}

role_hub_create_secrets() {
    log_step "Creating Kubernetes Secrets"

    kubectl create namespace bharatradar --dry-run=client -o yaml | kubectl apply -f -

    # GHCR secret
    kubectl create secret docker-registry ghcr-secret \
        --docker-server=ghcr.io \
        --docker-username="$GHCR_USERNAME" \
        --docker-password="$GHCR_PASSWORD" \
        -n bharatradar \
        --dry-run=client -o yaml | kubectl apply -f -

    log_success "GHCR secret created"

    # TLS secret (self-signed placeholder)
    if ! kubectl get secret bharatradar-tls -n bharatradar &>/dev/null; then
        openssl req -x509 -nodes -days 365 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
            -keyout /tmp/tls.key -out /tmp/tls.crt \
            -subj "/CN=${BASE_DOMAIN}" 2>/dev/null

        kubectl create secret tls bharatradar-tls \
            --cert=/tmp/tls.crt --key=/tmp/tls.key \
            -n bharatradar 2>/dev/null || true

        rm -f /tmp/tls.crt /tmp/tls.key
        log_warn "Self-signed TLS certificate created (placeholder)"
    fi

    # Flight DB credentials secret
    if ! kubectl get secret flight-db-credentials -n bharatradar &>/dev/null; then
        kubectl create secret generic flight-db-credentials \
            --from-literal=host="${FLIGHT_DB_HOST:-192.168.200.12}" \
            --from-literal=port="${FLIGHT_DB_PORT:-5432}" \
            --from-literal=dbname="${FLIGHT_DB_NAME:-flight_db}" \
            --from-literal=username="${FLIGHT_DB_USER:-flight_db_user}" \
            --from-literal=password="${FLIGHT_DB_PASSWORD:-raga@098}" \
            -n bharatradar 2>/dev/null || true
        log_success "Flight DB credentials secret created"
    fi

    # Redis credentials secret
    if ! kubectl get secret redis-credentials -n bharatradar &>/dev/null; then
        kubectl create secret generic redis-credentials \
            --from-literal=host="${REDIS_HOST:-192.168.200.12}" \
            --from-literal=port="${REDIS_PORT:-6379}" \
            --from-literal=password="${REDIS_PASSWORD}" \
            -n bharatradar 2>/dev/null || true
        log_success "Redis credentials secret created"
    fi

    # Telegram bot credentials secret
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
        if ! kubectl get secret telegram-bot-credentials -n bharatradar &>/dev/null; then
            kubectl create secret generic telegram-bot-credentials \
                --from-literal=bot_token="${TELEGRAM_BOT_TOKEN}" \
                --from-literal=chat_id="${TELEGRAM_CHAT_ID:-}" \
                --from-literal=gpt_api_key="${GPT_API_KEY:-}" \
                --from-literal=gpt_model="llama-3.1-70b-versatile" \
                -n bharatradar 2>/dev/null || true
            log_success "Telegram bot credentials secret created"
        fi
    fi

    # InfluxDB credentials secret
    if ! kubectl get secret influxdb-credentials -n bharatradar &>/dev/null; then
        local influx_token="${INFLUXDB_ADMIN_TOKEN:-token_not_configured}"
        kubectl create secret generic influxdb-credentials \
            --from-literal=url="http://192.168.200.12:8086" \
            --from-literal=token="$influx_token" \
            --from-literal=org="bharatradar" \
            --from-literal=bucket="metrics" \
            -n bharatradar 2>/dev/null || true
        log_success "InfluxDB credentials secret created"
    fi

    # Cloudflare AI Analytics credentials secret
    if [ "$CF_KEYS_JSON" != "[]" ]; then
        if ! kubectl get secret cloudflare-credentials -n bharatradar &>/dev/null; then
            kubectl create secret generic cloudflare-credentials \
                --from-literal=keys="$CF_KEYS_JSON" \
                -n bharatradar 2>/dev/null || true
            log_success "Cloudflare AI credentials secret created"
        fi
    fi

    # Rclone secret - use existing config path or create from MinIO credentials
    if ! kubectl get secret bharatradar-rclone -n bharatradar &>/dev/null; then
        if [ -n "${RCLONE_CONFIG_PATH:-}" ] && [ -f "$RCLONE_CONFIG_PATH" ]; then
            kubectl create secret generic bharatradar-rclone \
                --from-file=rclone.conf="$RCLONE_CONFIG_PATH" \
                -n bharatradar --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
            log_success "Rclone secret created from: $RCLONE_CONFIG_PATH"
        elif [ -n "${MINIO_ENDPOINT:-}" ] && [ -n "${MINIO_ROOT_USER:-}" ] && [ -n "${MINIO_ROOT_PASSWORD:-}" ]; then
            kubectl create secret generic bharatradar-rclone \
                --from-literal=rclone.conf="[bharatradar]
type = s3
provider = Other
access_key_id = ${MINIO_ROOT_USER}
secret_access_key = ${MINIO_ROOT_PASSWORD}
endpoint = http://${MINIO_ENDPOINT}" \
                -n bharatradar --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
            log_success "Rclone secret created for MinIO (${MINIO_ENDPOINT})"
        else
            kubectl create secret generic bharatradar-rclone \
                --from-literal=rclone.conf="[bharatradar]
type = s3
provider = Other
access_key_id = YOUR_ACCESS_KEY
secret_access_key = YOUR_SECRET_KEY
endpoint = http://YOUR_MINIO_ENDPOINT:9000" \
                -n bharatradar --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
            log_warn "Rclone secret created with placeholder (set MINIO_ENDPOINT in config.env)"
        fi
    fi
}

role_hub_deploy_services() {
    log_step "Deploying BharatRadar Services"

    source "${SCRIPT_DIR}/helpers/templating.sh"

    # Generate API salt
    API_SALT=$(openssl rand -hex 64)

    # Initialize overlay
    templating_init

    # Generate templated manifests
    templating_generate_kustomization \
        "${SCRIPT_DIR}/../manifests/default" \
        "$BASE_DOMAIN" "$READSB_LAT" "$READSB_LON" "$TIMEZONE" \
        "${FRP_SERVER:-}" "$API_SALT" "${FRP_TOKEN:-}"

    # Install kustomize if needed
    if ! command -v kustomize &>/dev/null; then
        log_info "Installing kustomize..."
        curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
        mv kustomize /usr/local/bin/
    fi

    # Apply shared resources first
    kubectl apply -f "${OVERLAY_DIR}/resources.yaml" -n bharatradar 2>/dev/null || true

    # Helper: deploy a component and strip ServiceMonitor resources
    deploy_component() {
        local component="$1"
        
        # Check for directory-based component (e.g., api/default, ingest/default)
        local manifest_dir="${SCRIPT_DIR}/../manifests/default/${component}/default"
        
        # Check for standalone YAML file (e.g., telegram-bot.yaml, ai-agents.yaml)
        local manifest_file="${SCRIPT_DIR}/../manifests/default/${component}.yaml"
        
        if [ -d "$manifest_dir" ]; then
            # Deploy from kustomize directory
            log_info "Deploying ${component}..."
            local built
            built=$(kustomize build "$manifest_dir" 2>&1)
            local rc=$?
            if [ $rc -ne 0 ]; then
                log_error "kustomize build failed for ${component}:"
                echo "$built"
                return 1
            fi
            
            echo "$built" | awk 'BEGIN{RS="---"; ORS="---"}
                !/kind: ServiceMonitor/ &&
                !/kind: ImagePolicy/ &&
                !/kind: ImageRepository/ &&
                !/kind: ImageUpdateAutomation/ &&
                /kind:/' | kubectl apply -f - -n bharatradar 2>&1
            rc=$?
            if [ $rc -ne 0 ]; then
                log_error "kubectl apply failed for ${component}"
                return 1
            fi
            
            log_success "${component} deployed"
        elif [ -f "$manifest_file" ]; then
            # Deploy standalone YAML file
            log_info "Deploying ${component}..."
            kubectl apply -f "$manifest_file" -n bharatradar 2>&1
            local rc=$?
            if [ $rc -ne 0 ]; then
                log_error "kubectl apply failed for ${component}"
                return 1
            fi
            log_success "${component} deployed"
        else
            log_info "Skipping ${component} - not found"
            return 0
        fi
    }

    # Helper: wait for a deployment to be ready
    wait_for_deployment() {
        local name="$1"
        log_info "Waiting for ${name} to be ready..."
        kubectl rollout status "deployment/${name}" -n bharatradar --timeout=120s 2>/dev/null || true
    }

    # Phase 1: Core services (no external dependencies)
    deploy_component "api" || return 1
    deploy_component "website" || return 1

    # Phase 2: ADS-B data sources (must exist before haproxy can resolve them)
    deploy_component "ingest" || return 1
    deploy_component "hub" || return 1
    deploy_component "external" || return 1
    deploy_component "reapi" || return 1
    deploy_component "mlat" || return 1
    deploy_component "planes" || return 1

    # Phase 3: Wait for data source deployments to be ready
    wait_for_deployment "ingest-readsb"
    wait_for_deployment "hub-readsb"
    wait_for_deployment "external-readsb"
    wait_for_deployment "reapi-readsb"
    wait_for_deployment "planes-readsb"
    wait_for_deployment "mlat-mlat-server"
    wait_for_deployment "api"

    # Give CoreDNS time to register services
    log_info "Waiting for CoreDNS to register services..."
    sleep 10

    # Phase 4: Dependent services (require data sources to be resolvable)
    deploy_component "mlat-map" || return 1
    deploy_component "history" || return 1
    deploy_component "schedule-downloader" || return 1

    # Phase 5: Additional services (telegram-bot, ai-agents, cortex-webapp, flight-tracker)
    deploy_component "telegram-bot" || true
    deploy_component "ai-agents" || true
    deploy_component "cortex-webapp" || true
    deploy_component "flight-tracker" || true
    deploy_component "redis" || true

    # Wait for additional services
    wait_for_deployment "telegram-bot" || true
    wait_for_deployment "ai-agents" || true
    wait_for_deployment "cortex-webapp" || true
    wait_for_deployment "flight-tracker" || true
    # NOTE: haproxy removed — beast/MLAT now use direct LoadBalancer services (Option C)

    log_success "All services deployed"

    # Apply runtime patches
    if [ -n "${FRP_SERVER:-}" ]; then
        log_info "Configuring ingest to connect to FRP server..."
        kubectl set env deployment/ingest-readsb \
            READSB_NET_CONNECTOR="${FRP_SERVER},30004,beast_in" \
            -n bharatradar 2>/dev/null || true
    fi

    # Apply planes env patch
    kubectl set env deployment/planes-readsb \
        READSB_LAT="${READSB_LAT}" \
        READSB_LON="${READSB_LON}" \
        TZ="${TIMEZONE}" \
        -n bharatradar 2>/dev/null || true

    # Apply API env patch (Redis URL + domain)
    kubectl set env deployment/api \
        MY_DOMAIN="my.${BASE_DOMAIN}" \
        ADSBLOL_REDIS_HOST="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}" \
        -n bharatradar 2>/dev/null || true

    # Apply API salt ConfigMap
    kubectl create configmap api-salts \
        --from-literal="ADSBLOL_API_SALT_MY=${API_SALT}" \
        -n bharatradar \
        --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

    log_success "All services deployed"

    # Install FRP client if enabled
    if [ "$FRP_ENABLED" = true ]; then
        role_hub_setup_frpc
    fi

    # Install keepalived if enabled
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        source "${SCRIPT_DIR}/roles/keepalived.sh"
        keepalived_install
        keepalived_configure
    fi
}

role_hub_setup_frpc() {
    log_step "Setting up FRP Client"

    # Use 127.0.0.1 for FRP since K3s LoadBalancer services are accessible
    # via kube-proxy on localhost, regardless of VIP configuration.
    local frp_local_ip="127.0.0.1"

    local arch
    arch=$(detect_arch)

    if command -v frpc &>/dev/null; then
        log_info "frpc already installed, updating config..."
    else
        log_info "Downloading frpc v${FRP_VERSION}..."
        local frp_url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"
        wget -q "$frp_url" -O /tmp/frp.tar.gz
        tar xzf /tmp/frp.tar.gz -C /tmp
        cp "/tmp/frp_${FRP_VERSION}_linux_${arch}/frpc" /usr/local/bin/frpc
        chmod +x /usr/local/bin/frpc
        rm -rf /tmp/frp_*
    fi

    local domain_list=""
    local subdomains=("map" "mlat" "history" "api" "my" "feed" "ws" "cortex" "grafana" "telegram")
    for sub in "${subdomains[@]}"; do
        domain_list+="    \"${sub}.${BASE_DOMAIN}\",
"
    done

    cat > /etc/frpc.toml <<EOF
serverAddr = "${FRP_SERVER}"
serverPort = 7000

auth.method = "token"
auth.token = "${FRP_TOKEN}"

[[proxies]]
name = "web-ui"
type = "http"
localIP = "${frp_local_ip}"
localPort = 80
customDomains = [
${domain_list}    "${BASE_DOMAIN}"
]

[[proxies]]
name = "feed-beast-30004"
type = "tcp"
localIP = "${frp_local_ip}"
localPort = 30004
remotePort = 30004

[[proxies]]
name = "feed-beast-30005"
type = "tcp"
localIP = "${frp_local_ip}"
localPort = 30005
remotePort = 30005

[[proxies]]
name = "feed-mlat-31090"
type = "tcp"
localIP = "${frp_local_ip}"
localPort = 31090
remotePort = 31090
EOF

    cat > /etc/systemd/system/frpc.service <<EOF
[Unit]
Description=FRP Client (BharatRadar Hub)
After=network.target k3s.service
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c /etc/frpc.toml
Restart=always
RestartSec=5
User=root
Group=root
StandardOutput=journal
StandardError=journal
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable frpc
    systemctl restart frpc

    sleep 3

    if systemctl is-active --quiet frpc; then
        log_success "FRP client started"
    else
        log_warn "FRP client may still be connecting. Check: journalctl -u frpc -f"
    fi
}

role_hub_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar Primary Hub Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 4.0.0

ROLE=hub
BASE_DOMAIN="${BASE_DOMAIN}"
READSB_LAT="${READSB_LAT}"
READSB_LON="${READSB_LON}"
TIMEZONE="${TIMEZONE}"
GHCR_USERNAME="${GHCR_USERNAME}"
REDIS_HOST="${REDIS_HOST}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
MINIO_ENDPOINT="${MINIO_ENDPOINT}"
MINIO_ROOT_USER="${MINIO_ROOT_USER}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}"
USE_EXTERNAL_DB="${USE_EXTERNAL_DB}"
DB_HOST="${DB_HOST:-}"
DB_PORT="${DB_PORT:-5432}"
DB_DBNAME="${DB_DBNAME:-k3s}"
DB_DBUSER="${DB_DBUSER:-k3s}"
DB_CONNECTION_STRING="${DB_CONNECTION_STRING:-}"
FRP_ENABLED="${FRP_ENABLED}"
FRP_SERVER="${FRP_SERVER:-}"
K3S_TOKEN="${K3S_TOKEN:-}"
KEEPALIVED_ENABLED="${KEEPALIVED_ENABLED}"
KEEPALIVED_VIP="${KEEPALIVED_VIP:-}"
KEEPALIVED_PRIORITY=100
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/config.env"

    # Save credentials file for reference
    local creds_dir="/etc/bharatradar/credentials"
    local creds_file="${creds_dir}/hub-$(date -u +"%Y%m%d-%H%M%S").txt"
    mkdir -p "$creds_dir"

    cat > "$creds_file" <<EOF
============================================================
  Primary Hub Credentials
  Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
  Host: $(hostname)
============================================================

  GitHub Container Registry (GHCR):
    Username: ${GHCR_USERNAME}
    Token:    ${GHCR_PASSWORD}

  K3s Cluster:
    Token:    ${K3S_TOKEN}

  Redis (external):
    Host:     ${REDIS_HOST}
    Port:     ${REDIS_PORT:-6379}
    Password: ${REDIS_PASSWORD}

  MinIO (S3-compatible storage):
    Endpoint: ${MINIO_ENDPOINT}
    User:     ${MINIO_ROOT_USER}
    Password: ${MINIO_ROOT_PASSWORD}
EOF

    if [ "$USE_EXTERNAL_DB" = true ]; then
        cat >> "$creds_file" <<EOF

  PostgreSQL (external datastore):
    Host:     ${DB_HOST:-}
    Port:     ${DB_PORT:-5432}
    Database: ${DB_DBNAME:-k3s}
    User:     ${DB_DBUSER:-k3s}
    Password: ${DB_PASSWORD:-}
    URL:      ${DB_CONNECTION_STRING:-}
EOF
    else
        cat >> "$creds_file" <<EOF

  K3s Datastore: Embedded etcd (no external DB)
EOF
    fi

    if [ "${FRP_ENABLED}" = "true" ]; then
        cat >> "$creds_file" <<EOF

  FRP Tunnel:
    Server:   ${FRP_SERVER:-}
    Token:    ${FRP_TOKEN:-}
EOF
    fi

    if [ -n "${FLIGHT_DB_HOST:-}" ]; then
        cat >> "$creds_file" <<EOF

  Flight Database (flight_db):
    Host:     ${FLIGHT_DB_HOST}
    Port:     ${FLIGHT_DB_PORT:-5432}
    Database: ${FLIGHT_DB_NAME:-flight_db}
    User:     ${FLIGHT_DB_USER:-flight_db_user}
    Password: ${FLIGHT_DB_PASSWORD:-raga@098}
EOF
    fi

    cat >> "$creds_file" <<EOF

============================================================
EOF

    chmod 600 "$creds_file"
    log_success "Credentials saved to ${creds_file}"
}

# Verify all pods are running
role_hub_verify() {
    log_info "Checking pod status..."

    local max_wait=120
    local waited=0
    local all_ready=false

    while [ $waited -lt $max_wait ]; do
        local not_ready
        not_ready=$(kubectl get pods -n bharatradar --no-headers 2>/dev/null | grep -v "Running\|Completed" | wc -l)

        if [ "$not_ready" -eq 0 ]; then
            all_ready=true
            break
        fi

        if [ $((waited % 10)) -eq 0 ]; then
            log_info "Waiting for pods... (${not_ready} not ready)"
        fi

        sleep 2
        waited=$((waited + 2))
    done

    if [ "$all_ready" = true ]; then
        log_success "All pods are running"
        return 0
    else
        log_warn "Some pods are still starting"
        kubectl get pods -n bharatradar
        return 1
    fi
}

role_hub_post_install() {
    local local_ip
    local_ip=$(hostname -I | awk '{print $1}')

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Primary Hub Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}URLs:${NC}"
    echo "    Map:       https://map.${BASE_DOMAIN}"
    echo "    MLAT Map:  https://mlat.${BASE_DOMAIN}"
    echo "    API:       https://api.${BASE_DOMAIN}"
    echo "    My Map:    https://my.${BASE_DOMAIN}"
    echo "    History:   https://history.${BASE_DOMAIN}"
    echo ""
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        echo -e "  ${CYAN}VIP:${NC} ${KEEPALIVED_VIP} (this server is MASTER)"
    fi
    if [ "$FRP_ENABLED" = true ]; then
        echo -e "  ${CYAN}FRP Tunnel:${NC} ${FRP_SERVER}:7000"
    else
        echo -e "  ${CYAN}Local Access:${NC} http://${local_ip}"
    fi
    echo ""
    echo -e "  ${CYAN}To add more nodes later:${NC}"
    echo "    HA Server:   curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- ha-server"
    echo "    Worker Node: curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- worker"
    echo ""
    echo -e "  ${CYAN}Cluster token (save this!):${NC} ${K3S_TOKEN}"
    echo ""
    echo -e "  ${CYAN}Credentials file:${NC} /etc/bharatradar/credentials/hub-*.txt"
    echo ""
    echo -e "  ${CYAN}Useful commands:${NC}"
    echo "    kubectl get pods -n bharatradar"
    echo "    sudo bharatradar-install status"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_hub_run() {
    require_root

    # Show resume banner if we have saved progress
    show_resume_banner

    # PHASE 1: Configuration
    if ! checkpoint_completed "config"; then
        log_step "Phase 1/5: Configuration"
        echo ""

        # Load existing config.env defaults if present (for re-run after completion)
        if [ -f /etc/bharatradar/config.env ]; then
            source /etc/bharatradar/config.env
        fi

        # Skip interactive prompts if all required vars are already set (silent mode)
        if [ -n "${BASE_DOMAIN:-}" ] && [ -n "${REDIS_HOST:-}" ] && [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_PASSWORD:-}" ]; then
            log_info "Using provided configuration (silent mode)"
            # Ensure defaults for optional vars
            READSB_LAT="${READSB_LAT:-18.480718}"
            READSB_LON="${READSB_LON:-73.898235}"
            TIMEZONE="${TIMEZONE:-UTC}"
            REDIS_PORT="${REDIS_PORT:-6379}"
            USE_EXTERNAL_DB="${USE_EXTERNAL_DB:-false}"
            FRP_ENABLED="${FRP_ENABLED:-false}"
            KEEPALIVED_ENABLED="${KEEPALIVED_ENABLED:-false}"
            # Build DB connection string if using external DB and not already set
            if [ "$USE_EXTERNAL_DB" = true ] && [ -z "${DB_CONNECTION_STRING:-}" ]; then
                if [ -n "${DB_HOST:-}" ] && [ -n "${DB_DBPASS:-}" ]; then
                    DB_CONNECTION_STRING="postgres://${DB_DBUSER:-k3s}:${DB_DBPASS}@${DB_HOST}:${DB_PORT:-5432}/${DB_DBNAME:-k3s}"
                fi
            fi
        else
            role_hub_collect_config
        fi

        # Save all answers to partial config for resume support
        save_config_value "ROLE" "hub"
        save_config_value "BASE_DOMAIN" "${BASE_DOMAIN}"
        save_config_value "READSB_LAT" "${READSB_LAT}"
        save_config_value "READSB_LON" "${READSB_LON}"
        save_config_value "TIMEZONE" "${TIMEZONE}"
        save_config_value "GHCR_USERNAME" "${GHCR_USERNAME}"
        save_config_value "GHCR_PASSWORD" "${GHCR_PASSWORD}"
        save_config_value "REDIS_HOST" "${REDIS_HOST}"
        save_config_value "REDIS_PORT" "${REDIS_PORT:-6379}"
        save_config_value "REDIS_PASSWORD" "${REDIS_PASSWORD}"
        save_config_value "MINIO_ENDPOINT" "${MINIO_ENDPOINT}"
        save_config_value "MINIO_ROOT_USER" "${MINIO_ROOT_USER}"
        save_config_value "MINIO_ROOT_PASSWORD" "${MINIO_ROOT_PASSWORD}"
        save_config_value "RCLONE_CONFIG_PATH" "${RCLONE_CONFIG_PATH:-}"
        save_config_value "USE_EXTERNAL_DB" "${USE_EXTERNAL_DB}"
        save_config_value "DB_HOST" "${DB_HOST:-}"
        save_config_value "DB_PORT" "${DB_PORT:-5432}"
        save_config_value "DB_DBNAME" "${DB_DBNAME:-k3s}"
        save_config_value "DB_DBUSER" "${DB_DBUSER:-k3s}"
        save_config_value "DB_CONNECTION_STRING" "${DB_CONNECTION_STRING:-}"
        save_config_value "FLIGHT_DB_HOST" "${FLIGHT_DB_HOST:-}"
        save_config_value "FLIGHT_DB_PORT" "${FLIGHT_DB_PORT:-5432}"
        save_config_value "FLIGHT_DB_NAME" "${FLIGHT_DB_NAME:-flight_db}"
        save_config_value "FLIGHT_DB_USER" "${FLIGHT_DB_USER:-flight_db_user}"
        save_config_value "FLIGHT_DB_PASSWORD" "${FLIGHT_DB_PASSWORD:-}"
        save_config_value "TELEGRAM_BOT_TOKEN" "${TELEGRAM_BOT_TOKEN:-}"
        save_config_value "TELEGRAM_CHAT_ID" "${TELEGRAM_CHAT_ID:-}"
        save_config_value "GPT_API_KEY" "${GPT_API_KEY:-}"
        save_config_value "CF_KEYS_JSON" "${CF_KEYS_JSON:-}"
        save_config_value "FRP_ENABLED" "${FRP_ENABLED}"
        save_config_value "FRP_SERVER" "${FRP_SERVER:-}"
        save_config_value "FRP_TOKEN" "${FRP_TOKEN:-}"
        save_config_value "KEEPALIVED_ENABLED" "${KEEPALIVED_ENABLED}"
        save_config_value "KEEPALIVED_VIP" "${KEEPALIVED_VIP:-}"
        save_config_value "KEEPALIVED_PRIORITY" "100"
        save_config_value "K3S_TOKEN" "${K3S_TOKEN:-}"

        checkpoint_mark "config"
        log_success "Configuration saved"
    else
        log_step "Phase 1/5: Configuration"
        log_success "Already completed - loading saved answers"
        load_partial_config || {
            log_error "Cannot load saved configuration from ${partial_config}"
            exit 1
        }
    fi

    # PHASE 2: K3s Installation
    if ! checkpoint_completed "k3s"; then
        log_step "Phase 2/5: K3s Installation"
        echo ""

        role_hub_install_k3s || {
            log_error "Phase 2 FAILED: K3s installation failed"
            echo ""
            echo "  To retry from this step, run:"
            echo "    sudo ./bharatradar-install hub"
            echo ""
            echo "  To diagnose:"
            echo "    journalctl -u k3s -n 50 --no-pager"
            echo ""
            exit 1
        }

        # Save the generated token for resume consistency
        save_config_value "K3S_TOKEN" "${K3S_TOKEN}"

        checkpoint_mark "k3s"
        log_success "K3s installed"
    else
        log_step "Phase 2/5: K3s Installation"
        log_success "Already completed"
    fi

    # PHASE 3: Secrets
    if ! checkpoint_completed "secrets"; then
        log_step "Phase 3/5: Secrets"
        echo ""

        setup_kubectl
        install_bharatradar_cli

        role_hub_create_secrets || {
            log_error "Phase 3 FAILED: Could not create secrets"
            echo ""
            echo "  To retry from this step, run:"
            echo "    sudo ./bharatradar-install hub"
            echo ""
            exit 1
        }

        checkpoint_mark "secrets"
        log_success "Secrets created"
    else
        log_step "Phase 3/5: Secrets"
        log_success "Already completed"
    fi

    # PHASE 4: Deploy Services
    if ! checkpoint_completed "deploy"; then
        log_step "Phase 4/5: Deploy Services"
        echo ""

        role_hub_deploy_services || {
            log_error "Phase 4 FAILED: Deployment failed"
            echo ""
            echo "  To retry from this step, run:"
            echo "    sudo ./bharatradar-install hub"
            echo ""
            echo "  To check status:"
            echo "    kubectl get pods -n bharatradar"
            echo ""
            exit 1
        }

        checkpoint_mark "deploy"
        log_success "Services deployed"
    else
        log_step "Phase 4/5: Deploy Services"
        log_success "Already completed"
    fi

    # PHASE 5: Verify
    if ! checkpoint_completed "verify"; then
        log_step "Phase 5/5: Verification"
        echo ""

        role_hub_verify || {
            log_warn "Phase 5: Some pods are not ready yet"
            echo ""
            echo "  To retry verification, run:"
            echo "    sudo ./bharatradar-install hub"
            echo ""
            echo "  To check status manually:"
            echo "    kubectl get pods -n bharatradar"
            echo ""
            exit 1
        }

        checkpoint_mark "verify"
        log_success "Verification passed"
    else
        log_step "Phase 5/5: Verification"
        log_success "Already completed"
    fi

    # Finalize: move partial config to permanent config
    if [ -f "$partial_config" ]; then
        cp "$partial_config" /etc/bharatradar/config.env
        chmod 600 /etc/bharatradar/config.env
    fi
    checkpoint_clear

    role_hub_post_install
}
