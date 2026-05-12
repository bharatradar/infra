# BharatRadar Infrastructure Scripts

Automated installation and management for the BharatRadar ADS-B/MLAT platform.

## Architecture

```
                     Cloudflare DNS → map.bharatradar.com
                                            |
                                            v
                ┌───────────────────────────────────────────┐
                │   HUB (45.88.189.38)                      │
                │   K3s server + Shared Services             │
                │   PostgreSQL, Redis, InfluxDB, MinIO       │
                │   All pods: planes, api, mlat, hub, etc.  │
                └───────────────────────────────────────────┘
                                        ▲
                                        │ feeds directly
      FEEDER PI (not K3s, standalone)   │
      192.168.200.127                   │
      readsb → feed.bharatradar.com:30004
      mlat-client → feed.bharatradar.com:31090
```

## Quick Start

### Interactive Wizard

Run on any machine and follow the prompts:

```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash
```

### Online Install (Non-Interactive)

Run the appropriate command on each machine:

```bash
# Step 1: Shared Services + Hub (all on same server)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services

# Fresh install (clear stale checkpoints from failed run)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --fresh shared-services

# Step 2: Hub (creates K3s cluster, deploys all services)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- hub

# Feeder Pi (RTL-SDR receiver, standalone)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- feeder
```

### Local Install

```bash
# Clone the repo
git clone https://github.com/bharatradar/infra.git
cd infra/scripts

# Interactive wizard (consolidated menu with sub-prompts)
sudo ./bharatradar-install

# Or specify role directly
sudo ./bharatradar-install hub
```

### Post-Install Commands

```bash
sudo ./bharatradar-install status        # Health dashboard
sudo ./bharatradar-install remove-node   # Remove a node from cluster
sudo ./bharatradar-install update        # Update scripts and redeploy
sudo ./bharatradar-install backup        # Backup configuration
sudo ./bharatradar-install uninstall     # Remove all components (interactive)
sudo ./bharatradar-install uninstall shared-services -y  # Non-interactive, removes everything including data
```

### Fresh Install (Skip Resume)

If a previous install failed and left stale checkpoints, use `--fresh` to start clean:

```bash
curl -Ls ... | sudo bash -s -- --fresh shared-services
sudo ./bharatradar-install --fresh hub
```

## Scripts Overview

| Script | Purpose |
|--------|---------|
| `bharatradar-install` | **Main entry point** - unified installer for all roles |
| `bharatradar-setup` | DEPRECATED - redirects to bharatradar-install |
| `bharatradar-cluster` | Advanced CLI for CI/CD and power users |

### Database Scripts (`db/`)

| Script | Purpose |
|--------|---------|
| `db/init.sh` | Initialize PostgreSQL, Redis, and seed data |
| `db/postgres/schema.sql` | Table definitions (airports, flights, feeders, etc.) |
| `db/postgres/seed-airports.sql` | ~130 Indian airports |
| `db/postgres/seed-runways.sql` | Runway data |

> These are automatically used during `shared-services` installation. See [db/README.md](db/README.md) for manual reset instructions.

### Role Modules (`roles/`)

| Module | Description |
|--------|-------------|
| `roles/shared-services.sh` | PostgreSQL + Redis + InfluxDB (primary or standby) |
| `roles/hub.sh` | Primary K3s server + all BharatRadar services (new cluster) |
| `roles/ha-server.sh` | Second K3s server joining existing cluster (HA) |
| `roles/worker.sh` | K3s agent node joining existing cluster |
| `roles/db-standby.sh` | PostgreSQL streaming replica |
| `roles/frp-server.sh` | FRP server setup (AWS/cloud VPS) |
| `roles/feeder.sh` | RTL-SDR standalone receiver setup |
| `roles/keepalived.sh` | Floating VIP for automatic server failover |

### Helper Modules (`helpers/`)

| Module | Description |
|--------|-------------|
| `helpers/functions.sh` | Shared utilities (logging, validation, prompts, IP detection) |
| `helpers/templating.sh` | Runtime manifest overlay generation |
| `helpers/verify.sh` | Post-install health checks |
| `helpers/uninstall.sh` | Cleanup and reset functions |
| `helpers/ssh-helpers.sh` | SSH/SCP wrappers, interactive node removal |
| `helpers/node-ops.sh` | kubectl drain, cordon, and node management |

### Standalone Scripts (kept for compatibility)

| Script | Description |
|--------|-------------|
| `frp/setup-frps.sh` | Standalone FRP server setup |
| `frp/setup-frpc.sh` | Standalone FRP client setup |
| `install/setup-nginx-ssl.sh` | Standalone nginx + Let's Encrypt setup |

## Installation Flow

```
bharatradar-install (no args)
│
├── [1] Prerequisites check
│     ├── Root access
│     ├── OS (Debian/Ubuntu/RPi OS)
│     ├── Architecture (amd64/arm64/arm)
│     └── Network connectivity
│
├── [2] Main menu selection
│     ├── 1) Shared Services
│     │       ├── 1) Primary DB Setup (fresh install)
│     │       └── 2) Join as DB Standby (streaming replica)
│     │
│     ├── 2) K3s Cluster
│     │       ├── 1) New Cluster (Primary Hub)
│     │       ├── 2) Join as HA Server
│     │       └── 3) Join as Worker
│     │
│     ├── 3) Feeder Pi
│     ├── 4) FRP Server
│     └── 5) Manage
│             ├── 1) Status
│             ├── 2) Remove Node
│             ├── 3) Backup
│             └── 4) Update
│
├── [3] Configuration collection
│     ├── Base domain
│     ├── Lat/lon/timezone
│     └── Role-specific (GHCR creds, DB connection, FRP token, etc.)
│
├── [4] Installation
│     ├── Package installation
│     ├── Binary downloads
│     ├── Service configuration
│     └── Deployment
│
└── [5] Post-install
      ├── Configuration saved to /etc/bharatradar/
      ├── Checkpoint cleared (install complete)
      ├── Health verification
      └── URLs, credentials, and next steps displayed

Checkpoint / Resume (automatic on all roles):
- Each role tracks completed phases in /etc/bharatradar/.install-progress
- Config answers saved to /etc/bharatradar/.config.partial
- Re-run the same command to resume from the last successful phase
- To restart from scratch: rm /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial
```

## Node Roles

| Feature | Hub | Feeder Pi |
|---------|-----|-----------|
| OS | Ubuntu/Debian | Raspberry Pi OS |
| K3s | Yes (Server) | No |
| Services | All (PostgreSQL, Redis, InfluxDB, MinIO, planes, api, mlat, etc.) | readsb + mlat-client |
| Hardware | Server machine | Raspberry Pi + RTL-SDR |
| Public IP | Yes (45.88.189.38) | No |

## Recommended Setup Order

1. **Shared Services + Hub** → on the same server (45.88.189.38) — installs PostgreSQL, Redis, InfluxDB, MinIO, creates K3s cluster, deploys all services
2. **Feeder Pi** → on RTL-SDR machine (sends ADS-B data to cluster)

## Requirements

- **Root/sudo** access on all machines
- **bash** shell (4.0+)
- **curl**, **wget**, **openssl**
- **systemd** for service management
- **Linux**: Debian 11+, Ubuntu 20.04+, Raspberry Pi OS
- **Architectures**: amd64, arm64, arm (armv7l)

## Configuration

All scripts save configuration to `/etc/bharatradar/config.env`.

```bash
# View current configuration
cat /etc/bharatradar/config.env

# Backup configuration
sudo ./bharatradar-install backup

# Restore from backup
sudo tar xzf /tmp/bharatradar-backup-YYYYMMDD_HHMMSS.tar.gz -C /
```

## Troubleshooting

```bash
# Check overall status
sudo ./bharatradar-install status

# Check K3s cluster
kubectl get nodes
kubectl get pods -n bharatradar

# Check FRP
sudo systemctl status frps     # On FRP server
sudo systemctl status frpc     # On Hub
journalctl -u frpc -f          # FRP client logs

# Check feeder services
sudo systemctl status bharat-feeder
sudo systemctl status bharat-mlat
journalctl -u bharat-feeder -f

# Check Keepalived VIP
ip addr show | grep <VIP>
sudo systemctl status keepalived

# Check shared services
sudo systemctl status postgresql
sudo systemctl status redis-server
sudo systemctl status influxdb

# Remove a misbehaving node
sudo ./bharatradar-install remove-node

# Full uninstall and clean start
sudo ./bharatradar-install uninstall

# Non-interactive uninstall (auto-confirm all prompts, remove data)
sudo ./bharatradar-install uninstall shared-services -y
sudo ./bharatradar-install uninstall hub -y
```

## Checkpoint / Resume

All roles automatically save progress and can resume from failures.

### How It Works

- **Phase tracking:** `/etc/bharatradar/.install-progress` lists completed phases
- **Config cache:** `/etc/bharatradar/.config.partial` stores all answers
- **Resume banner:** On restart, shows ✓ (complete) and ✗ (pending) phases

### Resume a Failed Install

Simply re-run the same command. No need to re-answer questions.

```bash
# Interactive
sudo ./bharatradar-install hub

# Non-interactive
curl -Ls ... | sudo bash -s -- hub

# Silent
sudo ./bharatradar-install --conf-file /tmp/hub.env hub
```

### View Progress

```bash
cat /etc/bharatradar/.install-progress
cat /etc/bharatradar/.config.partial
```

### Restart from Scratch

```bash
# Option 1: Use --fresh flag (recommended)
sudo ./bharatradar-install --fresh <role>

# Option 2: Manually clear checkpoint files
sudo rm -f /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial
sudo ./bharatradar-install <role>
```
