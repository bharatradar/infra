#!/bin/bash
# BharatRadar Infrastructure - FRP Client (frpc) Setup Script
# Version: 1.0.0
#
# This script sets up the FRP client on Raspberry Pi that will:
# 1. Download and install frpc
# 2. Generate frpc.toml configuration
# 3. Create systemd service
# 4. Forward web UI and ADS-B feeds to the FRP server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(dirname "$SCRIPT_DIR")"
source "${SCRIPTS_DIR}/helpers/functions.sh"

# Default configuration
FRP_SERVER_IP=""
FRP_AUTH_TOKEN=""
BASE_DOMAIN=""
FRP_VERSION="0.68.1"

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --server IP       FRP server public IP
  --token TOKEN     FRP authentication token
  --domain DOMAIN   Base domain (e.g., bharatradar.com)
  --version VER     FRP version (default: 0.68.1)
  --help            Show this help message

Examples:
  # Interactive setup
  sudo $0

  # Automated setup
  sudo $0 --server 13.48.249.103 --token "your-token" --domain bharatradar.com

EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --server)  FRP_SERVER_IP="$2"; shift 2 ;;
        --token)   FRP_AUTH_TOKEN="$2"; shift 2 ;;
        --domain)  BASE_DOMAIN="$2"; shift 2 ;;
        --version) FRP_VERSION="$2"; shift 2 ;;
        --help)    usage ;;
        *)         log_error "Unknown option: $1"; usage ;;
    esac
done

collect_config() {
    log_step "FRP Client Configuration"

    if [ -z "$FRP_SERVER_IP" ]; then
        prompt_input "FRP server public IP (AWS EC2)" "" FRP_SERVER_IP
    fi
    while ! validate_ip "$FRP_SERVER_IP"; do
        log_error "Invalid IP address"
        prompt_input "FRP server public IP" "$FRP_SERVER_IP" FRP_SERVER_IP
    done

    if [ -z "$FRP_AUTH_TOKEN" ]; then
        prompt_input "FRP authentication token" "" FRP_AUTH_TOKEN
    fi

    if [ -z "$BASE_DOMAIN" ]; then
        prompt_input "Base domain" "bharatradar.com" BASE_DOMAIN
    fi

    echo ""
    log_step "Configuration Summary"
    echo -e "  FRP Server:  ${CYAN}$FRP_SERVER_IP${NC}"
    echo -e "  Domain:      ${CYAN}$BASE_DOMAIN${NC}"
    echo ""

    if ! prompt_confirm "Proceed with FRP client setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

install_frpc() {
    log_step "Installing FRP Client"

    local arch
    arch=$(detect_arch)

    if command -v frpc &>/dev/null && [ "$(frpc --version 2>/dev/null | head -1)" = "0.68.1" ]; then
        log_info "frpc v${FRP_VERSION} already installed, skipping..."
        return 0
    fi

    log_info "Downloading frpc v${FRP_VERSION}..."
    local frp_url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"
    wget -q "$frp_url" -O /tmp/frp.tar.gz
    tar xzf /tmp/frp.tar.gz -C /tmp
    cp "/tmp/frp_${FRP_VERSION}_linux_${arch}/frpc" /usr/local/bin/frpc
    chmod +x /usr/local/bin/frpc
    rm -rf /tmp/frp_*

    log_success "frpc installed"
}

configure_frpc() {
    log_step "Configuring FRP Client"

    cat > /etc/frpc.toml <<EOF
serverAddr = "${FRP_SERVER_IP}"
serverPort = 7000

auth.method = "token"
auth.token = "${FRP_AUTH_TOKEN}"

# Route all web UI traffic to K3s Traefik Ingress
[[proxies]]
name = "web-ui"
type = "http"
localIP = "127.0.0.1"
localPort = 80
customDomains = [
    "${BASE_DOMAIN}",
    "map.${BASE_DOMAIN}",
    "history.${BASE_DOMAIN}",
    "mlat.${BASE_DOMAIN}",
    "api.${BASE_DOMAIN}",
    "feed.${BASE_DOMAIN}",
    "ws.${BASE_DOMAIN}",
    "cortex.${BASE_DOMAIN}",
    "grafana.${BASE_DOMAIN}"
]

# Forward Beast Protocol (Raw ADS-B)
[[proxies]]
name = "feed-beast-30005"
type = "tcp"
localIP = "127.0.0.1"
localPort = 30005
remotePort = 30005

# Forward Beast Protocol (Alternative)
[[proxies]]
name = "feed-beast-30004"
type = "tcp"
localIP = "127.0.0.1"
localPort = 30004
remotePort = 30004

# Forward MLAT Protocol
[[proxies]]
name = "feed-mlat-31090"
type = "tcp"
localIP = "127.0.0.1"
localPort = 31090
remotePort = 31090
EOF

    # Create systemd service
    cat > /etc/systemd/system/frpc.service <<EOF
[Unit]
Description=FRP Client (BharatRadar)
After=network.target
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
    systemctl start frpc

    log_success "FRP client configured and started"
}

verify_connection() {
    log_step "Verifying FRP Connection"

    sleep 5

    if systemctl is-active --quiet frpc; then
        log_success "FRP client is running"

        # Check journal for connection status
        if journalctl -u frpc --since "1 minute ago" --no-pager 2>/dev/null | grep -q "login to server success"; then
            log_success "Successfully connected to FRP server at ${FRP_SERVER_IP}"
        else
            log_warn "Connection status unknown. Check logs: journalctl -u frpc -f"
        fi
    else
        log_error "FRP client failed to start"
        journalctl -u frpc --since "1 minute ago" --no-pager || true
        exit 1
    fi
}

post_install_info() {
    echo ""
    log_step "FRP Client Setup Complete!"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}FRP Client Status:${NC}"
    echo "  - Server:       ${FRP_SERVER_IP}"
    echo "  - Domain:       ${BASE_DOMAIN}"
    echo "  - Web UI:       https://map.${BASE_DOMAIN}"
    echo "  - MLAT Map:     https://mlat.${BASE_DOMAIN}"
    echo ""
    echo -e "  ${CYAN}Useful Commands:${NC}"
    echo "  sudo systemctl status frpc       # Check FRP client"
    echo "  sudo systemctl restart frpc       # Restart FRP client"
    echo "  journalctl -u frpc -f             # View FRP logs"
    echo "  cat /etc/frpc.toml                # View configuration"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

main() {
    require_root
    collect_config
    install_frpc
    configure_frpc
    verify_connection
    post_install_info
}

main "$@"
