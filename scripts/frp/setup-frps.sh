#!/bin/bash
# BharatRadar Infrastructure - FRP Server (frps) Setup Script
# Version: 1.0.0
#
# This script sets up the FRP server on AWS/remote machine that will:
# 1. Run frps daemon
# 2. Configure nginx reverse proxy
# 3. Set up Let's Encrypt SSL certificates
# 4. Create systemd services for all components

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(dirname "$SCRIPT_DIR")"
source "${SCRIPTS_DIR}/helpers/functions.sh"

# Default configuration
BASE_DOMAIN=""
FRP_AUTH_TOKEN=""
FRPS_BIND_PORT=7000
FRPS_VHOST_HTTP_PORT=8080
FRPS_DASHBOARD_PORT=7500
FRPS_DASHBOARD_USER="admin"
FRPS_DASHBOARD_PASS=""
EMAIL=""
CERTBOT_METHOD="standalone"

# Subdomains
SUBDOMAINS=("map" "history" "mlat" "api" "feed" "ws")

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --domain DOMAIN           Base domain (e.g., bharat-radar.vellur.in)
  --frp-token TOKEN         FRP authentication token
  --bind-port PORT          FRP bind port (default: 7000)
  --vhttp-port PORT         vhost HTTP port (default: 8080)
  --dashboard-port PORT     Dashboard port (default: 7500)
  --dashboard-user USER     Dashboard username (default: admin)
  --dashboard-pass PASS     Dashboard password (auto-generated if empty)
  --email EMAIL             Email for Let's Encrypt
  --dns-provider PROVIDER   Cloudflare DNS challenge (cloudflare, route53, etc.)
  --help                    Show this help message

Examples:
  # Interactive setup
  sudo $0

  # Automated setup
  sudo $0 --domain bharat-radar.vellur.in --frp-token "your-token" --email admin@example.com

EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)         BASE_DOMAIN="$2"; shift 2 ;;
        --frp-token)      FRP_AUTH_TOKEN="$2"; shift 2 ;;
        --bind-port)      FRPS_BIND_PORT="$2"; shift 2 ;;
        --vhttp-port)     FRPS_VHOST_HTTP_PORT="$2"; shift 2 ;;
        --dashboard-port) FRPS_DASHBOARD_PORT="$2"; shift 2 ;;
        --dashboard-user) FRPS_DASHBOARD_USER="$2"; shift 2 ;;
        --dashboard-pass) FRPS_DASHBOARD_PASS="$2"; shift 2 ;;
        --email)          EMAIL="$2"; shift 2 ;;
        --dns-provider)   CERTBOT_METHOD="$1"; DNS_PROVIDER="$2"; shift 2 ;;
        --help)           usage ;;
        *)                log_error "Unknown option: $1"; usage ;;
    esac
done

collect_config() {
    log_step "FRP Server Configuration"

    if [ -z "$BASE_DOMAIN" ]; then
        prompt_input "Enter your base domain" "bharat-radar.vellur.in" BASE_DOMAIN
    fi

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Enter your base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    if [ -z "$FRP_AUTH_TOKEN" ]; then
        prompt_input "FRP authentication token" "$(generate_token)" FRP_AUTH_TOKEN
    fi

    if [ -z "$FRPS_DASHBOARD_PASS" ]; then
        prompt_input "FRP dashboard password" "$(generate_secret)" FRPS_DASHBOARD_PASS
    fi

    if [ -z "$EMAIL" ]; then
        prompt_input "Email for Let's Encrypt" "" EMAIL
    fi

    echo ""
    log_step "Configuration Summary"
    echo -e "  Base Domain:    ${CYAN}$BASE_DOMAIN${NC}"
    echo -e "  FRP Bind Port:  ${CYAN}$FRPS_BIND_PORT${NC}"
    echo -e "  vHTTP Port:     ${CYAN}$FRPS_VHOST_HTTP_PORT${NC}"
    echo -e "  Dashboard:      ${CYAN}port $FRPS_DASHBOARD_PORT, user $FRPS_DASHBOARD_USER${NC}"
    echo -e "  Email:          ${CYAN}$EMAIL${NC}"
    echo ""

    if ! prompt_confirm "Proceed with FRP server setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

install_frps() {
    log_step "Installing FRP Server"

    local arch
    arch=$(detect_arch)
    local FRP_VERSION="0.68.1"

    if command -v frps &>/dev/null; then
        log_info "frps already installed, skipping..."
        return 0
    fi

    log_info "Downloading frps v${FRP_VERSION}..."
    local frp_url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"
    wget -q "$frp_url" -O /tmp/frp.tar.gz
    tar xzf /tmp/frp.tar.gz -C /tmp
    cp "/tmp/frp_${FRP_VERSION}_linux_${arch}/frps" /usr/local/bin/frps
    chmod +x /usr/local/bin/frps
    rm -rf /tmp/frp_*

    log_success "frps installed"
}

configure_frps() {
    log_step "Configuring FRP Server"

    cat > /etc/frps.toml <<EOF
bindPort = ${FRPS_BIND_PORT}
vhostHTTPPort = ${FRPS_VHOST_HTTP_PORT}

auth.method = "token"
auth.token = "${FRP_AUTH_TOKEN}"

webServer.addr = "0.0.0.0"
webServer.port = ${FRPS_DASHBOARD_PORT}
webServer.user = "${FRPS_DASHBOARD_USER}"
webServer.password = "${FRPS_DASHBOARD_PASS}"

allowPorts = [
  { single = 30004 },
  { single = 30005 },
  { single = 30104 },
  { single = 30105 },
  { single = 31090 },
  { single = ${FRPS_VHOST_HTTP_PORT} }
]
EOF

    # Create systemd service
    cat > /etc/systemd/system/frps.service <<EOF
[Unit]
Description=FRP Server (BharatRadar)
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frps.toml
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
    systemctl enable frps
    systemctl start frps

    log_success "FRP server configured and started"
    log_info "Dashboard: http://$(curl -s ifconfig.me):${FRPS_DASHBOARD_PORT}"
    log_info "Username: ${FRPS_DASHBOARD_USER}"
    log_info "Password: ${FRPS_DASHBOARD_PASS}"
}

install_nginx() {
    log_step "Installing Nginx"

    local os
    os=$(detect_os)

    if command -v nginx &>/dev/null; then
        log_info "nginx already installed, skipping..."
        return 0
    fi

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get install -y nginx
            ;;
        fedora|centos|rhel)
            dnf install -y nginx
            ;;
        alpine)
            apk add nginx
            ;;
        *)
            log_error "Unsupported OS: $os"
            exit 1
            ;;
    esac

    log_success "nginx installed"
}

configure_nginx() {
    log_step "Configuring Nginx"

    local all_domains="${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        all_domains="${all_domains} ${sub}.${BASE_DOMAIN}"
    done

    # HTTP server - ACME challenge + redirect to HTTPS
    cat > /etc/nginx/sites-available/bharatradar-http <<EOF
server {
    listen 80;
    server_name ${all_domains};

    location ^~ /.well-known/ {
        root /var/www/html;
        allow all;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}
EOF

    # HTTPS server - proxy all traffic to FRP vhost HTTP port
    local server_blocks=""
    for domain in $all_domains; do
        server_blocks+="
server {
    listen 443 ssl http2;
    server_name ${domain};

    ssl_certificate /etc/letsencrypt/live/${BASE_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${BASE_DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:${FRPS_VHOST_HTTP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
"
    done

    # WebSocket support for ws subdomain
    server_blocks+="
server {
    listen 443 ssl http2;
    server_name ws.${BASE_DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${BASE_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${BASE_DOMAIN}/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:${FRPS_VHOST_HTTP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }
}
"

    cat > /etc/nginx/sites-available/bharatradar-ssl <<EOF
${server_blocks}
EOF

    # Enable sites
    rm -f /etc/nginx/sites-enabled/default
    ln -sf /etc/nginx/sites-available/bharatradar-http /etc/nginx/sites-enabled/
    ln -sf /etc/nginx/sites-available/bharatradar-ssl /etc/nginx/sites-enabled/

    # Test and reload
    nginx -t
    systemctl reload nginx

    log_success "nginx configured"
}

setup_certbot() {
    log_step "Setting up Let's Encrypt SSL"

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get install -y certbot python3-certbot-nginx
            ;;
        fedora|centos|rhel)
            dnf install -y certbot python3-certbot-nginx
            ;;
        *)
            log_error "Unsupported OS for certbot"
            exit 1
            ;;
    esac

    # Ensure nginx is running for HTTP-01 challenge
    systemctl restart nginx

    local all_domains="${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        all_domains="${all_domains} -d ${sub}.${BASE_DOMAIN}"
    done

    log_info "Requesting SSL certificate for: $all_domains"
    certbot --nginx -d ${BASE_DOMAIN} $(for sub in "${SUBDOMAINS[@]}"; do echo "-d ${sub}.${BASE_DOMAIN}"; done) \
        --non-interactive --agree-tos --email "${EMAIL}" --redirect

    # Setup auto-renewal
    systemctl enable certbot.timer
    systemctl start certbot.timer

    log_success "SSL certificate configured"
    log_info "Certificate will auto-renew via certbot timer"
}

save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    cat > /etc/bharatradar/frps-config.env <<EOF
# FRP Server Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Version: 1.0.0

BASE_DOMAIN=${BASE_DOMAIN}
FRP_AUTH_TOKEN=${FRP_AUTH_TOKEN}
FRPS_BIND_PORT=${FRPS_BIND_PORT}
FRPS_VHOST_HTTP_PORT=${FRPS_VHOST_HTTP_PORT}
FRPS_DASHBOARD_PORT=${FRPS_DASHBOARD_PORT}
FRPS_DASHBOARD_USER=${FRPS_DASHBOARD_USER}
FRPS_DASHBOARD_PASS=${FRPS_DASHBOARD_PASS}
EMAIL=${EMAIL}

# Subdomains
SUBDOMAINS="${SUBDOMAINS[*]}"

# FRP Client Token (share this with frpc machines)
FRP_TOKEN=${FRP_AUTH_TOKEN}
FRP_SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")
EOF

    chmod 600 /etc/bharatradar/frps-config.env
    log_success "Configuration saved to /etc/bharatradar/frps-config.env"
}

post_install_info() {
    echo ""
    log_step "FRP Server Setup Complete!"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}FRP Server:${NC}"
    echo "  - Bind Port:      ${FRPS_BIND_PORT}"
    echo "  - vHTTP Port:     ${FRPS_VHOST_HTTP_PORT}"
    echo "  - Dashboard:      http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP'):${FRPS_DASHBOARD_PORT}"
    echo "  - User:           ${FRPS_DASHBOARD_USER}"
    echo "  - Password:       ${FRPS_DASHBOARD_PASS}"
    echo ""
    echo -e "  ${CYAN}SSL Certificate:${NC}"
    echo "  - Domains: ${BASE_DOMAIN} $(for sub in "${SUBDOMAINS[@]}"; do echo "${sub}.${BASE_DOMAIN}"; done)"
    echo "  - Auto-renewal: enabled (certbot timer)"
    echo ""
    echo -e "  ${CYAN}On Raspberry Pi (frpc):${NC}"
    echo "  Run: sudo scripts/frp/setup-frpc.sh --server $(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP') --token ${FRP_AUTH_TOKEN} --domain ${BASE_DOMAIN}"
    echo ""
    echo -e "  ${CYAN}Useful Commands:${NC}"
    echo "  sudo systemctl status frps           # Check FRP server"
    echo "  sudo systemctl status nginx           # Check nginx"
    echo "  sudo certbot certificates             # View certificates"
    echo "  sudo nginx -t                          # Test nginx config"
    echo ""
    echo -e "  ${CYAN}Configuration saved to: /etc/bharatradar/frps-config.env${NC}"
    echo -e "${GREEN}================================================================${NC}"
}

main() {
    require_root
    collect_config
    install_frps
    configure_frps
    install_nginx
    configure_nginx
    setup_certbot
    save_config
    post_install_info
}

main "$@"
