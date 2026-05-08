#!/bin/bash
# Uninstall and cleanup functions for BharatRadar

set -euo pipefail

# Uninstall K3s server
uninstall_k3s_server() {
    log_step "Uninstalling K3s Server"

    if prompt_confirm "This will remove K3s and all workloads. Continue?"; then
        if command -v k3s &>/dev/null; then
            log_info "Running k3s-uninstall.sh..."
            /usr/local/bin/k3s-uninstall.sh 2>/dev/null || {
                log_warn "k3s-uninstall.sh failed, cleaning manually..."
                systemctl stop k3s 2>/dev/null || true
                rm -rf /usr/local/bin/k3s /usr/local/bin/k3s-uninstall.sh
                rm -rf /etc/rancher/k3s
                rm -rf /var/lib/rancher/k3s
                rm -rf /var/lib/cni
            }
            log_success "K3s server removed"
        else
            log_info "K3s server is not installed"
        fi
    fi
}

# Uninstall K3s agent
uninstall_k3s_agent() {
    log_step "Uninstalling K3s Agent"

    if prompt_confirm "This will remove K3s agent. Continue?"; then
        if command -v k3s &>/dev/null; then
            log_info "Running k3s-agent-uninstall.sh..."
            /usr/local/bin/k3s-agent-uninstall.sh 2>/dev/null || {
                log_warn "k3s-agent-uninstall.sh failed, cleaning manually..."
                systemctl stop k3s-agent 2>/dev/null || true
                rm -rf /usr/local/bin/k3s /usr/local/bin/k3s-agent-uninstall.sh
                rm -rf /etc/rancher/k3s
                rm -rf /var/lib/rancher/k3s
            }
            log_success "K3s agent removed"
        else
            log_info "K3s agent is not installed"
        fi
    fi
}

# Uninstall FRP client
uninstall_frpc() {
    log_step "Uninstalling FRP Client"

    if systemctl is-active --quiet frpc 2>/dev/null; then
        systemctl stop frpc 2>/dev/null || true
        systemctl disable frpc 2>/dev/null || true
        rm -f /etc/systemd/system/frpc.service
        systemctl daemon-reload
        log_info "FRP client service removed"
    fi

    if [ -f /usr/local/bin/frpc ]; then
        rm -f /usr/local/bin/frpc
        log_info "FRP client binary removed"
    fi

    if [ -f /etc/frpc.toml ]; then
        rm -f /etc/frpc.toml
        log_info "FRP client config removed"
    fi

    log_success "FRP client uninstalled"
}

# Uninstall FRP server
uninstall_frps() {
    log_step "Uninstalling FRP Server"

    if systemctl is-active --quiet frps 2>/dev/null; then
        systemctl stop frps 2>/dev/null || true
        systemctl disable frps 2>/dev/null || true
        rm -f /etc/systemd/system/frps.service
        systemctl daemon-reload
        log_info "FRP server service removed"
    fi

    if [ -f /usr/local/bin/frps ]; then
        rm -f /usr/local/bin/frps
        log_info "FRP server binary removed"
    fi

    if [ -f /etc/frps.toml ]; then
        rm -f /etc/frps.toml
        log_info "FRP server config removed"
    fi

    log_success "FRP server uninstalled"
}

# Uninstall nginx
uninstall_nginx() {
    log_step "Uninstalling Nginx"

    if prompt_confirm "This will remove nginx and its configuration. Continue?"; then
        local os
        os=$(detect_os)

        case "$os" in
            debian|ubuntu|raspbian)
                apt-get remove -y nginx 2>/dev/null || true
                apt-get autoremove -y 2>/dev/null || true
                ;;
            fedora|centos|rhel)
                dnf remove -y nginx 2>/dev/null || true
                ;;
        esac

        rm -f /etc/nginx/sites-available/bharatradar-http
        rm -f /etc/nginx/sites-available/bharatradar-ssl
        rm -f /etc/nginx/sites-enabled/bharatradar-http
        rm -f /etc/nginx/sites-enabled/bharatradar-ssl

        log_success "Nginx uninstalled"
    fi
}

# Uninstall feeder services
uninstall_feeder() {
    log_step "Uninstalling Feeder Services"

    if systemctl is-active --quiet bharat-feeder 2>/dev/null; then
        systemctl stop bharat-feeder 2>/dev/null || true
        systemctl disable bharat-feeder 2>/dev/null || true
        rm -f /etc/systemd/system/bharat-feeder.service
        systemctl daemon-reload
        log_info "bharat-feeder service removed"
    fi

    if systemctl is-active --quiet bharat-mlat 2>/dev/null; then
        systemctl stop bharat-mlat 2>/dev/null || true
        systemctl disable bharat-mlat 2>/dev/null || true
        rm -f /etc/systemd/system/bharat-mlat.service
        systemctl daemon-reload
        log_info "bharat-mlat service removed"
    fi

    if [ -f /etc/bharat-radar-id ]; then
        rm -f /etc/bharat-radar-id
        log_info "UUID file removed"
    fi

    log_success "Feeder services uninstalled"
}

# Uninstall PostgreSQL
uninstall_postgresql() {
    log_step "Uninstalling PostgreSQL"

    # CRITICAL: Handle broken influxdb2 BEFORE apt operations to prevent autoremove errors
    log_info "Checking for broken packages before PostgreSQL removal..."
    if dpkg -l 2>/dev/null | grep -q "^.i.*influxdb2"; then
        log_warn "Found broken influxdb2 - fixing first..."
        # Move each file explicitly (not glob - more reliable)
        for f in /var/lib/dpkg/info/influxdb2.*; do
            [ -f "$f" ] && sudo mv "$f" /tmp/
        done
        sudo dpkg --remove --force-remove-reinstreq influxdb2
    fi

    local os
    os=$(detect_os)

    if prompt_confirm "This will stop PostgreSQL, drop the k3s database and user, and remove packages. Continue?"; then
        # Stop the actual cluster service, not just the meta-service
        local pg_version
        pg_version=$(ls /etc/postgresql/ 2>/dev/null | head -1)
        if [ -n "$pg_version" ]; then
            systemctl stop "postgresql@${pg_version}-main" 2>/dev/null || true
            systemctl disable "postgresql@${pg_version}-main" 2>/dev/null || true
        fi
        systemctl stop postgresql 2>/dev/null || true
        systemctl disable postgresql 2>/dev/null || true

        if command -v psql &>/dev/null; then
            sudo -u postgres psql -c "DROP DATABASE IF EXISTS k3s;" 2>/dev/null || true
            sudo -u postgres psql -c "DROP USER IF EXISTS k3s;" 2>/dev/null || true
        fi

        case "$os" in
            debian|ubuntu|raspbian)
                apt-get remove -y -qq postgresql postgresql-client 2>/dev/null || true
                apt-get autoremove -y -qq 2>/dev/null || true
                ;;
            fedora|centos|rhel)
                dnf remove -y postgresql-server postgresql 2>/dev/null || true
                ;;
        esac

        if prompt_confirm "Also remove PostgreSQL data directory?"; then
            rm -rf /var/lib/postgresql/*
            log_info "PostgreSQL data removed"
        fi

        log_success "PostgreSQL uninstalled"
    fi
}

# Uninstall Redis
uninstall_redis() {
    log_step "Uninstalling Redis"

    # CRITICAL: Handle broken influxdb2 BEFORE apt operations
    log_info "Checking for broken packages before Redis removal..."
    if dpkg -l 2>/dev/null | grep -q "^.i.*influxdb2"; then
        log_warn "Found broken influxdb2 - fixing first..."
        for f in /var/lib/dpkg/info/influxdb2.*; do
            [ -f "$f" ] && sudo mv "$f" /tmp/
        done
        sudo dpkg --remove --force-remove-reinstreq influxdb2
    fi

    local os
    os=$(detect_os)

    if prompt_confirm "This will remove Redis. Continue?"; then
        systemctl stop redis-server 2>/dev/null || true
        systemctl disable redis-server 2>/dev/null || true

        case "$os" in
            debian|ubuntu|raspbian)
                apt-get remove -y -qq redis-server 2>/dev/null || true
                apt-get autoremove -y -qq 2>/dev/null || true
                ;;
            fedora|centos|rhel)
                dnf remove -y redis 2>/dev/null || true
                ;;
        esac

        log_success "Redis uninstalled"
    fi
}

# Uninstall InfluxDB
uninstall_influxdb() {
    log_step "Uninstalling InfluxDB"

    if prompt_confirm "This will remove InfluxDB and all data. Continue?"; then
        log_info "Removing broken influxdb2 package..."

        for f in /var/lib/dpkg/info/influxdb2.*; do
            [ -f "$f" ] && sudo mv "$f" /tmp/
        done
        sudo dpkg --remove --force-remove-reinstreq influxdb2
        sudo apt-get autoremove
        sudo apt-get autoclean
        sudo apt-get update
        sudo systemctl unmask influxdb
        sudo apt-get purge influxdb2

        if prompt_confirm "Also remove InfluxDB data directory?"; then
            rm -rf /var/lib/influxdb2/*
            log_info "InfluxDB data removed"
        fi

        log_success "InfluxDB uninstalled"
    fi
}

# Uninstall MinIO
uninstall_minio() {
    log_step "Uninstalling MinIO"

    if systemctl is-active --quiet minio 2>/dev/null; then
        systemctl stop minio 2>/dev/null || true
        systemctl disable minio 2>/dev/null || true
        rm -f /etc/systemd/system/minio.service
        systemctl daemon-reload
        log_info "MinIO service removed"
    fi

    if [ -f /usr/local/bin/minio ]; then
        rm -f /usr/local/bin/minio
        log_info "MinIO binary removed"
    fi

    if [ -f /usr/local/bin/mc ]; then
        rm -f /usr/local/bin/mc
        log_info "MinIO client removed"
    fi

    if prompt_confirm "Also remove MinIO data directory (/data/minio)?"; then
        rm -rf /data/minio
        log_info "MinIO data removed"
    fi

    log_success "MinIO uninstalled"
}

# Uninstall shared services (PostgreSQL + Redis + InfluxDB + MinIO)
uninstall_shared_services() {
    log_step "Uninstalling Shared Services"

    if prompt_confirm "Remove PostgreSQL?"; then
        uninstall_postgresql
    fi

    if prompt_confirm "Remove Redis?"; then
        uninstall_redis
    fi

    if prompt_confirm "Remove InfluxDB?"; then
        uninstall_influxdb
    fi

    if prompt_confirm "Remove MinIO?"; then
        uninstall_minio
    fi

    if prompt_confirm "Remove all shared data directories (/var/lib/postgresql, /var/lib/redis, /var/lib/influxdb2, /data/minio)?"; then
        rm -rf /var/lib/postgresql/* /var/lib/redis/* /var/lib/influxdb2/* /data/minio
        log_info "All shared data directories removed"
    fi
}

# Uninstall keepalived
uninstall_keepalived() {
    log_step "Uninstalling Keepalived"

    systemctl stop keepalived 2>/dev/null || true
    systemctl disable keepalived 2>/dev/null || true

    if [ -f /etc/keepalived/keepalived.conf ]; then
        rm -f /etc/keepalived/keepalived.conf
    fi

    local os
    os=$(detect_os)
    case "$os" in
        debian|ubuntu|raspbian)
            apt-get remove -y -qq keepalived 2>/dev/null || true
            ;;
        fedora|centos|rhel)
            dnf remove -y keepalived 2>/dev/null || true
            ;;
    esac

    log_success "Keepalived uninstalled"
}

# Remove BharatRadar config directory
uninstall_config() {
    log_step "Removing Configuration"

    if [ -d /etc/bharatradar ]; then
        rm -rf /etc/bharatradar
        log_success "Configuration directory removed"
    else
        log_info "No configuration directory found"
    fi
}

# Remove certbot certificates
uninstall_certbot() {
    log_step "Removing SSL Certificates"

    if prompt_confirm "This will delete Let's Encrypt certificates. Continue?"; then
        if command -v certbot &>/dev/null; then
            log_info "Deleting certificates..."
            certbot delete --non-interactive 2>/dev/null || true
            log_success "Certificates removed"
        fi
    fi
}

# Full uninstall: prompts for what to remove
uninstall_all() {
    local role="${1:-}"

    echo ""
    echo -e "${RED}================================================================${NC}"
    echo -e "${RED}        BharatRadar Uninstall${NC}"
    echo -e "${RED}================================================================${NC}"
    echo ""
    log_warn "This will remove BharatRadar components from this machine."
    echo ""

    if [ -f /etc/bharatradar/config.env ]; then
        source /etc/bharatradar/config.env
        role="${ROLE:-${role}}"
    fi

    case "$role" in
        frp-server)
            uninstall_frps
            uninstall_nginx
            uninstall_certbot
            ;;
        hub|ha-server)
            uninstall_frpc
            uninstall_keepalived 2>/dev/null || true
            uninstall_k3s_server
            ;;
        worker)
            uninstall_k3s_agent
            ;;
        aggregator)
            uninstall_k3s_agent
            ;;
        feeder)
            uninstall_feeder
            uninstall_frpc 2>/dev/null || true
            ;;
        shared-services|db-standby)
            uninstall_shared_services
            ;;
        *)
            # Unknown role: ask what to remove
            log_info "Unknown role, showing all uninstall options:"
            echo ""

            if prompt_confirm "Remove K3s server?"; then
                uninstall_k3s_server
            fi

            if prompt_confirm "Remove K3s agent?"; then
                uninstall_k3s_agent
            fi

            if prompt_confirm "Remove FRP client?"; then
                uninstall_frpc
            fi

            if prompt_confirm "Remove FRP server?"; then
                uninstall_frps
            fi

            if prompt_confirm "Remove feeder services?"; then
                uninstall_feeder
            fi

            if prompt_confirm "Remove shared data services (PostgreSQL, Redis, InfluxDB)?"; then
                uninstall_shared_services
            fi

            if prompt_confirm "Remove keepalived?"; then
                uninstall_keepalived
            fi

            if prompt_confirm "Remove nginx?"; then
                uninstall_nginx
            fi
            ;;
    esac

    if prompt_confirm "Remove BharatRadar configuration files?"; then
        uninstall_config
    fi

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}        Uninstall Complete${NC}"
    echo -e "${GREEN}================================================================${NC}"
}
