#!/bin/bash
# Runtime manifest templating for BharatRadar
# Generates a temporary kustomize overlay that replaces domains, lat/lon, etc.
# without modifying source manifests.

set -euo pipefail

OVERLAY_DIR=""

templating_init() {
    OVERLAY_DIR=$(mktemp -d /tmp/bharatradar-overlay.XXXXXX)
    log_info "Generated overlay directory: ${OVERLAY_DIR}"
}

templating_cleanup() {
    if [ -n "${OVERLAY_DIR}" ] && [ -d "${OVERLAY_DIR}" ]; then
        rm -rf "${OVERLAY_DIR}"
        log_info "Cleaned up overlay directory"
    fi
}

# Generate the kustomization.yaml for the overlay
templating_generate_kustomization() {
    local base_dir="$1"
    local domain="$2"
    local lat="$3"
    local lon="$4"
    local tz="$5"
    local frp_server="${6:-}"
    local api_salt="${7:-}"

    # Copy all needed files to overlay (not just kustomization)
    local files=(
        "kustomization.yaml"
        "ai-agents.yaml"
        "telegram-bot.yaml"
        "flight-tracker.yaml"
    )
    for f in "${files[@]}"; do
        [ -f "${base_dir}/${f}" ] && cp "${base_dir}/${f}" "${OVERLAY_DIR}/${f}"
    done
    
    # Copy subdirs if exist
    [ -d "${base_dir}/cortex-webapp" ] && cp -r "${base_dir}/cortex-webapp" "${OVERLAY_DIR}/"

    # Patch shared services IPs in overlay copies
    templating_patch_shared_services

    # Generate domain patch for resources.yaml
    templating_patch_resources "$domain"

    # Generate lat/lon patch for planes
    templating_patch_planes "$lat" "$lon" "$tz"

    # Generate API domain/salt patch
    templating_patch_api "$domain" "$api_salt"

    # Generate api-app-py configmap from patched source
    templating_create_api_app_configmap

    # Generate ingest args patch if FRP server specified
    if [ -n "$frp_server" ]; then
        templating_patch_ingest "$frp_server"
    fi

    # Generate FRP client config
    if [ -n "$frp_server" ]; then
        templating_generate_frpc_config "$frp_server" "$domain" "${8:-}"
    fi

    log_success "Overlay generated in ${OVERLAY_DIR}"
}

# Patch shared services IPs in overlay (not source files)
templating_patch_shared_services() {
    local flight_db_host="${FLIGHT_DB_HOST:-192.168.200.12}"
    local redis_host="${REDIS_HOST:-192.168.200.12}"
    local influxdb_url="http://${INFLUXDB_HOST:-192.168.200.12}:8086"
    
    local old_ip="192.168.200.15"
    
    # Replace hardcoded IPs in files copied to overlay
    local files=(
        "${OVERLAY_DIR}/ai-agents.yaml"
        "${OVERLAY_DIR}/telegram-bot.yaml"
        "${OVERLAY_DIR}/flight-tracker.yaml"
    )
    
    for f in "${files[@]}"; do
        if [ -f "$f" ]; then
            sed -i "s/${old_ip}/${flight_db_host}/g" "$f"
            log_info "Patched $(basename $f): ${old_ip} -> ${flight_db_host}"
        fi
    done
    
    # Handle subdir files
    if [ -d "${OVERLAY_DIR}/cortex-webapp" ]; then
        sed -i "s/${old_ip}/${flight_db_host}/g" "${OVERLAY_DIR}/cortex-webapp/deployment.yaml"
    fi
    if [ -d "${OVERLAY_DIR}/schedule-downloader-manual-job.yaml" ]; then
        sed -i "s/${old_ip}/${flight_db_host}/g" "${OVERLAY_DIR}/schedule-downloader-manual-job.yaml"
    fi
    if [ -d "${OVERLAY_DIR}/schedule-downloader-cronjob.yaml" ]; then
        sed -i "s/${old_ip}/${flight_db_host}/g" "${OVERLAY_DIR}/schedule-downloader-cronjob.yaml"
    fi
}

# Patch resources.yaml: replace all domain references
templating_patch_resources() {
    local domain="$1"
    local src="${SCRIPT_DIR}/../manifests/default/resources.yaml"

    if [ ! -f "$src" ]; then
        log_error "resources.yaml not found at ${src}"
        return 1
    fi

    # Copy and replace the old domain with new one
    local old_domain="bharatradar.com"
    sed "s/${old_domain}/${domain}/g" "$src" > "${OVERLAY_DIR}/resources.yaml"

    log_info "Patched resources.yaml: ${old_domain} -> ${domain}"
}

# Patch planes deployment: lat, lon, timezone
templating_patch_planes() {
    local lat="$1"
    local lon="$2"
    local tz="$3"

    cat > "${OVERLAY_DIR}/patch-planes-env.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: planes-readsb
spec:
  template:
    spec:
      containers:
      - name: readsb
        env:
        - name: READSB_LAT
          value: "${lat}"
        - name: READSB_LON
          value: "${lon}"
        - name: TZ
          value: "${tz}"
EOF

    log_info "Generated planes env patch (lat=${lat}, lon=${lon}, tz=${tz})"
}

# Patch API: MY_DOMAIN env and generate unique salt
templating_patch_api() {
    local domain="$1"
    local api_salt="$2"

    if [ -z "$api_salt" ]; then
        api_salt=$(openssl rand -hex 64)
    fi

    cat > "${OVERLAY_DIR}/patch-api.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: api-salts
data:
  ADSBLOL_API_SALT_MY: "${api_salt}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: MY_DOMAIN
          value: "my.${domain}"
EOF

    log_info "Generated API patch (MY_DOMAIN=my.${domain})"
}

# Create api-app-py configmap from patched app.py source
templating_create_api_app_configmap() {
    local src="${SCRIPT_DIR}/../manifests/default/api/default/patched-app.py"

    if [ -f "$src" ]; then
        cat > "${OVERLAY_DIR}/api-app-py-configmap.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: api-app-py
data:
  app.py: |
$(sed 's/^/    /' "$src")
EOF
        log_info "Generated api-app-py configmap from patched source"
    else
        log_warn "patched-app.py not found, api-app-py configmap will not be created"
    fi
}

# Patch ingest to point to FRP server
templating_patch_ingest() {
    local frp_server="$1"

    cat > "${OVERLAY_DIR}/patch-ingest.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingest-readsb
spec:
  template:
    spec:
      containers:
      - name: readsb
        env:
        - name: READSB_NET_CONNECTOR
          value: "${frp_server},30004,beast_in"
EOF

    log_info "Generated ingest patch (FRP connector: ${frp_server}:30004)"
}

# Generate FRP client config for the hub
templating_generate_frpc_config() {
    local frp_server="$1"
    local domain="$2"
    local frp_token="$3"

    cat > "${OVERLAY_DIR}/frpc.toml" <<EOF
serverAddr = "${frp_server}"
serverPort = 7000

auth.method = "token"
auth.token = "${frp_token}"

[[proxies]]
name = "web-ui"
type = "http"
localIP = "127.0.0.1"
localPort = 80
customDomains = [
    "${domain}",
    "map.${domain}",
    "mlat.${domain}",
    "history.${domain}",
    "api.${domain}",
    "my.${domain}",
    "feed.${domain}",
    "ws.${domain}"
]

[[proxies]]
name = "feed-beast-30004"
type = "tcp"
localIP = "127.0.0.1"
localPort = 30004
remotePort = 30004

[[proxies]]
name = "feed-beast-30005"
type = "tcp"
localIP = "127.0.0.1"
localPort = 30005
remotePort = 30005

[[proxies]]
name = "feed-mlat-31090"
type = "tcp"
localIP = "127.0.0.1"
localPort = 31090
remotePort = 31090
EOF

    log_info "Generated FRP client config"
}

# Deploy using the generated overlay
templating_deploy() {
    local namespace="bharatradar"

    if ! command -v kustomize &>/dev/null; then
        log_info "Installing kustomize..."
        curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
        mv kustomize /usr/local/bin/
    fi

    log_step "Applying shared resources..."
    kubectl apply -f "${OVERLAY_DIR}/resources.yaml" -n "${namespace}" 2>/dev/null || true

    # Apply patches inline
    log_step "Applying deployment patches..."

    # Apply all components
    kustomize build "${OVERLAY_DIR}" 2>/dev/null | kubectl apply -f - -n "${namespace}" || {
        log_warn "kustomize build failed, falling back to source manifests with kubectl patch"
        templating_fallback_deploy "$@"
    }
}

# Fallback: deploy from source and patch individual resources
templating_fallback_deploy() {
    local domain="$1"
    local namespace="bharatradar"
    local old_domain="bharatradar.com"

    # Deploy from source manifests
    kustomize build "${SCRIPT_DIR}/../manifests/default" 2>/dev/null | kubectl apply -f - -n "${namespace}" || {
        log_warn "kustomize build from source failed"
    }

    log_info "Applying patches via kubectl..."

    # Patch ingest if FRP server was provided
    if [ -n "${FRP_SERVER:-}" ]; then
        kubectl set env deployment/ingest-readsb \
            READSB_NET_CONNECTOR="${FRP_SERVER},30004,beast_in" \
            -n "${namespace}" 2>/dev/null || true
    fi

    log_info "Patches applied"
}

# Install the generated frpc config to /etc/frpc.toml
templating_install_frpc() {
    local frp_token="$1"

    if [ -n "${OVERLAY_DIR}" ] && [ -f "${OVERLAY_DIR}/frpc.toml" ]; then
        # Ensure token is in the config
        sed -i "s/auth.token = \"\"/auth.token = \"${frp_token}\"/" "${OVERLAY_DIR}/frpc.toml"

        cp "${OVERLAY_DIR}/frpc.toml" /etc/frpc.toml
        chmod 600 /etc/frpc.toml
        log_success "Installed FRP client config to /etc/frpc.toml"
    fi
}

# Trap to clean up overlay on exit
trap templating_cleanup EXIT
