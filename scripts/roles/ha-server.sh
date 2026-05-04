#!/bin/bash
# Role: HA Hub (Second K3s server, joins existing cluster)
# Installs K3s server with shared PostgreSQL datastore and optional keepalived.

set -euo pipefail

role_ha_server_collect_config() {
    log_step "HA Hub Configuration"

    echo ""
    echo "  This machine will be a second K3s server in your cluster."
    echo "  It shares the same PostgreSQL datastore as the Primary Hub."
    echo "  If the Primary Hub fails, this server keeps the cluster running."
    echo ""

    # PostgreSQL server details
    echo ""
    log_info "Enter your PostgreSQL server details:"
    collect_pg_config "DB"
    log_info "Using PostgreSQL: ${DB_HOST}:${DB_PORT}/${DB_DBNAME}"

    # Cluster token from primary
    prompt_input "K3s cluster token (from Primary Hub)" "" K3S_CLUSTER_TOKEN

    while [ -z "$K3S_CLUSTER_TOKEN" ]; do
        log_error "Token cannot be empty"
        echo ""
        log_info "Get the token from your Primary Hub with:"
        echo "  ssh user@<hub-ip> 'sudo cat /var/lib/rancher/k3s/server/node-token'"
        echo ""
        prompt_input "K3s cluster token" "" K3S_CLUSTER_TOKEN
    done

    # Primary Hub IP (for keepalived awareness)
    prompt_input "Primary Hub IP" "" PRIMARY_HUB_IP

    while ! validate_ip "$PRIMARY_HUB_IP"; do
        log_error "Invalid IP address"
        prompt_input "Primary Hub IP" "$PRIMARY_HUB_IP" PRIMARY_HUB_IP
    done

    # Domain
    prompt_input "Base domain (from Primary Hub)" "bharatradar.com" BASE_DOMAIN

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    # Keepalived
    echo ""
    if prompt_confirm "Set up Keepalived VIP for automatic failover?"; then
        KEEPALIVED_ENABLED=true
        # Source keepalived config
        source "${SCRIPT_DIR}/roles/keepalived.sh"
        keepalived_collect_config
    else
        KEEPALIVED_ENABLED=false
        KEEPALIVED_VIP=""
    fi

    echo ""
    log_step "Configuration Summary"
    echo -e "  Role:        ${CYAN}HA Server (K3s)${NC}"
    echo -e "  Primary Hub: ${CYAN}${PRIMARY_HUB_IP}${NC}"
    echo -e "  Domain:      ${CYAN}${BASE_DOMAIN}${NC}"
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        echo -e "  VIP:         ${CYAN}${KEEPALIVED_VIP}${NC}"
    fi
    echo ""

    if ! prompt_confirm "Proceed with HA Hub setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_ha_server_install_k3s() {
    log_step "Installing K3s Server (HA Mode)"

    if command -v k3s &>/dev/null && kubectl cluster-info &>/dev/null 2>&1; then
        log_info "K3s already installed and running"
        return 0
    fi

    export K3S_TOKEN="$K3S_CLUSTER_TOKEN"
    export K3S_DATASTORE_ENDPOINT="$DB_CONNECTION_STRING"

    log_info "Installing K3s with external datastore..."
    
    # Pre-download k3s binary to avoid GitHub redirect issues
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
            log_warn "Failed to download from GitHub"
            if ! curl -fsS -o /usr/local/bin/k3s "${k3s_url}"; then
                log_error "K3s binary download failed"
                exit 1
            fi
        fi
        chmod +x /usr/local/bin/k3s
        log_success "K3s binary pre-downloaded"
    fi
    
    INSTALL_K3S_SKIP_DOWNLOAD=true curl -sfL https://get.k3s.io | sh -s - server

    log_info "Waiting for K3s to join cluster..."
    for i in $(seq 1 60); do
        if kubectl cluster-info &>/dev/null 2>&1; then
            log_success "K3s server joined cluster"
            return 0
        fi
        sleep 2
    done

    log_error "K3s failed to join cluster"
    log_info "Check logs: journalctl -u k3s -f"
    exit 1
}

role_ha_server_setup_keepalived() {
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        source "${SCRIPT_DIR}/roles/keepalived.sh"
        keepalived_install
        keepalived_configure
    fi
}

role_ha_server_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar HA Hub Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 3.4.1

ROLE=ha-server
BASE_DOMAIN="${BASE_DOMAIN}"
DB_HOST="${DB_HOST}"
DB_PORT="${DB_PORT}"
DB_DBNAME="${DB_DBNAME}"
DB_DBUSER="${DB_DBUSER}"
DB_CONNECTION_STRING="${DB_CONNECTION_STRING}"
K3S_CLUSTER_TOKEN="${K3S_CLUSTER_TOKEN}"
PRIMARY_HUB_IP="${PRIMARY_HUB_IP}"
KEEPALIVED_ENABLED="${KEEPALIVED_ENABLED}"
KEEPALIVED_VIP="${KEEPALIVED_VIP:-}"
KEEPALIVED_PRIORITY="${KEEPALIVED_PRIORITY:-90}"
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/config.env"
}

role_ha_server_post_install() {
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        HA Hub Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}This server has joined the cluster.${NC}"
    echo -e "  ${CYAN}Primary Hub:${NC} ${PRIMARY_HUB_IP}"
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        echo -e "  ${CYAN}VIP:${NC} ${KEEPALIVED_VIP} (this server is BACKUP)"
    fi
    echo ""
    echo -e "  ${CYAN}To verify from the Primary Hub:${NC}"
    echo "    kubectl get nodes"
    echo "    kubectl get pods -n bharatradar -o wide"
    echo ""
    echo -e "  ${CYAN}Useful commands:${NC}"
    echo "    sudo systemctl status k3s"
    echo "    sudo journalctl -u k3s -f"
    if [ "$KEEPALIVED_ENABLED" = true ]; then
        echo "    sudo systemctl status keepalived"
        echo "    ip addr show | grep ${KEEPALIVED_VIP:-VIP}"
    fi
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_ha_server_run() {
    require_root

    local phases=(config k3s kubectl cli keepalived save)

    show_resume_banner "${phases[@]}"

    # Load existing config if present
    if [ -f /etc/bharatradar/config.env ]; then
        source /etc/bharatradar/config.env
        BASE_DOMAIN="${BASE_DOMAIN:-}"
        DB_CONNECTION_STRING="${DB_CONNECTION_STRING:-}"
        DB_HOST="${DB_HOST:-}"
        DB_PORT="${DB_PORT:-5432}"
        DB_DBNAME="${DB_DBNAME:-k3s}"
        DB_DBUSER="${DB_DBUSER:-k3s}"
        K3S_CLUSTER_TOKEN="${K3S_CLUSTER_TOKEN:-}"
        PRIMARY_HUB_IP="${PRIMARY_HUB_IP:-}"
    fi

    # Phase: config
    if ! checkpoint_completed "config"; then
        # Skip interactive prompts if all required vars are already set (silent mode)
        if [ -n "${DB_CONNECTION_STRING:-}" ] && [ -n "${K3S_CLUSTER_TOKEN:-}" ] && [ -n "${PRIMARY_HUB_IP:-}" ] && [ -n "${BASE_DOMAIN:-}" ]; then
            log_info "Using provided configuration (silent mode)"
            # Ensure defaults for optional vars
            KEEPALIVED_ENABLED="${KEEPALIVED_ENABLED:-false}"
            KEEPALIVED_STATE="${KEEPALIVED_STATE:-BACKUP}"
            KEEPALIVED_PRIORITY="${KEEPALIVED_PRIORITY:-90}"
            KEEPALIVED_VIP="${KEEPALIVED_VIP:-}"
            DB_HOST="${DB_HOST:-}"
            DB_PORT="${DB_PORT:-5432}"
            DB_DBNAME="${DB_DBNAME:-k3s}"
            DB_DBUSER="${DB_DBUSER:-k3s}"
        else
            role_ha_server_collect_config
        fi

        save_config_value "ROLE" "ha-server"
        save_config_value "BASE_DOMAIN" "${BASE_DOMAIN}"
        save_config_value "DB_HOST" "${DB_HOST:-}"
        save_config_value "DB_PORT" "${DB_PORT:-5432}"
        save_config_value "DB_DBNAME" "${DB_DBNAME:-k3s}"
        save_config_value "DB_DBUSER" "${DB_DBUSER:-k3s}"
        save_config_value "DB_DBPASS" "${DB_DBPASS:-}"
        save_config_value "DB_CONNECTION_STRING" "${DB_CONNECTION_STRING:-}"
        save_config_value "K3S_CLUSTER_TOKEN" "${K3S_CLUSTER_TOKEN}"
        save_config_value "PRIMARY_HUB_IP" "${PRIMARY_HUB_IP}"
        save_config_value "KEEPALIVED_ENABLED" "${KEEPALIVED_ENABLED:-false}"
        save_config_value "KEEPALIVED_VIP" "${KEEPALIVED_VIP:-}"
        save_config_value "KEEPALIVED_STATE" "${KEEPALIVED_STATE:-BACKUP}"

        checkpoint_mark "config"
    else
        load_partial_config || {
            log_error "Saved config not found. To restart from scratch, run:"
            echo "  sudo rm /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial"
            exit 1
        }
    fi

    # Phase: k3s
    if ! checkpoint_completed "k3s"; then
        role_ha_server_install_k3s || {
            log_error "K3s installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install ha-server"
            exit 1
        }
        checkpoint_mark "k3s"
    fi

    # Phase: kubectl
    if ! checkpoint_completed "kubectl"; then
        setup_kubectl
        checkpoint_mark "kubectl"
    fi

    # Phase: cli
    if ! checkpoint_completed "cli"; then
        install_bharatradar_cli
        checkpoint_mark "cli"
    fi

    # Phase: keepalived
    if ! checkpoint_completed "keepalived"; then
        role_ha_server_setup_keepalived || log_warn "Keepalived setup skipped/failed"
        checkpoint_mark "keepalived"
    fi

    # Phase: save
    if ! checkpoint_completed "save"; then
        role_ha_server_save_config
        role_ha_server_post_install
        checkpoint_mark "save"
    fi

    checkpoint_clear
    log_success "HA Server installation complete!"
}
