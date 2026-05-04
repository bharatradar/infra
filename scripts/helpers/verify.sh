#!/bin/bash
# Post-installation health checks and verification for BharatRadar

set -euo pipefail

# Verify K3s cluster health
verify_k3s() {
    log_step "Verifying K3s Cluster"

    if ! command -v k3s &>/dev/null; then
        log_error "K3s is not installed"
        return 1
    fi

    if ! kubectl cluster-info &>/dev/null 2>&1; then
        log_error "K3s cluster is not responding"
        return 1
    fi

    log_success "K3s cluster is running"

    # Show nodes
    log_info "Cluster nodes:"
    kubectl get nodes -o wide 2>/dev/null || true
    echo ""
}

# Verify pods in bharatradar namespace
verify_pods() {
    local namespace="bharatradar"
    log_step "Verifying Pods in '${namespace}' namespace"

    if ! kubectl get namespace "${namespace}" &>/dev/null 2>&1; then
        log_error "Namespace '${namespace}' does not exist"
        return 1
    fi

    local pod_count
    pod_count=$(kubectl get pods -n "${namespace}" --no-headers 2>/dev/null | wc -l)

    if [ "$pod_count" -eq 0 ]; then
        log_warn "No pods found in '${namespace}' namespace"
        return 1
    fi

    log_info "Total pods: ${pod_count}"
    echo ""

    # Check for failed pods
    local failed_pods
    failed_pods=$(kubectl get pods -n "${namespace}" --field-selector status.phase=Failed --no-headers 2>/dev/null | wc -l)

    if [ "$failed_pods" -gt 0 ]; then
        log_warn "${failed_pods} pod(s) in Failed state:"
        kubectl get pods -n "${namespace}" --field-selector status.phase=Failed 2>/dev/null || true
        echo ""
    fi

    # Check for pending pods
    local pending_pods
    pending_pods=$(kubectl get pods -n "${namespace}" --field-selector status.phase=Pending --no-headers 2>/dev/null | wc -l)

    if [ "$pending_pods" -gt 0 ]; then
        log_warn "${pending_pods} pod(s) still Pending (may be pulling images):"
        kubectl get pods -n "${namespace}" --field-selector status.phase=Pending 2>/dev/null || true
        echo ""
    fi

    # Show all pods
    kubectl get pods -n "${namespace}" 2>/dev/null || true
    echo ""

    if [ "$failed_pods" -eq 0 ] && [ "$pending_pods" -eq 0 ]; then
        log_success "All pods are healthy"
    fi
}

# Verify FRP client connection
verify_frpc() {
    log_step "Verifying FRP Client"

    if ! systemctl is-active --quiet frpc 2>/dev/null; then
        log_warn "FRP client (frpc) is not running"
        log_info "Check logs: journalctl -u frpc -f"
        return 1
    fi

    log_success "FRP client is running"

    # Check for successful login in recent logs
    if journalctl -u frpc --since "5 minutes ago" --no-pager 2>/dev/null | grep -q "login to server success"; then
        log_success "FRP client connected to server"
    else
        log_warn "Could not verify FRP connection status"
        log_info "Check logs: journalctl -u frpc -f"
    fi
    echo ""
}

# Verify FRP server
verify_frps() {
    log_step "Verifying FRP Server"

    if ! systemctl is-active --quiet frps 2>/dev/null; then
        log_error "FRP server (frps) is not running"
        return 1
    fi

    log_success "FRP server is running"

    if systemctl is-active --quiet nginx 2>/dev/null; then
        log_success "Nginx is running"
    else
        log_warn "Nginx is not running"
    fi

    if command -v certbot &>/dev/null; then
        local cert_count
        cert_count=$(certbot certificates 2>/dev/null | grep -c "Certificate Name:" || true)
        if [ "$cert_count" -gt 0 ]; then
            log_success "SSL certificates found: ${cert_count}"
        else
            log_warn "No SSL certificates found"
        fi
    fi
    echo ""
}

# Verify feeder services (readsb + mlat-client)
verify_feeder() {
    log_step "Verifying Feeder Services"

    local all_ok=true

    if systemctl is-active --quiet bharat-feeder 2>/dev/null; then
        log_success "bharat-feeder service is running"
    else
        log_error "bharat-feeder service is not running"
        all_ok=false
    fi

    if systemctl is-active --quiet bharat-mlat 2>/dev/null; then
        log_success "bharat-mlat service is running"
    else
        log_error "bharat-mlat service is not running"
        all_ok=false
    fi

    # Check readsb
    if systemctl is-active --quiet readsb 2>/dev/null; then
        log_success "readsb service is running"
    else
        log_warn "readsb service is not running (may use different service name)"
    fi

    # Check UUID file
    if [ -f /etc/bharat-radar-id ]; then
        local uuid
        uuid=$(cat /etc/bharat-radar-id)
        log_info "Feeder UUID: ${uuid}"
    else
        log_warn "UUID file /etc/bharat-radar-id not found"
    fi

    echo ""

    if [ "$all_ok" = true ]; then
        log_success "All feeder services are healthy"
    fi
}

# Verify DNS resolution for all subdomains
verify_dns() {
    local domain="$1"
    log_step "Verifying DNS Resolution"

    local subdomains=("map" "mlat" "history" "api" "my" "feed" "ws")
    local all_ok=true

    for sub in "${subdomains[@]}"; do
        local host="${sub}.${domain}"
        if host "$host" &>/dev/null || nslookup "$host" &>/dev/null; then
            log_success "${host}: resolves"
        else
            log_warn "${host}: not resolving (DNS may not be configured yet)"
            all_ok=false
        fi
    done

    echo ""

    if [ "$all_ok" = true ]; then
        log_success "All DNS records are resolving"
    fi
}

# Verify HTTPS endpoints
verify_endpoints() {
    local domain="$1"
    log_step "Verifying HTTPS Endpoints"

    local endpoints=(
        "map.${domain}:/|200|301|302|307"
        "api.${domain}:/api/0/|200"
        "my.${domain}:/|301|302|307"
    )

    for entry in "${endpoints[@]}"; do
        IFS=':' read -r host path codes <<< "$entry"
        IFS='|' read -ra expected_codes <<< "$codes"

        local http_code
        http_code=$(curl -sk -o /dev/null -w "%{http_code}" "https://${host}${path}" 2>/dev/null || echo "000")

        local matched=false
        for code in "${expected_codes[@]}"; do
            if [ "$http_code" = "$code" ]; then
                matched=true
                break
            fi
        done

        if [ "$matched" = true ]; then
            log_success "${host}${path}: HTTP ${http_code}"
        else
            log_warn "${host}${path}: HTTP ${http_code} (expected ${expected_codes[*]})"
        fi
    done

    echo ""
}

# Verify secrets exist
verify_secrets() {
    local namespace="bharatradar"
    log_step "Verifying Kubernetes Secrets"

    local secrets=("ghcr-secret" "bharatradar-tls" "bharatradar-rclone")

    for secret in "${secrets[@]}"; do
        if kubectl get secret "${secret}" -n "${namespace}" &>/dev/null; then
            log_success "Secret '${secret}' exists"
        else
            log_warn "Secret '${secret}' not found"
        fi
    done

    echo ""
}

# Main status command: comprehensive dashboard
verify_status() {
    local role="${1:-unknown}"
    local domain="${2:-unknown}"

    echo ""
    echo -e "${CYAN}================================================================${NC}"
    echo -e "${CYAN}        BharatRadar Status Dashboard${NC}"
    echo -e "${CYAN}================================================================${NC}"
    echo ""
    echo -e "  Role:     ${CYAN}${role}${NC}"
    echo -e "  Domain:   ${CYAN}${domain}${NC}"
    echo -e "  OS:       ${CYAN}$(detect_os) ($(detect_arch))${NC}"
    echo ""

    case "$role" in
        frp-server)
            verify_frps
            verify_dns "$domain"
            ;;
        hub)
            verify_k3s
            verify_pods
            verify_secrets
            verify_frpc
            verify_endpoints "$domain"
            ;;
        aggregator)
            verify_k3s
            ;;
        feeder)
            verify_feeder
            verify_endpoints "$domain"
            ;;
        *)
            verify_k3s 2>/dev/null || true
            verify_pods 2>/dev/null || true
            verify_frpc 2>/dev/null || true
            verify_feeder 2>/dev/null || true
            ;;
    esac

    echo -e "${CYAN}================================================================${NC}"
}
