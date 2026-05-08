#!/bin/bash
# BharatRadar Infrastructure - Nginx + Let's Encrypt Setup Script
# Version: 1.0.0
#
# This script configures nginx as a reverse proxy for FRP
# and sets up Let's Encrypt SSL certificates for all subdomains.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(dirname "$SCRIPT_DIR")"
source "${SCRIPTS_DIR}/helpers/functions.sh"

# Default configuration
BASE_DOMAIN=""
FRPS_VHOST_HTTP_PORT=8080
EMAIL=""
SUBDOMAINS=("map" "history" "mlat" "api" "feed" "ws" "grafana" "cortex")

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --domain DOMAIN   Base domain (e.g., bharatradar.com)
  --vhttp-port PORT FRP vhost HTTP port (default: 8080)
  --email EMAIL     Email for Let's Encrypt
  --help            Show this help message

Examples:
  # Interactive setup
  sudo $0

  # Automated setup
  sudo $0 --domain bharatradar.com --email admin@example.com

EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)     BASE_DOMAIN="$2"; shift 2 ;;
        --vhttp-port) FRPS_VHOST_HTTP_PORT="$2"; shift 2 ;;
        --email)      EMAIL="$2"; shift 2 ;;
        --help)       usage ;;
        *)            log_error "Unknown option: $1"; usage ;;
    esac
done

collect_config() {
    log_step "Nginx + SSL Configuration"

    if [ -z "$BASE_DOMAIN" ]; then
        prompt_input "Base domain" "bharatradar.com" BASE_DOMAIN
    fi

    if [ -z "$EMAIL" ]; then
        prompt_input "Email for Let's Encrypt" "" EMAIL
    fi

    echo ""
    log_step "Configuration"
    echo -e "  Base Domain: ${CYAN}$BASE_DOMAIN${NC}"
    echo -e "  vHTTP Port:  ${CYAN}$FRPS_VHOST_HTTP_PORT${NC}"
    echo -e "  Email:       ${CYAN}$EMAIL${NC}"
    echo ""

    if ! prompt_confirm "Proceed?"; then
        exit 0
    fi
}

install_packages() {
    log_step "Installing Packages"

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get update
            apt-get install -y nginx certbot python3-certbot-nginx
            ;;
        fedora|centos|rhel)
            dnf install -y nginx certbot python3-certbot-nginx
            ;;
        *)
            log_error "Unsupported OS: $os"
            exit 1
            ;;
    esac

    log_success "Packages installed"
}

configure_nginx() {
    log_step "Configuring Nginx"

    local all_domains="${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        all_domains="${all_domains} ${sub}.${BASE_DOMAIN}"
    done

    # HTTP server block - ACME challenge + redirect
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

    # Create SSL config file (will be populated by certbot)
    cat > /etc/nginx/sites-available/bharatradar-ssl <<EOF
# SSL server blocks - populated by certbot
# This file will be modified by certbot during certificate issuance
EOF

    # Enable sites
    rm -f /etc/nginx/sites-enabled/default
    rm -f /etc/nginx/sites-enabled/bharatradar-http
        rm -f /etc/nginx/sites-enabled/bharatradar-ssl

        ln -sf /etc/nginx/sites-available/bharatradar-http /etc/nginx/sites-enabled/

    # Test and reload
    nginx -t
    systemctl reload nginx

    log_success "Nginx configured (HTTP only - HTTPS will be added by certbot)"
}

setup_ssl() {
    log_step "Setting up SSL with Let's Encrypt"

    local all_domains="-d ${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        all_domains="${all_domains} -d ${sub}.${BASE_DOMAIN}"
    done

    log_info "Requesting SSL certificate for all domains..."
    certbot --nginx ${all_domains} \
        --non-interactive --agree-tos --email "${EMAIL}" --redirect

    # Setup auto-renewal
    systemctl enable certbot.timer
    systemctl start certbot.timer

    log_success "SSL certificate configured"

    # Reload nginx after certbot modifies configs
    systemctl reload nginx
}

verify_ssl() {
    log_step "Verifying SSL Setup"

    for domain in ${BASE_DOMAIN} $(for sub in "${SUBDOMAINS[@]}"; do echo "${sub}.${BASE_DOMAIN}"; done); do
        if curl -sI "https://${domain}" 2>/dev/null | head -1 | grep -q "200\|301\|302"; then
            log_success "${domain}: OK"
        else
            log_warn "${domain}: Not responding (DNS may not be configured yet)"
        fi
    done
}

post_install_info() {
    echo ""
    log_step "Nginx + SSL Setup Complete!"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}Domains:${NC}"
    for domain in ${BASE_DOMAIN} $(for sub in "${SUBDOMAINS[@]}"; do echo "${sub}.${BASE_DOMAIN}"; done); do
        echo "  - https://${domain}"
    done
    echo ""
    echo -e "  ${CYAN}Useful Commands:${NC}"
    echo "  sudo nginx -t                           # Test nginx config"
    echo "  sudo systemctl reload nginx              # Reload nginx"
    echo "  sudo certbot certificates                # View certificates"
    echo "  sudo certbot renew --dry-run             # Test renewal"
    echo "  sudo nginx -T | grep -A5 server_name     # View server blocks"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

main() {
    require_root
    collect_config
    install_packages
    configure_nginx
    setup_ssl
    verify_ssl
    post_install_info
}

main "$@"
