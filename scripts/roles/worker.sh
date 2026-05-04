#!/bin/bash
# Role: Worker Node (K3s agent, joins existing cluster)
# Simple agent node that runs pods but has no control plane or DB.

set -euo pipefail

role_worker_collect_config() {
    log_step "Worker Node Configuration"

    echo ""
    echo "  This machine will join your cluster as a K3s agent (worker)."
    echo "  It runs pods scheduled by the K3s servers but does not"
    echo "  participate in the control plane or datastore."
    echo ""

    prompt_input "Primary Hub IP" "" HUB_IP

    while ! validate_ip "$HUB_IP"; do
        log_error "Invalid IP address"
        prompt_input "Primary Hub IP" "$HUB_IP" HUB_IP
    done

    prompt_input "K3s join token" "" K3S_TOKEN

    while [ -z "$K3S_TOKEN" ]; do
        log_error "Token cannot be empty"
        echo ""
        log_info "Get the token from your Hub with:"
        echo "  ssh user@${HUB_IP} 'sudo cat /var/lib/rancher/k3s/server/node-token'"
        echo ""
        prompt_input "K3s join token" "" K3S_TOKEN
    done

    prompt_input "Base domain" "bharatradar.com" BASE_DOMAIN

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    echo ""
    log_step "Configuration Summary"
    echo -e "  Hub IP:    ${CYAN}${HUB_IP}${NC}"
    echo -e "  Domain:    ${CYAN}${BASE_DOMAIN}${NC}"
    echo ""

    if ! prompt_confirm "Proceed with Worker Node setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_worker_install_k3s_agent() {
    log_step "Installing K3s Agent"

    if command -v k3s &>/dev/null; then
        log_error "K3s is already installed on this machine."
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
            log_success "K3s agent is running and registered"
            return 0
        fi
        sleep 2
    done

    log_warn "Agent may still be registering. Check: systemctl status k3s-agent"
}

role_worker_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/config.env <<EOF
# BharatRadar Worker Node Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 3.4.1

ROLE=worker
HUB_IP="${HUB_IP}"
K3S_TOKEN="${K3S_TOKEN}"
BASE_DOMAIN="${BASE_DOMAIN}"
EOF

    chmod 600 /etc/bharatradar/config.env
    log_success "Configuration saved to /etc/bharatradar/config.env"
}

role_worker_post_install() {
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Worker Node Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}This node has joined the cluster at:${NC} ${HUB_IP}"
    echo ""
    echo -e "  ${CYAN}To verify, run on the Hub:${NC}"
    echo "    ssh user@${HUB_IP} 'sudo kubectl get nodes'"
    echo ""
    echo -e "  ${CYAN}Note: kubectl only works on K3s server nodes.${NC}"
    echo "  This node is an agent and does not serve the Kubernetes API."
    echo ""
    echo -e "  ${CYAN}Local commands:${NC}"
    echo "    sudo systemctl status k3s-agent"
    echo "    sudo journalctl -u k3s-agent -f"
    echo "    sudo bharatradar-install status"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_worker_run() {
    require_root

    local phases=(config k3s cli save)

    show_resume_banner "${phases[@]}"

    if [ -f /etc/bharatradar/config.env ]; then
        source /etc/bharatradar/config.env
        HUB_IP="${HUB_IP:-}"
        K3S_TOKEN="${K3S_TOKEN:-}"
        BASE_DOMAIN="${BASE_DOMAIN:-}"
    fi

    # Phase: config
    if ! checkpoint_completed "config"; then
        role_worker_collect_config

        save_config_value "ROLE" "worker"
        save_config_value "HUB_IP" "${HUB_IP}"
        save_config_value "K3S_TOKEN" "${K3S_TOKEN}"
        save_config_value "BASE_DOMAIN" "${BASE_DOMAIN}"

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
        role_worker_install_k3s_agent || {
            log_error "K3s agent installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install worker"
            exit 1
        }
        checkpoint_mark "k3s"
    fi

    # Phase: cli
    if ! checkpoint_completed "cli"; then
        install_bharatradar_cli
        checkpoint_mark "cli"
    fi

    # Phase: save
    if ! checkpoint_completed "save"; then
        role_worker_save_config
        role_worker_post_install
        checkpoint_mark "save"
    fi

    checkpoint_clear
    log_success "Worker node installation complete!"
}
