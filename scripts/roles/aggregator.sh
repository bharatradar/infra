#!/bin/bash
# Role: Aggregator (K3s agent, joins existing hub)
# Installs K3s agent and joins an existing cluster.

set -euo pipefail

role_aggregator_collect_config() {
    log_step "Aggregator Configuration"

    echo ""
    echo "  This machine will join an existing BharatRadar hub as a K3s agent node."
    echo "  Pods will be scheduled here for failover and load distribution."
    echo ""

    prompt_input "Hub LAN IP address" "" HUB_IP

    while ! validate_ip "$HUB_IP"; do
        log_error "Invalid IP address"
        prompt_input "Hub LAN IP address" "$HUB_IP" HUB_IP
    done

    prompt_input "K3s join token" "" K3S_TOKEN

    while [ -z "$K3S_TOKEN" ]; do
        log_error "Token cannot be empty"
        echo ""
        log_info "Get the token from your hub with:"
        echo "  ssh user@${HUB_IP} 'sudo cat /var/lib/rancher/k3s/server/node-token'"
        echo ""
        prompt_input "K3s join token" "" K3S_TOKEN
    done

    # Ask for base domain (needed for status checks, config)
    prompt_input "Base domain (from hub)" "bharatradar.com" BASE_DOMAIN

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    echo ""
    log_step "Configuration Summary"
    echo -e "  Hub IP:    ${CYAN}${HUB_IP}${NC}"
    echo -e "  Domain:    ${CYAN}${BASE_DOMAIN}${NC}"
    echo ""

    if ! prompt_confirm "Proceed with Aggregator setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_aggregator_install_k3s_agent() {
    log_step "Installing K3s Agent"

    if command -v k3s &>/dev/null; then
        log_error "K3s is already installed on this machine."
        log_info "To join an existing cluster, the K3s agent service must not exist."
        log_info "To rejoin, run: sudo k3s-agent-uninstall.sh"
        exit 1
    fi

    export K3S_URL="https://${HUB_IP}:6443"
    export K3S_TOKEN

    log_info "Connecting to K3s server at ${HUB_IP}:6443..."
    
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
    
    INSTALL_K3S_SKIP_DOWNLOAD=true curl -sfL https://get.k3s.io | sh -

    log_info "Waiting for agent to register..."
    for i in $(seq 1 30); do
        if systemctl is-active --quiet k3s-agent 2>/dev/null; then
            log_success "K3s agent is running"
            return 0
        fi
        sleep 2
    done

    log_warn "Agent may still be registering. Check: systemctl status k3s-agent"
}

role_aggregator_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar Aggregator Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 3.4.1

ROLE=aggregator
HUB_IP=${HUB_IP}
K3S_TOKEN=${K3S_TOKEN}
BASE_DOMAIN=${BASE_DOMAIN:-}
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/config.env"
}

role_aggregator_post_install() {
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Aggregator Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}This node has joined the cluster at:${NC} ${HUB_IP}"
    echo ""
    echo -e "  ${CYAN}To verify, run on the Hub machine (not this one):${NC}"
    echo "    ssh user@${HUB_IP} 'sudo kubectl get nodes'"
    echo "    ssh user@${HUB_IP} 'sudo kubectl get pods -n bharatradar -o wide'"
    echo ""
    echo -e "  ${CYAN}Note: kubectl only works on the Hub (K3s server).${NC}"
    echo "  This node is an agent and does not serve the Kubernetes API."
    echo ""
    echo -e "  ${CYAN}Local commands:${NC}"
    echo "    sudo systemctl status k3s-agent"
    echo "    sudo journalctl -u k3s-agent -f"
    echo "    sudo bharatradar-install status"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_aggregator_run() {
    require_root

    # Load existing config if present
    if [ -f /etc/bharatradar/config.env ]; then
        source /etc/bharatradar/config.env
        HUB_IP="${HUB_IP:-}"
        K3S_TOKEN="${K3S_TOKEN:-}"
        BASE_DOMAIN="${BASE_DOMAIN:-}"
    fi

    role_aggregator_collect_config
    role_aggregator_install_k3s_agent
    role_aggregator_save_config
    role_aggregator_post_install
}
