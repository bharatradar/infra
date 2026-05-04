#!/bin/bash
# Role: Feeder Pi (RTL-SDR standalone receiver, NOT K3s)
# Installs readsb, mlat-client, and systemd services for ADS-B/MLAT feeding.

set -euo pipefail

FEEDER_VERSION="0.68.1"

role_feeder_collect_config() {
    log_step "Feeder Pi Configuration"

    echo ""
    echo "  This sets up a standalone RTL-SDR receiver that feeds data"
    echo "  to your BharatRadar hub via the FRP server."
    echo ""

    prompt_input "Feeder server domain" "feed.bharatradar.com" FEEDER_DOMAIN

    while ! validate_domain "$FEEDER_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Feeder server domain" "$FEEDER_DOMAIN" FEEDER_DOMAIN
    done

    prompt_input "Receiver latitude" "18.480718" READSB_LAT
    prompt_input "Receiver longitude" "73.898235" READSB_LON
    prompt_input "Antenna altitude (meters)" "10" FEEDER_ALT_M

    # Generate or use existing UUID
    if [ -f /etc/bharat-radar-id ]; then
        EXISTING_UUID=$(cat /etc/bharat-radar-id)
        log_info "Existing UUID found: ${EXISTING_UUID}"
        if ! prompt_confirm "Use existing UUID?"; then
            EXISTING_UUID=""
        fi
    fi

    if [ -z "${EXISTING_UUID:-}" ]; then
        FEEDER_UUID="BR-$(openssl rand -hex 9)"
        log_info "Generated new UUID: ${FEEDER_UUID}"
    else
        FEEDER_UUID="${EXISTING_UUID}"
    fi

    prompt_input "Feeder name (shows on MLAT map)" "Feeder-${FEEDER_UUID:3:6}" FEEDER_NAME

    echo ""
    log_step "SDR Device Configuration"

    echo ""
    echo "  Detecting RTL-SDR devices..."
    if command -v rtl_test &>/dev/null; then
        local serials
        serials=$(rtl_test 2>&1 | grep "Found" || echo "No devices found")
        log_info "SDR detection: ${serials}"
    else
        log_warn "rtl_test not installed. Will install with readsb."
    fi

    prompt_input "SDR serial number (or '0' for first device)" "0" SDR_SERIAL

    echo ""
    log_step "MLAT Configuration"
    echo ""

    if prompt_confirm "Show your feeder on the public MLAT map?"; then
        MLAT_PRIVACY=""
    else
        MLAT_PRIVACY="--privacy"
        log_info "Privacy mode enabled (hidden from MLAT map)"
    fi

    echo ""
    log_step "Configuration Summary"
    echo -e "  Server:      ${CYAN}${FEEDER_DOMAIN}${NC}"
    echo -e "  Lat/Lon:     ${CYAN}${READSB_LAT}, ${READSB_LON}${NC}"
    echo -e "  Altitude:    ${CYAN}${FEEDER_ALT_M}m${NC}"
    echo -e "  UUID:        ${CYAN}${FEEDER_UUID}${NC}"
    echo -e "  Name:        ${CYAN}${FEEDER_NAME}${NC}"
    echo -e "  SDR Serial:  ${CYAN}${SDR_SERIAL}${NC}"
    if [ -n "$MLAT_PRIVACY" ]; then
        echo -e "  MLAT Map:    ${CYAN}hidden${NC}"
    else
        echo -e "  MLAT Map:    ${CYAN}visible${NC}"
    fi
    echo ""

    if ! prompt_confirm "Proceed with Feeder Pi setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_feeder_install_packages() {
    log_step "Installing Packages"

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get update -qq
            apt-get install -y -qq git build-essential debhelper librtlsdr-dev \
                rtl-sdr pkg-config python3 python3-requests socat
            ;;
        *)
            log_error "Unsupported OS: $os"
            exit 1
            ;;
    esac

    log_success "Base packages installed"
}

role_feeder_install_readsb() {
    log_step "Installing readsb"

    if command -v readsb &>/dev/null; then
        log_info "readsb already installed: $(readsb --version 2>/dev/null || echo 'unknown version')"
        return 0
    fi

    # Install wiedehopf's readsb fork
    log_info "Installing wiedehopf's readsb..."

    local tmpdir
    tmpdir=$(mktemp -d)

    cd "$tmpdir"
    git clone --depth 1 https://github.com/wiedehopf/readsb.git
    cd readsb

    make -j$(nproc) BLADERF=no

    if make install; then
        log_success "readsb installed"
    else
        log_error "readsb installation failed"
        exit 1
    fi

    cd /
    rm -rf "$tmpdir"
}

role_feeder_install_mlat_client() {
    log_step "Installing mlat-client"

    if command -v mlat-client &>/dev/null; then
        log_info "mlat-client already installed: $(mlat-client --version 2>/dev/null || echo 'unknown version')"
        return 0
    fi

    # Install wiedehopf's mlat-client
    log_info "Installing mlat-client..."

    local tmpdir
    tmpdir=$(mktemp -d)

    cd "$tmpdir"
    git clone --depth 1 https://github.com/wiedehopf/mlat-client.git
    cd mlat-client

    make -j$(nproc)

    cp mlat-client /usr/local/bin/mlat-client
    chmod +x /usr/local/bin/mlat-client

    log_success "mlat-client installed"

    cd /
    rm -rf "$tmpdir"
}

role_feeder_configure_readsb() {
    log_step "Configuring readsb"

    # Save UUID
    echo "$FEEDER_UUID" > /etc/bharat-radar-id
    chmod 644 /etc/bharat-radar-id

    # Create readsb default config
    cat > /etc/default/readsb <<EOF
# readsb configuration for BharatRadar
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

RECEIVER_OPTIONS="--device ${SDR_SERIAL} --device-type rtlsdr --gain auto --ppm 0 --dcfilter --enable-biastee --uuid-file /etc/bharat-radar-id --preamble-threshold 58"
DECODER_OPTIONS="--lat ${READSB_LAT} --lon ${READSB_LON} --max-range 450 --fix"
NET_OPTIONS="--net --net-ingest --net-receiver-id --net-ri-port 30001 --net-ro-port 30002 --net-sbs-port 30003 --net-bi-port 30004,30104,31008,30105 --net-bo-port 30005 --net-beast-reduce-interval 0.5"
JSON_OPTIONS="--json-location-accuracy 2"
ADAPTIVE_DYNAMIC_RANGE=yes
EOF

    log_success "readsb configured"
}

role_feeder_create_feeder_service() {
    log_step "Creating bharat-feeder Service"

    cat > /etc/systemd/system/bharat-feeder.service <<EOF
[Unit]
Description=Bharat Radar Beast Feeder Bridge
After=network.target readsb.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/readsb --net-only --uuid-file /etc/bharat-radar-id \\
  --net-connector 127.0.0.1,30005,beast_in \\
  --net-connector ${FEEDER_DOMAIN},30004,beast_reduce_plus_out
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable bharat-feeder

    log_success "bharat-feeder service created"
}

role_feeder_create_mlat_service() {
    log_step "Creating bharat-mlat Service"

    cat > /etc/systemd/system/bharat-mlat.service <<EOF
[Unit]
Description=Bharat Radar MLAT Client
After=network.target readsb.service bharat-feeder.service

[Service]
Type=simple
ExecStart=/usr/local/bin/mlat-client \\
  --input-type beast \\
  --input-connect 127.0.0.1:30005 \\
  --server ${FEEDER_DOMAIN}:31090 \\
  --user ${FEEDER_UUID} \\
  --lat ${READSB_LAT} \\
  --lon ${READSB_LON} \\
  --alt ${FEEDER_ALT_M} \\
  ${MLAT_PRIVACY} \\
  --results beast,connect,127.0.0.1:30105
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable bharat-mlat

    log_success "bharat-mlat service created"
}

role_feeder_start_services() {
    log_step "Starting Services"

    # Stop existing readsb if running (will be started by feeder services)
    systemctl stop readsb 2>/dev/null || true

    # Start feeder services
    systemctl restart bharat-feeder
    systemctl restart bharat-mlat

    sleep 5

    if systemctl is-active --quiet bharat-feeder; then
        log_success "bharat-feeder is running"
    else
        log_error "bharat-feeder failed to start"
        journalctl -u bharat-feeder --since "30 seconds ago" --no-pager || true
    fi

    if systemctl is-active --quiet bharat-mlat; then
        log_success "bharat-mlat is running"
    else
        log_error "bharat-mlat failed to start"
        journalctl -u bharat-mlat --since "30 seconds ago" --no-pager || true
    fi
}

role_feeder_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar Feeder Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 3.4.1

ROLE=feeder
FEEDER_DOMAIN="${FEEDER_DOMAIN}"
READSB_LAT="${READSB_LAT}"
READSB_LON="${READSB_LON}"
FEEDER_ALT_M="${FEEDER_ALT_M}"
FEEDER_UUID="${FEEDER_UUID}"
FEEDER_NAME="${FEEDER_NAME}"
SDR_SERIAL="${SDR_SERIAL}"
MLAT_PRIVACY="${MLAT_PRIVACY:-}"
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/config.env"
}

role_feeder_post_install() {
    local local_ip
    local_ip=$(hostname -I | awk '{print $1}')

    # Extract base domain from feeder domain
    local base_domain
    base_domain=$(echo "$FEEDER_DOMAIN" | sed 's/^feed\.//')

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Feeder Pi Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}Feeder UUID:${NC}  ${FEEDER_UUID}"
    echo -e "  ${CYAN}Feeder Name:${NC}  ${FEEDER_NAME}"
    echo -e "  ${CYAN}Server:${NC}       ${FEEDER_DOMAIN}"
    echo ""
    echo -e "  ${CYAN}Your Map:${NC}     https://my.${base_domain}"
    echo -e "  ${CYAN}Public Map:${NC}   https://map.${base_domain}"
    echo -e "  ${CYAN}Local readsb:${NC} http://${local_ip}:8080"
    echo ""
    echo -e "  ${CYAN}Check status:${NC}"
    echo "    systemctl status bharat-feeder"
    echo "    systemctl status bharat-mlat"
    echo "    journalctl -u bharat-feeder -f"
    echo "    journalctl -u bharat-mlat -f"
    echo ""
    echo -e "  ${CYAN}Local readsb interfaces:${NC}"
    echo "    http://${local_ip}:8080/       (tar1090 map)"
    echo "    http://${local_ip}:8080/data/  (JSON API)"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_feeder_run() {
    require_root

    local phases=(config packages readsb mlat_client configure services start save)

    show_resume_banner "${phases[@]}"

    # Load existing config if present
    if [ -f /etc/bharatradar/config.env ]; then
        source /etc/bharatradar/config.env
        FEEDER_DOMAIN="${FEEDER_DOMAIN:-}"
        READSB_LAT="${READSB_LAT:-}"
        READSB_LON="${READSB_LON:-}"
    fi

    # Phase: config
    if ! checkpoint_completed "config"; then
        role_feeder_collect_config

        save_config_value "ROLE" "feeder"
        save_config_value "FEEDER_DOMAIN" "${FEEDER_DOMAIN}"
        save_config_value "READSB_LAT" "${READSB_LAT}"
        save_config_value "READSB_LON" "${READSB_LON}"
        save_config_value "FEEDER_ALT_M" "${FEEDER_ALT_M:-10}"
        save_config_value "FEEDER_UUID" "${FEEDER_UUID}"
        save_config_value "FEEDER_NAME" "${FEEDER_NAME}"
        save_config_value "SDR_SERIAL" "${SDR_SERIAL:-0}"
        save_config_value "MLAT_PRIVACY" "${MLAT_PRIVACY:-}"

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
        role_feeder_install_packages || {
            log_error "Package installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install feeder"
            exit 1
        }
        checkpoint_mark "packages"
    fi

    # Phase: readsb
    if ! checkpoint_completed "readsb"; then
        role_feeder_install_readsb || {
            log_error "readsb installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install feeder"
            exit 1
        }
        checkpoint_mark "readsb"
    fi

    # Phase: mlat_client
    if ! checkpoint_completed "mlat_client"; then
        role_feeder_install_mlat_client || {
            log_error "mlat-client installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install feeder"
            exit 1
        }
        checkpoint_mark "mlat_client"
    fi

    # Phase: configure
    if ! checkpoint_completed "configure"; then
        role_feeder_configure_readsb || {
            log_error "readsb configuration failed. To retry, run:"
            echo "  sudo ./bharatradar-install feeder"
            exit 1
        }
        checkpoint_mark "configure"
    fi

    # Phase: services
    if ! checkpoint_completed "services"; then
        role_feeder_create_feeder_service
        role_feeder_create_mlat_service
        checkpoint_mark "services"
    fi

    # Phase: start
    if ! checkpoint_completed "start"; then
        role_feeder_start_services || {
            log_warn "Some services failed to start. Check with:"
            echo "  sudo systemctl status bharat-feeder bharat-mlat"
        }
        checkpoint_mark "start"
    fi

    # Phase: save
    if ! checkpoint_completed "save"; then
        role_feeder_save_config
        role_feeder_post_install
        checkpoint_mark "save"
    fi

    checkpoint_clear
    log_success "Feeder Pi installation complete!"
}
