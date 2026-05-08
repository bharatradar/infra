#!/bin/bash
# Role: FRP Server (Cloud/VPS)
# Sets up frps, nginx, and Let's Encrypt SSL on a public-facing server.

set -euo pipefail

SUBDOMAINS=("map" "mlat" "history" "api" "my" "feed" "ws" "grafana" "cortex")
FRP_VERSION="0.68.1"
FRPS_BIND_PORT=7000
FRPS_VHOST_HTTP_PORT=8080
FRPS_DASHBOARD_PORT=7500

role_frps_collect_config() {
    log_step "FRP Server Configuration"

    prompt_input "Base domain" "bharatradar.com" BASE_DOMAIN

    while ! validate_domain "$BASE_DOMAIN"; do
        log_error "Invalid domain format"
        prompt_input "Base domain" "$BASE_DOMAIN" BASE_DOMAIN
    done

    prompt_input "Email for Let's Encrypt" "" EMAIL

    while [ -z "$EMAIL" ]; do
        log_error "Email cannot be empty"
        prompt_input "Email for Let's Encrypt" "" EMAIL
    done

    # Auto-generate tokens
    FRP_AUTH_TOKEN=$(generate_token)
    FRPS_DASHBOARD_PASS=$(generate_secret)

    echo ""
    log_step "Configuration Summary"
    echo -e "  Base Domain:    ${CYAN}${BASE_DOMAIN}${NC}"
    echo -e "  FRP Bind Port:  ${CYAN}${FRPS_BIND_PORT}${NC}"
    echo -e "  vHTTP Port:     ${CYAN}${FRPS_VHOST_HTTP_PORT}${NC}"
    echo -e "  Dashboard Port: ${CYAN}${FRPS_DASHBOARD_PORT}${NC}"
    echo -e "  Email:          ${CYAN}${EMAIL}${NC}"
    echo ""
    echo -e "  ${YELLOW}FRP Token (save this!):     ${FRP_AUTH_TOKEN}${NC}"
    echo -e "  ${YELLOW}Dashboard Password (save!): ${FRPS_DASHBOARD_PASS}${NC}"
    echo ""

    if ! prompt_confirm "Proceed with FRP server setup?"; then
        log_info "Installation cancelled."
        exit 0
    fi
}

role_frps_install_packages() {
    log_step "Installing Packages"

    local os
    os=$(detect_os)

    case "$os" in
        debian|ubuntu|raspbian)
            apt-get update -qq
            apt-get install -y -qq wget nginx certbot python3-certbot-nginx
            ;;
        fedora|centos|rhel)
            dnf install -y wget nginx certbot python3-certbot-nginx
            ;;
        *)
            log_error "Unsupported OS: $os"
            exit 1
            ;;
    esac

    log_success "Packages installed"
}

role_frps_install_binary() {
    log_step "Installing FRP Server Binary"

    local arch
    arch=$(detect_arch)

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

    log_success "frps v${FRP_VERSION} installed"
}

role_frps_configure() {
    log_step "Configuring FRP Server"

    cat > /etc/frps.toml <<EOF
bindPort = ${FRPS_BIND_PORT}
vhostHTTPPort = ${FRPS_VHOST_HTTP_PORT}

auth.method = "token"
auth.token = "${FRP_AUTH_TOKEN}"

webServer.addr = "0.0.0.0"
webServer.port = ${FRPS_DASHBOARD_PORT}
webServer.user = "admin"
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
    systemctl restart frps

    sleep 2

    if systemctl is-active --quiet frps; then
        log_success "FRP server started"
    else
        log_error "FRP server failed to start"
        journalctl -u frps --since "30 seconds ago" --no-pager || true
        exit 1
    fi
}

role_frps_configure_nginx() {
    log_step "Configuring Nginx"

    local all_domains="${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        all_domains="${all_domains} ${sub}.${BASE_DOMAIN}"
    done

    # HTTP server - ACME challenge + redirect
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

    # HTTPS server - proxy to FRP vhost HTTP
    local server_blocks=""
    for domain in $all_domains; do
        local ws_block=""
        if [ "$domain" = "ws.${BASE_DOMAIN}" ]; then
            ws_block="
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_read_timeout 86400;
"
        fi

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
        proxy_set_header X-Forwarded-Proto \$scheme;${ws_block}
    }
}
"
    done

    cat > /etc/nginx/sites-available/bharatradar-ssl <<EOF
${server_blocks}
EOF

    # Enable sites
    rm -f /etc/nginx/sites-enabled/default
    ln -sf /etc/nginx/sites-available/bharatradar-http /etc/nginx/sites-enabled/
    ln -sf /etc/nginx/sites-available/bharatradar-ssl /etc/nginx/sites-enabled/

    nginx -t
    systemctl reload nginx

    log_success "Nginx configured"
}

role_frps_setup_ssl() {
    log_step "Requesting Let's Encrypt SSL Certificate"

    systemctl restart nginx

    local domain_args="-d ${BASE_DOMAIN}"
    for sub in "${SUBDOMAINS[@]}"; do
        domain_args="${domain_args} -d ${sub}.${BASE_DOMAIN}"
    done

    log_info "Requesting certificate for: ${BASE_DOMAIN} ${SUBDOMAINS[*]}.${BASE_DOMAIN}"

    certbot --nginx ${domain_args} \
        --non-interactive --agree-tos --email "${EMAIL}" --redirect

    systemctl enable certbot.timer
    systemctl start certbot.timer

    log_success "SSL certificate configured (auto-renewal enabled)"
}

role_frps_save_config() {
    log_step "Saving Configuration"

    mkdir -p /etc/bharatradar

    local server_ip
    server_ip=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")

    cat > /etc/bharatradar/frps-config.env <<EOF
# FRP Server Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Role: frp-server

ROLE=frp-server
BASE_DOMAIN="${BASE_DOMAIN}"
EMAIL="${EMAIL}"
FRP_AUTH_TOKEN="${FRP_AUTH_TOKEN}"
FRPS_BIND_PORT=${FRPS_BIND_PORT}
FRPS_VHOST_HTTP_PORT=${FRPS_VHOST_HTTP_PORT}
FRPS_DASHBOARD_PORT=${FRPS_DASHBOARD_PORT}
FRP_SERVER_IP=${server_ip}
SUBDOMAINS="${SUBDOMAINS[*]}"
EOF

    chmod 600 /etc/bharatradar/frps-config.env
    log_success "Configuration saved to /etc/bharatradar/frps-config.env"
}

role_frps_post_install() {
    local server_ip
    server_ip=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        FRP Server Setup Complete!${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  ${CYAN}Server IP:${NC}      ${server_ip}"
    echo -e "  ${CYAN}FRP Token:${NC}      ${FRP_AUTH_TOKEN}"
    echo -e "  ${CYAN}Dashboard:${NC}      http://${server_ip}:${FRPS_DASHBOARD_PORT}"
    echo -e "  ${CYAN}Dashboard User:${NC} admin"
    echo -e "  ${CYAN}Dashboard Pass:${NC} ${FRPS_DASHBOARD_PASS}"
    echo ""
    echo -e "  ${CYAN}DNS Records to Create (point to ${server_ip}):${NC}"
    for sub in "" "${SUBDOMAINS[@]}"; do
        if [ -z "$sub" ]; then
            echo "    A  ${BASE_DOMAIN}                    → ${server_ip}"
        else
            echo "    A  ${sub}.${BASE_DOMAIN}              → ${server_ip}"
        fi
    done
    echo ""
    echo -e "  ${CYAN}Open these ports in your cloud security group:${NC}"
    echo "    7000 (FRP control)"
    echo "    80/443 (HTTP/HTTPS)"
    echo "    30004/30005 (Beast ADS-B)"
    echo "    31090 (MLAT)"
    echo ""
    echo -e "  ${CYAN}On your Hub machine, run:${NC}"
    echo "    sudo bharatradar-install"
    echo "    Select: Hub"
    echo "    Enter FRP server IP: ${server_ip}"
    echo "    Enter FRP token: ${FRP_AUTH_TOKEN}"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
}

role_frps_run() {
    require_root

    local phases=(config packages binary frp nginx ssl save)

    show_resume_banner "${phases[@]}"

    # Load existing config if present
    if [ -f /etc/bharatradar/frps-config.env ]; then
        source /etc/bharatradar/frps-config.env
        BASE_DOMAIN="${BASE_DOMAIN:-}"
        EMAIL="${EMAIL:-}"
        FRP_AUTH_TOKEN="${FRP_AUTH_TOKEN:-}"
    fi

    # Phase: config
    if ! checkpoint_completed "config"; then
        role_frps_collect_config

        save_config_value "ROLE" "frp-server"
        save_config_value "BASE_DOMAIN" "${BASE_DOMAIN}"
        save_config_value "EMAIL" "${EMAIL}"
        save_config_value "FRP_AUTH_TOKEN" "${FRP_AUTH_TOKEN}"
        save_config_value "FRPS_DASHBOARD_PASS" "${FRPS_DASHBOARD_PASS}"

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
        role_frps_install_packages || {
            log_error "Package installation failed. To retry, run:"
            echo "  sudo ./bharatradar-install frp-server"
            exit 1
        }
        checkpoint_mark "packages"
    fi

    # Phase: binary
    if ! checkpoint_completed "binary"; then
        role_frps_install_binary || {
            log_error "FRP binary download failed. To retry, run:"
            echo "  sudo ./bharatradar-install frp-server"
            exit 1
        }
        checkpoint_mark "binary"
    fi

    # Phase: frp
    if ! checkpoint_completed "frp"; then
        role_frps_configure || {
            log_error "FRP server configuration failed. To retry, run:"
            echo "  sudo ./bharatradar-install frp-server"
            exit 1
        }
        checkpoint_mark "frp"
    fi

    # Phase: nginx
    if ! checkpoint_completed "nginx"; then
        role_frps_configure_nginx || {
            log_error "Nginx configuration failed. To retry, run:"
            echo "  sudo ./bharatradar-install frp-server"
            exit 1
        }
        checkpoint_mark "nginx"
    fi

    # Phase: ssl
    if ! checkpoint_completed "ssl"; then
        role_frps_setup_ssl || {
            log_warn "SSL setup failed (DNS may not be ready yet). To retry, run:"
            echo "  sudo ./bharatradar-install frp-server"
        }
        checkpoint_mark "ssl"
    fi

    # Phase: save
    if ! checkpoint_completed "save"; then
        role_frps_save_config
        role_frps_post_install
        checkpoint_mark "save"
    fi

    checkpoint_clear
    log_success "FRP Server installation complete!"
}
