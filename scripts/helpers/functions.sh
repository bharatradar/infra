#!/bin/bash
# Helper functions for installation scripts

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "\n${CYAN}>>> $1${NC}"
}

# Validation functions
validate_domain() {
    local domain="$1"
    if [[ "$domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]]; then
        return 0
    else
        return 1
    fi
}

validate_ip() {
    local ip="$1"
    if [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 0
    else
        return 1
    fi
}

validate_latitude() {
    local lat="$1"
    if (( $(echo "$lat >= -90 && $lat <= 90" | bc -l 2>/dev/null) )); then
        return 0
    else
        return 1
    fi
}

validate_longitude() {
    local lon="$1"
    if (( $(echo "$lon >= -180 && $lon <= 180" | bc -l 2>/dev/null) )); then
        return 0
    else
        return 1
    fi
}

# Input functions
# All reads from /dev/tty to work when script is piped (curl | bash)

prompt_input() {
    local prompt_msg="$1"
    local default_value="$2"
    local result_var="$3"
    local input

    # In silent mode, use default without prompting
    if [ -n "${SILENT_MODE:-}" ]; then
        if [ -n "$default_value" ]; then
            printf -v "$result_var" '%s' "$default_value"
        else
            printf -v "$result_var" '%s' ""
        fi
        return
    fi

    if [ -n "$default_value" ]; then
        read -rp "${prompt_msg} [${default_value}]: " input < /dev/tty
        printf -v "$result_var" '%s' "${input:-$default_value}"
    else
        read -rp "${prompt_msg}: " input < /dev/tty
        printf -v "$result_var" '%s' "$input"
    fi
}

prompt_confirm() {
    local prompt_msg="$1"
    local default_value="${2:-y}"
    local response

    # In silent mode, use default without prompting
    if [ -n "${SILENT_MODE:-}" ]; then
        if [ "$default_value" = "y" ]; then
            return 0
        else
            return 1
        fi
    fi

    # If /dev/tty is not available (piped/automated), use default
    if ! [ -r /dev/tty ] 2>/dev/null; then
        if [ "$default_value" = "y" ]; then
            return 0
        else
            return 1
        fi
    fi

    if [ "$default_value" = "y" ]; then
        read -rp "${prompt_msg} [Y/n]: " response < /dev/tty
        response=${response:-y}
    else
        read -rp "${prompt_msg} [y/N]: " response < /dev/tty
        response=${response:-n}
    fi

    [[ "$response" =~ ^[Yy]$ ]]
}

prompt_select() {
    local prompt_msg="$1"
    local options=("${@:2:$(( $# - 2 ))}")
    local result_var="${@: -1}"
    local i=1
    local choice

    # In silent mode, use first option
    if [ -n "${SILENT_MODE:-}" ]; then
        printf -v "$result_var" '%s' "${options[0]}"
        return 0
    fi

    echo "$prompt_msg"
    for option in "${options[@]}"; do
        echo "  ${i}) ${option}"
        i=$((i + 1))
    done

    while true; do
        read -rp "Select [1-${#options[@]}]: " choice < /dev/tty
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
            printf -v "$result_var" '%s' "${options[$((choice - 1))]}"
            return 0
        else
            echo "Invalid selection. Please enter a number between 1 and ${#options[@]}."
        fi
    done
}

# Requirement checks
require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_error "This script must be run as root. Use 'sudo'."
        exit 1
    fi
}

require_command() {
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        log_error "Required command not found: $cmd"
        return 1
    fi
    return 0
}

detect_arch() {
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        armv7l)  echo "arm" ;;
        *)       echo "$arch" ;;
    esac
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

# Token generation
generate_token() {
    openssl rand -hex 32
}

generate_secret() {
    openssl rand -base64 32 | tr -d '/+='
}

# Detect the machine's primary LAN IP
# Tries common interface names, falls back to routing default
detect_local_ip() {
    local ip=""

    # Try common interface names first
    for iface in eth0 enp0s3 eno1 ens33 wlan0; do
        ip=$(ip -4 addr show "$iface" 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
        if [ -n "$ip" ]; then
            echo "$ip"
            return 0
        fi
    done

    # Fallback: find IP used for default route
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
    if [ -n "$ip" ]; then
        echo "$ip"
        return 0
    fi

    # Last resort: hostname -I first address
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "$ip" ]; then
        echo "$ip"
        return 0
    fi

    return 1
}

# Collect PostgreSQL connection details and build connection string
collect_pg_config() {
    local prefix="${1:-DB}"

    local host_var="${prefix}_HOST"
    local port_var="${prefix}_PORT"
    local dbname_var="${prefix}_DBNAME"
    local dbuser_var="${prefix}_DBUSER"
    local dbpass_var="${prefix}_DBPASS"

    prompt_input "PostgreSQL host IP" "" "$host_var"
    while ! validate_ip "${!host_var}"; do
        log_error "Invalid IP address"
        prompt_input "PostgreSQL host IP" "${!host_var}" "$host_var"
    done

    prompt_input "PostgreSQL port" "5432" "$port_var"
    prompt_input "PostgreSQL database name" "k3s" "$dbname_var"
    prompt_input "PostgreSQL username" "k3s" "$dbuser_var"
    prompt_input "PostgreSQL password" "" "$dbpass_var"

    while [ -z "${!dbpass_var}" ]; do
        log_error "Password cannot be empty"
        prompt_input "PostgreSQL password" "" "$dbpass_var"
    done

    local conn_string="postgres://${!dbuser_var}:${!dbpass_var}@${!host_var}:${!port_var}/${!dbname_var}"
    printf -v "${prefix}_CONNECTION_STRING" '%s' "$conn_string"
}

# Validate a PostgreSQL connection string
# Format: postgres://user:pass@host:port/dbname
validate_pg_connstring() {
    local connstr="$1"
    if [[ "$connstr" =~ ^postgres://[a-zA-Z0-9_]+:[^@]+@[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/[a-zA-Z0-9_]+$ ]]; then
        return 0
    else
        return 1
    fi
}

# Fix kubeconfig permissions and symlink to ~/.kube/config
setup_kubectl() {
    log_step "Configuring kubectl"

    if [ ! -f /etc/rancher/k3s/k3s.yaml ]; then
        log_warn "k3s.yaml not found yet, skipping kubeconfig setup"
        return 0
    fi

    chmod 644 /etc/rancher/k3s/k3s.yaml

    local target_user="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
    local target_home
    target_home=$(eval echo "~${target_user}")

    mkdir -p "${target_home}/.kube"
    cp -f /etc/rancher/k3s/k3s.yaml "${target_home}/.kube/config"
    chown -R "${target_user}:" "${target_home}/.kube"

    log_success "kubectl configured for ${target_user}"
}

# Checkpoint / resume functions
# Track install progress so we can resume from failures

checkpoint_file="/etc/bharatradar/.install-progress"
partial_config="/etc/bharatradar/.config.partial"

save_config_value() {
    local key="$1"
    local value="$2"

    mkdir -p "$(dirname "$partial_config")"
    chmod 700 "$(dirname "$partial_config")"

    if grep -q "^${key}=" "$partial_config" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=\"${value}\"|" "$partial_config"
    else
        echo "${key}=\"${value}\"" >> "$partial_config"
    fi
    chmod 600 "$partial_config"
}

load_partial_config() {
    if [ -f "$partial_config" ]; then
        source "$partial_config"
        log_info "Loaded saved configuration (resume mode)"
        return 0
    fi
    return 1
}

checkpoint_mark() {
    local phase="$1"
    echo "COMPLETED:${phase}:$(date -u +%s)" >> "$checkpoint_file"
    chmod 600 "$checkpoint_file"
}

checkpoint_completed() {
    local phase="$1"
    grep -q "^COMPLETED:${phase}:" "$checkpoint_file" 2>/dev/null
}

checkpoint_clear() {
    rm -f "$checkpoint_file"
    rm -f "$partial_config"
}

# Retry with exponential backoff
retry_with_backoff() {
    local max_attempts="${1:-3}"
    shift
    local attempt=1
    local delay=2

    while [ $attempt -le $max_attempts ]; do
        if "$@"; then
            return 0
        fi

        if [ $attempt -lt $max_attempts ]; then
            log_warn "Attempt ${attempt}/${max_attempts} failed. Retrying in ${delay}s..."
            sleep $delay
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done

    log_error "All ${max_attempts} attempts failed."
    return 1
}

# Show resume banner if we have saved progress
# Usage: show_resume_banner [phase1 phase2 phase3 ...]
# If no phases provided, defaults to hub phases for backward compatibility.
show_resume_banner() {
    if [ ! -f "$checkpoint_file" ]; then
        return
    fi

    echo ""
    echo -e "${CYAN}>>> Resuming Previous Installation${NC}"
    echo ""

    local saved_date=""
    if [ -f "$partial_config" ]; then
        saved_date=$(stat -c %y "$partial_config" 2>/dev/null | cut -d' ' -f1,2 | cut -d'.' -f1)
        log_info "Saved progress from: ${saved_date}"
    fi

    local phases=("$@")
    if [ ${#phases[@]} -eq 0 ]; then
        phases=(config k3s secrets deploy verify)
    fi

    for phase in "${phases[@]}"; do
        if checkpoint_completed "$phase"; then
            echo -e "  ${GREEN}✓${NC} Phase: ${phase}"
        else
            echo -e "  ${RED}✗${NC} Phase: ${phase} (pending)"
        fi
    done
    echo ""
}

# Install bharatradar-install into PATH
install_bharatradar_cli() {
    log_step "Installing CLI to PATH"

    if [ -z "${SCRIPT_DIR:-}" ]; then
        log_warn "SCRIPT_DIR not set, cannot install CLI"
        return 0
    fi

    local installer="${SCRIPT_DIR}/bharatradar-install"
    if [ ! -f "$installer" ]; then
        log_warn "Installer not found at ${installer}"
        return 0
    fi

    ln -sf "$installer" /usr/local/bin/bharatradar-install
    chmod +x "$installer"

    log_success "CLI installed to /usr/local/bin/bharatradar-install"
}
