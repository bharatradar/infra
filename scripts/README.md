# BharatRadar Infrastructure Scripts

Automated installation and management for the BharatRadar ADS-B/MLAT platform.

## Architecture

```
                     Cloudflare DNS → map.bharat-radar.vellur.in
                                           |
                            ┌──────────────┴──────────────┐
                            │      Keepalived VIP         │
                            │    (auto-failover)          │
                            └──────┬───────────────┬──────┘
                                   │               │
               ┌─────────────────────────┐ ┌─────────────────────────┐
               │   HUB (K3s server)      │ │   HA SERVER (K3s)       │
               │   192.168.200.145 (i7)  │ │   192.168.200.155 (i5)  │
               │   MASTER keepalived     │ │   BACKUP keepalived     │
               │   planes api mlat hub   │ │   joins same DB         │
               │   haproxy redis mlat-map│ │                         │
               └───────────┬─────────────┘ └─────────────┬───────────┘
                           │         shared PostgreSQL   │
                           │              │              │
                           v              v              v
               ┌───────────────────────────────────────────────────────┐
               │  SHARED SERVICES (Pi)                                 │
               │  192.168.200.127                                      │
               │  PostgreSQL ←───────────────────────── DB Standby (opt)│
               │  Redis                                                  │
               │  InfluxDB                                               │
               └─────────────────────────────────────────────────────────┘
                                       ▲
                                       │ feeds to FRP server
     FEEDER PI (not K3s, standalone)   │
     192.168.200.127                   │
     readsb → feed.bharat-radar.vellur.in:30004 (via AWS FRP)
     mlat-client → feed.bharat-radar.vellur.in:31090 (via AWS FRP)

     WORKER NODE (K3s agent, optional)
     joins via Hub:6443, runs scheduled pods
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
# Step 1: Shared Services (PostgreSQL + Redis + InfluxDB)
# Always prompts for primary or standby
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services

# Step 2: Primary Hub (first K3s server, creates cluster)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- hub

# Step 3a: HA Server (second K3s server, shares control plane)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- ha-server

# Step 3b: Worker Node (K3s agent, runs pods only)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- worker

# DB Standby (PostgreSQL streaming replica for failover)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- db-standby

# Feeder Pi (RTL-SDR receiver, standalone)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- feeder

# FRP Server (Cloud/VPS with public IP)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- frp-server
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
sudo ./bharatradar-install ha-server
sudo ./bharatradar-install worker
```

### Post-Install Commands

```bash
sudo ./bharatradar-install status        # Health dashboard
sudo ./bharatradar-install remove-node   # Remove a node from cluster
sudo ./bharatradar-install update        # Update scripts and redeploy
sudo ./bharatradar-install backup        # Backup configuration
sudo ./bharatradar-install uninstall     # Remove all components
```

## Scripts Overview

| Script | Purpose |
|--------|---------|
| `bharatradar-install` | **Main entry point** - unified installer for all roles |
| `bharatradar-setup` | DEPRECATED - redirects to bharatradar-install |
| `bharatradar-cluster` | Advanced CLI for CI/CD and power users |

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

| Feature | Shared Services | Hub | HA Server | Worker | DB Standby | Feeder Pi | FRP Server |
|---------|-----------------|-----|-----------|--------|------------|-----------|------------|
| OS | Debian/RPi OS | Ubuntu/Debian | Ubuntu/Debian | Any Linux | Debian/Ubuntu | Raspberry Pi OS | Ubuntu/Debian |
| K3s | No | Server | Server | Agent | No | No | No |
| Services | PostgreSQL, Redis, InfluxDB | All (planes, api, mlat, etc.) | Joins same DB, no separate deploy | Runs scheduled pods | PostgreSQL replica | readsb + mlat-client | frps, nginx |
| Keepalived | No | MASTER (optional) | BACKUP (optional) | No | No | No | No |
| Hardware | Raspberry Pi or any | Powerful machine (i7+) | Any server | Any | Same as primary DB | Raspberry Pi + RTL-SDR | Cloud VPS / AWS EC2 |
| Public IP | No | Optional | No | No | No | No | Required |

## Recommended Setup Order

1. **Shared Services** → on Raspberry Pi (installs PostgreSQL, Redis, InfluxDB)
2. **Primary Hub** → on Core i7 (creates K3s cluster, deploys all services)
3. **HA Server** → on Mac Mini or second server (joins cluster, enables failover)
4. **Worker** → on any additional machine (runs pods)
5. **Feeder Pi** → on RTL-SDR machine (sends ADS-B data to cluster)

Optional: **DB Standby** for PostgreSQL replica, **FRP Server** for public access if behind NAT.

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
sudo rm -f /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial
sudo ./bharatradar-install <role>
```
