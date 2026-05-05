# BharatRadar Infrastructure - Installation Guide

> **Version:** 5.7.0
> **Last Updated:** May 2026
> **GitHub:** https://github.com/bharatradar/infra

Complete guide to deploying the BharatRadar ADS-B/MLAT aggregator platform with FRP tunneling and multi-node Kubernetes cluster support.

---

## Table of Contents

 1. [Architecture Overview](#architecture-overview)
 2. [Prerequisites](#prerequisites)
 3. [Quick Start](#quick-start)
 4. [Online Install (Non-Interactive)](#online-install-non-interactive)
 5. [Silent Installation](#silent-installation)
 6. [Checkpoint / Resume](#checkpoint--resume)
 7. [Silent Configuration Reference](#silent-configuration-reference)
 8. [Step-by-Step Installation](#step-by-step-installation)
 9. [Custom Docker Images](#custom-docker-images)
 10. [FRP Server Setup (AWS/Cloud)](#frp-server-setup-awscloud)
 11. [FRP Client Setup (Hub Node)](#frp-client-setup-hub-node)
 12. [Feeder Pi Setup](#feeder-pi-setup)
 13. [Disk Maintenance (Feeder Pi)](#disk-maintenance-feeder-pi)
 14. [SSL/TLS Configuration](#ssltls-configuration)
 15. [Cluster Management](#cluster-management)
 16. [Configuration Reference](#configuration-reference)
 17. [Troubleshooting](#troubleshooting)
 18. [Useful Commands](#useful-commands)

---

## Architecture Overview

```
                    Cloudflare (DNS only)
                            |
                            v
                    AWS EC2 frps + nginx
                 (feed.bharatradar.com)
                    /            |            \
            port 30004    port 31090    HTTP/HTTPS tunnels
                   |             |              |
                   v             v              v
         ┌─────────────────────────────────────────────┐
         │       PRIMARY HUB (Ubuntu i7)               │
         │       192.168.200.145 (MASTER)              │
         │       VIP: 192.168.200.150                  │
         │                                             │
         │  ingest  hub  planes  api  mlat  mlat-map   │
         │  external  reapi  history  website          │
         └──────────────┬──────────────────────────────┘
                        │ k3s join (shared PostgreSQL)
                        v
         ┌─────────────────────────────────────────────┐
         │       HA SERVER (Ubuntu i7, Backup)         │
         │       192.168.200.186 (BACKUP)              │
         │       VIP: 192.168.200.150 (on failover)    │
         └─────────────────────────────────────────────┘
                        │ k3s join
                        v
         ┌─────────────────────────────┐
         │  BR-AGGRIGATOR (Pi, agent)  │
         │  192.168.200.15            │
         │  PostgreSQL, Redis, MinIO   │
         └─────────────────────────────┘

    FEEDER PI (not K3s)
    192.168.200.127
    readsb → feed.bharatradar.com:30004
    mlat-client → feed.bharatradar.com:31090
```

### Nodes

| Node | IP | Role | OS | Arch | K3s |
|------|----|------|----|------|-----|
| **Primary Hub** | 192.168.200.145 | K3s server (MASTER), all services | Ubuntu 24.04 | amd64 | Yes |
| **HA Server** | 192.168.200.186 | K3s server (BACKUP), failover | Ubuntu 24.04 | amd64 | Yes |
| **br-aggrigator** | 192.168.200.15 | K3s agent, shared services | Debian 12 (Pi) | arm64 | Yes |
| **Feeder Pi** | 192.168.200.127 | RTL-SDR + readsb + mlat-client | Raspberry Pi OS | arm64 | No |

### Services

| Component | Image | Port | Notes |
|-----------|-------|------|-------|
| **ingest-readsb** | `ghcr.io/bharatradar/readsb` | 30004, 30005 | Receives data from feeders via FRP |
| **hub-readsb** | `ghcr.io/bharatradar/readsb` | 30004 | Aggregates ingest data |
| **planes-readsb** | `ghcr.io/bharatradar/docker-tar1090-uuid` | 80, 30152 | Public tar1090 map with UUID tracking |
| **mlat-mlat-server** | `ghcr.io/bharatradar/mlat-server` | 31090, 30104 | MLAT processing |
| **mlat-map** | `ghcr.io/bharatradar/mlat-server-sync-map` | 80 | MLAT coverage map |
| **reapi-readsb** | `ghcr.io/bharatradar/readsb` | 30152 | REST API data feed (v2 endpoints) |
| **external-readsb** | `ghcr.io/bharatradar/readsb` | 30004 | External feeds (cnvr.io) |
| **api** | `ghcr.io/bharatradar/api` | 8080 | Main web API |
| **history** | `ghcr.io/bharatradar/history` | 8080, 80 | Historical data (amd64 only) |
| **website** | `ghcr.io/bharatradar/website` | 80 | Homepage |

### Shared Services (192.168.200.15)

| Service | Port | Purpose |
|---------|------|---------|
| **PostgreSQL** | 5432 | K3s external datastore |
| **Redis** | 6379 | Cache for API, feeder lookups |
| **InfluxDB** | 8086 | Metrics storage |
| **MinIO** | 9000 (API), 9001 (Console) | S3-compatible storage for history |

---

## Prerequisites

### Hub Node (K3s Server)

- Ubuntu 22.04+ or Debian 12+ (Core i7, VM, or powerful Pi)
- 4GB+ RAM, 50GB+ disk
- Static IP on local network
- Root/sudo access

### Aggregator Node (K3s Agent - Optional)

- Raspberry Pi 4+ with 4GB+ RAM
- Raspberry Pi OS (64-bit) or Debian 12+
- Static IP on local network
- Used for failover - all custom images support arm64

### Feeder Pi (RTL-SDR Receiver)

- Raspberry Pi with RTL-SDR dongle
- Raspberry Pi OS
- readsb + tar1090 + mlat-client installed
- Connects to `feed.bharatradar.com:30004` directly (no FRP client needed)

### AWS/Cloud Server (FRP Server)

- Any cloud VPS (AWS EC2, DigitalOcean, etc.)
- Ubuntu 22.04+ or Debian 12+
- Public IP address
- Ports 7000, 80, 443, 30004, 31090 open in security group

### DNS Setup

- Domain with Cloudflare (or any DNS provider)
- DNS records pointing to FRP server IP

### Docker Build Machine

- Machine with Docker + buildx installed
- GitHub PAT with `packages:write` scope for pushing to GHCR
- Used to build custom multi-arch images

---

## Quick Start

### 1. Build Custom Images

```bash
git clone https://github.com/bharatradar/infra.git
cd infra

# Build and push all images (multi-arch)
for img in readsb docker-tar1090-uuid mlat-server mlat-server-sync-map api; do
  docker buildx build --push --platform linux/amd64,linux/arm64 \
    -t ghcr.io/bharatradar/$img:latest build/$img/
done
```

### 2. Set up Hub Node

```bash
# On Hub (Ubuntu i7):
# Install K3s
curl -sfL https://get.k3s.io | sh -

# Create namespace and secrets
kubectl create ns bharatradar
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GH_USERNAME \
  --docker-password=YOUR_GH_PAT \
  -n bharatradar

# Deploy
cd infra
kustomize build manifests/default | kubectl apply -f -
```

### 3. Set up FRP Server (AWS)

```bash
# On AWS server
sudo scripts/frp/setup-frps.sh \
  --domain bharatradar.com \
  --frp-token "your-secret-token" \
  --email admin@example.com
```

### 4. Set up FRP Client (Hub)

```bash
# On Hub
sudo scripts/frp/setup-frpc.sh \
  --server 13.48.249.103 \
  --token "your-secret-token" \
  --domain bharatradar.com
```

### 5. Join Aggregator Node (Optional)

```bash
# Get join token from Hub
TOKEN=$(sudo cat /var/lib/rancher/k3s/server/node-token)

# On aggregator Pi
curl -sfL https://get.k3s.io | \
  K3S_URL=https://HUB_IP:6443 \
  K3S_TOKEN=$TOKEN \
  sh -
```

---

## Online Install (Non-Interactive)

Run the appropriate one-liner on each machine. The installer will prompt only for required information and automatically resume from failures.

> **Why `--`?** The `--` after `bash -s` is required. It tells bash to stop parsing its own options and pass everything after to the script. Without it, arguments starting with `--` (like `--conf-file`) are rejected as invalid bash options.

```bash
# Step 1: Shared Services (PostgreSQL + Redis + InfluxDB + MinIO)
# Always prompts for primary or standby mode
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services

# Step 2: Primary Hub (first K3s server, creates cluster)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- hub

# Step 2b: Primary Hub with Keepalived VIP (prepares for HA failover)
# Create /tmp/hub.env with KEEPALIVED_ENABLED=true and KEEPALIVED_VIP first
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/hub.env hub

# Step 3a: HA Server (second K3s server, shares control plane)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- ha-server

# Step 3b: Worker Node (K3s agent, runs pods only)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- worker

# DB Standby (PostgreSQL streaming replica for failover)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- db-standby

# Feeder Pi (RTL-SDR receiver, standalone)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-feeder | sudo bash

# FRP Server (Cloud/VPS with public IP)
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- frp-server
```

> **Note:** If the script is interrupted (network error, package failure, etc.), simply re-run the same command. It will detect saved progress and resume from the last completed phase. See [Checkpoint / Resume](#checkpoint--resume) below.

---

## Silent Installation

For fully automated, non-interactive installs, create a configuration file with all required variables and pass it with `--conf-file` (or `-c`).

### 1. Create a Config File

Create a file (e.g., `/tmp/bharatradar.env`) with the variables for your role:

**Example: Hub (basic)**
```bash
cat > /tmp/hub.env << 'EOF'
ROLE=hub
BASE_DOMAIN=bharatradar.com
READSB_LAT=18.480718
READSB_LON=73.898235
TIMEZONE=Asia/Kolkata
REDIS_HOST=192.168.200.15
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password
GHCR_USERNAME=your-github-user
GHCR_PASSWORD=your-github-pat
USE_EXTERNAL_DB=true
DB_HOST=192.168.200.15
DB_PORT=5432
DB_DBNAME=k3s
DB_DBUSER=k3s
DB_DBPASS=your-db-password
MINIO_ENDPOINT=192.168.200.15:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=your-minio-password
FRP_ENABLED=false
KEEPALIVED_ENABLED=false
EOF
```

**Example: Hub with Keepalived VIP (recommended if you plan to add a second server)**
```bash
cat > /tmp/hub.env << 'EOF'
ROLE=hub
BASE_DOMAIN=bharatradar.com
READSB_LAT=18.480718
READSB_LON=73.898235
TIMEZONE=Asia/Kolkata
REDIS_HOST=192.168.200.15
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password
GHCR_USERNAME=your-github-user
GHCR_PASSWORD=your-github-pat
USE_EXTERNAL_DB=true
DB_HOST=192.168.200.15
DB_PORT=5432
DB_DBNAME=k3s
DB_DBUSER=k3s
DB_DBPASS=your-db-password
MINIO_ENDPOINT=192.168.200.15:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=your-minio-password
FRP_ENABLED=false
KEEPALIVED_ENABLED=true
KEEPALIVED_VIP=192.168.200.150
EOF
```

**Example: Worker**
```bash
cat > /tmp/worker.env << 'EOF'
ROLE=worker
HUB_IP=192.168.200.145
K3S_TOKEN=K10xxxxxxxx::server:xxxxxxxx
BASE_DOMAIN=bharatradar.com
EOF
```

### 2. Run the Installer

```bash
# Local
sudo ./bharatradar-install --conf-file /tmp/hub.env hub

# Online
# When piping via curl, write the config to a file first, then reference it
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/hub.env hub
```

### 3. Resume on Failure

If a silent install fails mid-way, re-run the exact same command. The installer will:
- Load saved answers from `/etc/bharatradar/.config.partial`
- Skip phases already marked complete in `/etc/bharatradar/.install-progress`
- Resume from the first pending phase

```bash
# Retry the exact same command — no re-prompting
sudo ./bharatradar-install --conf-file /tmp/hub.env hub
```

### 4. Force Restart from Scratch

```bash
sudo rm -f /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial
sudo ./bharatradar-install --conf-file /tmp/hub.env hub
```

---

## Checkpoint / Resume

All roles now support automatic checkpoint/resume. If the installer is interrupted, re-running it will skip completed phases and continue from where it left off.

### How It Works

- **Config answers** are saved to `/etc/bharatradar/.config.partial`
- **Completed phases** are tracked in `/etc/bharatradar/.install-progress`
- On restart, a resume banner shows which phases are complete (✓) and which are pending (✗)

### Resume Examples

```bash
# Interactive mode — re-run, it resumes automatically
sudo ./bharatradar-install hub

# Non-interactive mode — same command resumes
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- hub

# Silent mode — same file resumes
sudo ./bharatradar-install --conf-file /tmp/hub.env hub
```

### View Saved Progress

```bash
cat /etc/bharatradar/.install-progress
cat /etc/bharatradar/.config.partial
```

### Checkpoint Phases by Role

| Role | Phases |
|------|--------|
| **hub** | `config` → `k3s` → `secrets` → `deploy` → `verify` |
| **shared-services** | `config` → `packages` → `postgresql` → `redis` → `influxdb` → `minio` → `save` |
| **feeder** | `config` → `packages` → `readsb` → `mlat_client` → `configure` → `services` → `start` → `save` |
| **worker** | `config` → `k3s` → `cli` → `save` |
| **ha-server** | `config` → `k3s` → `kubectl` → `cli` → `keepalived` → `save` |
| **db-standby** | `config` → `install` → `replication` → `save` |
| **frp-server** | `config` → `packages` → `binary` → `frp` → `nginx` → `ssl` → `save` |

### Force Restart from Beginning

```bash
sudo rm -f /etc/bharatradar/.install-progress /etc/bharatradar/.config.partial
sudo ./bharatradar-install <role>
```

---

## Adding a Second Hub (HA Server)

### Prerequisites

- Second machine on the same LAN (Ubuntu/Debian, amd64 or arm64)
- Same PostgreSQL external datastore as the Primary Hub
- Keepalived VIP already configured on the Primary Hub (`KEEPALIVED_ENABLED=true`)

### Setup

**1. Get the K3s cluster token from the Primary Hub:**

```bash
ssh user@192.168.200.145 'sudo cat /var/lib/rancher/k3s/server/node-token'
```

**2. Create the config file on the HA Server:**

```bash
cat > /tmp/ha.env << 'EOF'
ROLE=ha-server
BASE_DOMAIN=bharatradar.com
DB_HOST=192.168.200.15
DB_PORT=5432
DB_DBNAME=k3s
DB_DBUSER=k3s
DB_DBPASS=your-db-password
K3S_CLUSTER_TOKEN=K10xxxxxxxx::server:xxxxxxxx
PRIMARY_HUB_IP=192.168.200.145
KEEPALIVED_ENABLED=true
KEEPALIVED_VIP=192.168.200.150
KEEPALIVED_STATE=BACKUP
EOF
```

**3. Run the installer:**

```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/ha.env ha-server
```

### Failover Behavior

| Step | Time | What happens |
|------|------|-------------|
| Primary Hub fails | 0s | Keepalived detects loss of MASTER |
| VIP moves to backup | ~3s | Backup node becomes MASTER, claims `192.168.200.150` |
| kube-proxy routes traffic | ~3s | `externalTrafficPolicy: Cluster` forwards beast/MLAT to pod on dead primary |
| K3s reschedules pods | ~30-60s | ingest-readsb and mlat-mlat-server recreated on backup |
| Feeder Pi reconnects | Auto | readsb and mlat-client auto-reconnect to feed.bharatradar.com |

**Result:**
- Web/API/map traffic: **0s downtime**
- Beast/MLAT data: **~30-60s interruption** (until pods reschedule)

### Load Balancing vs Active-Standby

| Traffic Type | Behavior |
|-------------|----------|
| **HTTP/HTTPS** (map, API, mlat-map, history) | ✅ **Load balanced** — Traefik ingress runs on all nodes, distributes across both hubs |
| **Beast (port 30004)** | ❌ **Active-standby only** — VIP is on ONE node at a time |
| **MLAT (port 31090)** | ❌ **Active-standby only** — VIP is on ONE node at a time |

To get active-active load balancing for beast/MLAT, you would need separate public IPs per hub or a cloud load balancer with health checks.

---

## Future Enhancements

### DaemonSet for Instant Failover (Planned)

**Current limitation:** The ~30-60s pod reschedule window for beast/MLAT during failover.

**Planned improvement:** Convert `ingest-readsb` and `mlat-mlat-server` from **Deployment** to **DaemonSet** (one pod per node).

**Benefits:**
- Instant failover — pod already running on the backup node
- Zero reschedule delay
- Simpler mental model

**Challenges:**
- `hub-readsb` must connect to ALL ingest pods simultaneously (not just one)
- Requires a headless service to discover all node IPs
- `hub-readsb --net-connector` needs to target multiple endpoints

**Implementation approach:**
1. Change `ingest-readsb` from Deployment → DaemonSet
2. Change `mlat-mlat-server` from Deployment → DaemonSet
3. `hub-readsb` connects to `ingest-readsb-headless` (returns all pod IPs via DNS)
4. `hub-readsb` opens a beast connection to each ingest node independently

---

## Silent Configuration Reference

Below are all environment variables accepted by each role for silent installation.

### Shared Services (`shared-services`)

| Variable | Default | Description |
|----------|---------|-------------|
| `SUB_ROLE` | `primary` | `primary` or `replica` (replica redirects to db-standby) |
| `DB_LISTEN_IP` | (detected) | IP address PostgreSQL/Redis should bind to |
| `DB_PORT` | `5432` | PostgreSQL port |

> **Note:** Passwords are auto-generated if not provided: `DB_PASSWORD`, `REDIS_PASSWORD`, `INFLUXDB_ADMIN_TOKEN`, `MINIO_ROOT_PASSWORD`

### Hub (`hub`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `BASE_DOMAIN` | — | Yes | Base domain (e.g., `bharatradar.com`) |
| `READSB_LAT` | `18.480718` | No | Receiver latitude |
| `READSB_LON` | `73.898235` | No | Receiver longitude |
| `TIMEZONE` | `UTC` | No | Timezone (e.g., `Asia/Kolkata`) |
| `REDIS_HOST` | — | Yes | Redis server IP |
| `REDIS_PORT` | `6379` | No | Redis port |
| `REDIS_PASSWORD` | — | Yes | Redis password |
| `GHCR_USERNAME` | — | Yes | GitHub username for GHCR |
| `GHCR_PASSWORD` | — | Yes | GitHub PAT with `read:packages` |
| `USE_EXTERNAL_DB` | `false` | No | `true` = external PostgreSQL, `false` = embedded etcd |
| `DB_HOST` | — | If `USE_EXTERNAL_DB=true` | PostgreSQL host IP |
| `DB_PORT` | `5432` | No | PostgreSQL port |
| `DB_DBNAME` | `k3s` | No | PostgreSQL database name |
| `DB_DBUSER` | `k3s` | No | PostgreSQL username |
| `DB_DBPASS` | — | If `USE_EXTERNAL_DB=true` | PostgreSQL password |
| `MINIO_ENDPOINT` | — | No | MinIO host:port (e.g., `192.168.200.15:9000`) |
| `MINIO_ROOT_USER` | `minioadmin` | No | MinIO access key |
| `MINIO_ROOT_PASSWORD` | — | If using MinIO | MinIO secret key |
| `RCLONE_CONFIG_PATH` | — | No | Path to existing rclone.conf (alternative to MinIO) |
| `FRP_ENABLED` | `false` | No | `true` to enable FRP tunnel |
| `FRP_SERVER` | — | If `FRP_ENABLED=true` | FRP server public IP |
| `FRP_TOKEN` | — | If `FRP_ENABLED=true` | FRP authentication token |
| `KEEPALIVED_ENABLED` | `false` | No | `true` to enable Keepalived VIP |
| `KEEPALIVED_VIP` | — | If `KEEPALIVED_ENABLED=true` | Virtual IP address |

### HA Server (`ha-server`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `BASE_DOMAIN` | — | Yes | Base domain |
| `DB_HOST` | — | Yes | PostgreSQL host IP |
| `DB_PORT` | `5432` | No | PostgreSQL port |
| `DB_DBNAME` | `k3s` | No | PostgreSQL database name |
| `DB_DBUSER` | `k3s` | No | PostgreSQL username |
| `DB_DBPASS` | — | Yes | PostgreSQL password |
| `K3S_CLUSTER_TOKEN` | — | Yes | K3s cluster token from Primary Hub |
| `PRIMARY_HUB_IP` | — | Yes | Primary Hub IP address |
| `KEEPALIVED_ENABLED` | `false` | No | `true` to enable Keepalived |
| `KEEPALIVED_VIP` | — | If enabled | Virtual IP address |
| `KEEPALIVED_STATE` | `BACKUP` | No | `MASTER` or `BACKUP` |

### Worker (`worker`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `HUB_IP` | — | Yes | Primary Hub IP |
| `K3S_TOKEN` | — | Yes | K3s join token |
| `BASE_DOMAIN` | — | Yes | Base domain |

### Feeder (`feeder`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `FEEDER_DOMAIN` | `feed.bharatradar.com` | No | Feeder endpoint domain |
| `READSB_LAT` | `18.480718` | No | Receiver latitude |
| `READSB_LON` | `73.898235` | No | Receiver longitude |
| `FEEDER_ALT_M` | `10` | No | Antenna altitude (meters) |
| `FEEDER_UUID` | (generated) | No | Feeder UUID (auto-generated if empty) |
| `FEEDER_NAME` | (generated) | No | Display name on MLAT map |
| `SDR_SERIAL` | `0` | No | RTL-SDR serial number |
| `MLAT_PRIVACY` | — | No | Set to `--privacy` to hide from map |

### DB Standby (`db-standby`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `PRIMARY_DB_IP` | — | Yes | Primary PostgreSQL server IP |
| `PRIMARY_DB_USER` | `bharatradar` | No | SSH username on primary |
| `PRIMARY_DB_PASSWORD` | — | Yes | Primary DB password |
| `STANDBY_IP` | (detected) | No | This machine's IP |

### FRP Server (`frp-server`)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `BASE_DOMAIN` | — | Yes | Base domain |
| `EMAIL` | — | Yes | Let's Encrypt email |
| `FRP_AUTH_TOKEN` | (generated) | No | FRP auth token (auto-generated) |
| `FRPS_DASHBOARD_PASS` | (generated) | No | Dashboard password (auto-generated) |

---

## Step-by-Step Installation

### Step 1: Shared Services (br-aggrigator Pi — 192.168.200.15)

This installs PostgreSQL, Redis, InfluxDB, and MinIO. These are prerequisites for the K3s cluster.

```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services
```

After installation, save the credentials shown at the end. You'll need:
- **PostgreSQL connection string** (for K3s `--datastore-endpoint`)
- **Redis host and password** (for API, planes, mlat-map)
- **MinIO endpoint and credentials** (for history service rclone config)

> **Note:** This must be done BEFORE installing the Primary Hub. The Hub needs the PostgreSQL connection string to use as its external datastore.

For manual database reset or troubleshooting, see [scripts/db/README.md](scripts/db/README.md).

#### Schedule Downloader

After the database is initialized, the schedule-downloader CronJob will be deployed automatically when you install the Hub. By default:

- **Scheduled runs** are `DISABLED` (`scheduler_enabled = FALSE`)
- **Manual runs** are `ENABLED` (`enabled = TRUE`)

**To run manually:**
```bash
/opt/bharatradar/scripts/trigger-downloader.sh
# or
sudo kubectl create job manual-run --from=cronjob/schedule-downloader -n bharatradar
```

**To enable automatic scheduled runs (daily at 22:00 UTC):**
```bash
# Connect to PostgreSQL
psql -h 192.168.200.15 -U flight_db_user -d flight_db

# Enable scheduler
UPDATE download_config SET scheduler_enabled = TRUE, updated_at = NOW() WHERE id = 1;
```

**To check status:**
```bash
sudo kubectl get jobs -n bharatradar -l app=schedule-downloader
sudo kubectl logs -n bharatradar job/schedule-downloader-manual -f
```

### Step 2: DNS Setup

Add the following A records to your DNS provider (Cloudflare recommended).
All records point to your AWS EC2 (FRP server) public IP.

| Type | Name | Value | Cloudflare Proxy | Purpose |
|------|------|-------|------------------|---------|
| `A` | `bharatradar.com` | `<AWS_IP>` | **Proxied** | Homepage |
| `A` | `map.bharatradar.com` | `<AWS_IP>` | **Proxied** | Live map |
| `A` | `my.bharatradar.com` | `<AWS_IP>` | **Proxied** | Feeder map |
| `A` | `mlat.bharatradar.com` | `<AWS_IP>` | **Proxied** | MLAT map |
| `A` | `history.bharatradar.com` | `<AWS_IP>` | **Proxied** | History |
| `A` | `api.bharatradar.com` | `<AWS_IP>` | **Proxied** | REST API |
| `A` | `feed.bharatradar.com` | `<AWS_IP>` | **DNS only** | Feeder TCP endpoint |
| `A` | `ws.bharatradar.com` | `<AWS_IP>` | **Proxied** | WebSocket (future) |

> **Critical:** `feed.bharatradar.com` must be **DNS only** (grey cloud in Cloudflare).
> Cloudflare proxy only supports HTTP/HTTPS — raw TCP connections for ADS-B beast feeds
> will fail if proxied.

> **Tip:** Use a wildcard record `*.bharatradar.com` → `<AWS_IP>` (DNS only) as a catch-all,
> then add explicit proxied records for the web subdomains above.

### Step 2: Build and Push Custom Images

All images built from forked source repos via centralized CI in `bharatradar/infra`.
Fork repos hold source code only — no CI workflows.

### Source Forks (built by infra CI)

| Fork | Upstream | Branch | Image | Platforms |
|------|----------|--------|-------|-----------|
| [bharatradar/readsb](https://github.com/bharatradar/readsb) | wiedehopf/readsb | `dev` | `ghcr.io/bharatradar/readsb` | amd64, arm64 |
| [bharatradar/docker-tar1090](https://github.com/bharatradar/docker-tar1090) | sdr-enthusiasts/docker-tar1090 | `main` | `ghcr.io/bharatradar/docker-tar1090` | amd64, arm64 |
| [bharatradar/mlat-server](https://github.com/bharatradar/mlat-server) | adsblol/mlat-server | `master` | `ghcr.io/bharatradar/mlat-server` | amd64 |
| [bharatradar/mlat-server-sync-map](https://github.com/bharatradar/mlat-server-sync-map) | adsblol/mlat-server-sync-map | `master` | `ghcr.io/bharatradar/mlat-server-sync-map` | amd64 |
| [bharatradar/api](https://github.com/bharatradar/api) | adsblol/api | `main` | `ghcr.io/bharatradar/api` | amd64 |
| [bharatradar/history](https://github.com/bharatradar/history) | adsblol/history | `main` | `ghcr.io/bharatradar/history` | amd64 |
| [bharatradar/website](https://github.com/bharatradar/website) | adsblol/website | `main` | `ghcr.io/bharatradar/website` | amd64, arm64 |

### Wrapper Images (built by infra CI)

| Image | Base | Build Dir |
|-------|------|-----------|
| `ghcr.io/bharatradar/docker-tar1090-uuid` | `docker-tar1090` fork + uuid binaries from `readsb` fork | `build/docker-tar1090-uuid/` |
| `ghcr.io/bharatradar/mlat-server-sync-map` | fork image + nginx proxy | `build/mlat-server-sync-map/` |
| `ghcr.io/bharatradar/api` | fork image + patch.py | `build/api/` |

```bash
# Build all images (triggers from infra repo)
# Go to Actions → "Build All Images" → Run workflow

# Build a single image
# workflow_dispatch with image=readsb
```

> **Note:** `history` image is amd64 only (upstream doesn't provide arm64). The manifest includes `nodeSelector: kubernetes.io/arch: amd64` to ensure it only schedules on amd64 nodes.

### Step 3: Set up Hub Node (K3s Server)

```bash
# Install K3s
curl -sfL https://get.k3s.io | sh -

# Verify
sudo kubectl get nodes
sudo kubectl get pods -A
```

### Step 4: Create Namespace and Secrets

```bash
# Create namespace
sudo kubectl create ns bharatradar

# Create GHCR pull secret
sudo kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GH_USERNAME \
  --docker-password=YOUR_GH_PAT \
  -n bharatradar

# Create TLS secret (copy from AWS EC2 after certbot)
sudo kubectl create secret tls bharatradar-tls \
  --cert=/etc/letsencrypt/live/bharatradar.com/fullchain.pem \
  --key=/etc/letsencrypt/live/bharatradar.com/privkey.pem \
  -n bharatradar

# Create rclone secret for history service (MinIO)
echo -e "[bharatradar]\ntype = s3\nprovider = MinIO\naccess_key_id = minioadmin\nsecret_access_key = <from-shared-services>\nendpoint = http://192.168.200.15:9000\nacl = private" | \
  sudo kubectl create secret generic bharatradar-rclone \
  --from-file=rclone.conf=/dev/stdin -n bharatradar
```

### Step 5: Deploy Infrastructure

```bash
cd /path/to/infra

# Preview manifests
kustomize build manifests/default

# Apply
kustomize build manifests/default | sudo kubectl apply -f -

# Verify
sudo kubectl get pods -n bharatradar
```

### Step 6: Set up FRP Server (AWS)

See [FRP Server Setup](#frp-server-setup-awscloud) below.

### Step 7: Set up FRP Client (Hub)

See [FRP Client Setup](#frp-client-setup-hub-node) below.

### Step 8: Join Aggregator Node (br-aggrigator Pi as K3s agent)

```bash
# On Hub - get join token
sudo cat /var/lib/rancher/k3s/server/node-token

# On br-aggrigator Pi (192.168.200.15)
curl -sfL https://get.k3s.io | \
  K3S_URL=https://192.168.200.145:6443 \
  K3S_TOKEN=K10xxxxxxxxx \
  sh -

# Verify on Hub
sudo kubectl get nodes -o wide
```

### Step 9: Set up Feeder Pi

See [Feeder Pi Setup](#feeder-pi-setup) below.

---

## Custom Docker Images

### Building

All custom images are defined in the `build/` directory. Each contains a Dockerfile that pulls the upstream image and optionally adds customizations.

```bash
# Single arch (faster)
docker buildx build --push -t ghcr.io/bharatradar/readsb:latest build/readsb/

# Multi-arch (for Pi compatibility)
docker buildx build --push --platform linux/amd64,linux/arm64 \
  -t ghcr.io/bharatradar/readsb:latest build/readsb/
```

### Image Matrix

| Fork | Upstream | CI | Archs |
|------|----------|----|-------|
| [bharatradar/readsb](https://github.com/bharatradar/readsb) | wiedehopf/readsb | Fork workflow | amd64, arm64 |
| [bharatradar/docker-tar1090](https://github.com/bharatradar/docker-tar1090) | sdr-enthusiasts/docker-tar1090 | Fork workflow | amd64, arm64 |
| [bharatradar/mlat-server](https://github.com/bharatradar/mlat-server) | adsblol/mlat-server | Fork workflow | amd64 |
| [bharatradar/mlat-server-sync-map](https://github.com/bharatradar/mlat-server-sync-map) | adsblol/mlat-server-sync-map | Fork workflow + infra wrapper | amd64 |
| [bharatradar/api](https://github.com/bharatradar/api) | adsblol/api | Fork workflow + infra wrapper (patch.py) | amd64 |
| [bharatradar/history](https://github.com/bharatradar/history) | adsblol/history | Fork workflow | amd64 |

### Wrapper Images (built by infra CI)

| Image | Base | Customizations | Archs |
|-------|------|----------------|-------|
| `docker-tar1090-uuid` | `bharatradar/docker-tar1090` + `bharatradar/readsb` | Adds `readsb-with-uuids` binary, enables `rId` in aircraft.json | amd64, arm64 |
| `mlat-server-sync-map` | `bharatradar/mlat-server-sync-map` | Adds redirect index.html, nginx reverse proxy for `/api/0/mlat-server/` | amd64 |
| `api` | `bharatradar/api` | Patches MY_DOMAIN, v2 routes, Redis integration | amd64 |

### CI/CD

All images are built from a single workflow: `.github/workflows/build-image.yml`.

- **Stage 1**: Checks out fork repos and builds base images (readsb, docker-tar1090, mlat-server, etc.)
- **Stage 2**: Builds wrapper images that add customizations (docker-tar1090-uuid, mlat-server-sync-map, api)

Trigger: push to `main` or manual dispatch with `image=<name>` to build a specific image.

---

## FRP Server Setup (AWS/Cloud)

The FRP server runs on your cloud machine and tunnels traffic to the Hub's Kubernetes services.

### Prerequisites

- Ubuntu/Debian cloud server
- Public IP
- DNS records pointing to this IP
- Ports open: 7000 (FRP control), 80/443 (HTTP/HTTPS), 30004 (beast input), 31090 (MLAT)

### Automated Setup

```bash
git clone https://github.com/bharatradar/infra.git
cd infra

sudo scripts/frp/setup-frps.sh \
  --domain bharatradar.com \
  --frp-token "your-secret-token" \
  --email admin@example.com
```

### Manual Setup

```bash
# 1. Download frps
FRP_VERSION="0.68.1"
ARCH=$(uname -m)
curl -L "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz" | tar xz
sudo cp "frp_${FRP_VERSION}_linux_${ARCH}/frps" /usr/local/bin/

# 2. Configure frps
sudo tee /etc/frps.toml << 'EOF'
bindPort = 7000
vhostHTTPPort = 8080
vhostHTTPSPort = 8443

auth.method = "token"
auth.token = "YOUR_SECRET_TOKEN"
EOF

# 3. Create systemd service
sudo tee /etc/systemd/system/frps.service << 'EOF'
[Unit]
Description=FRP Server (BharatRadar)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frps.toml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable frps
sudo systemctl start frps
```

### FRP Server Proxies

The AWS FRP server exposes these ports:

| Proxy Name | Type | Remote Port | Forwards To |
|-----------|------|-------------|-------------|
| `feed-beast-30004` | TCP | 30004 | Hub ingest-readsb:30004 |
| `feed-mlat-31090` | TCP | 31090 | Hub mlat-mlat-server:31090 |
| `web-http` | HTTP | 80 | Hub Traefik (port 80) |
| `web-https` | HTTPS | 443 | Hub Traefik (port 443) |

---

## FRP Client Setup (Hub Node)

The FRP client runs on the Hub and creates tunnels for web traffic and feeder data.

### Automated Setup

```bash
cd infra

sudo scripts/frp/setup-frpc.sh \
  --server 13.48.249.103 \
  --token "your-secret-token" \
  --domain bharatradar.com
```

### Manual Setup

```bash
# 1. Download frpc
FRP_VERSION="0.68.1"
ARCH=$(uname -m)
curl -L "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz" | tar xz
sudo cp "frp_${FRP_VERSION}_linux_${ARCH}/frpc" /usr/local/bin/

# 2. Configure frpc (update IPs to match your cluster)
sudo tee /etc/frpc.toml << 'EOF'
serverAddr = "13.48.249.103"
serverPort = 7000
log.level = "info"

auth.method = "token"
auth.token = "YOUR_SECRET_TOKEN"

[[proxies]]
name = "feed-beast-30004"
type = "tcp"
localIP = "INGEST_READSB_CLUSTER_IP"
localPort = 30004
remotePort = 30004

[[proxies]]
name = "feed-beast-30005"
type = "tcp"
localIP = "INGEST_READSB_CLUSTER_IP"
localPort = 30005
remotePort = 30005

[[proxies]]
name = "feed-mlat-31090"
type = "tcp"
localIP = "MLAT_SERVER_CLUSTER_IP"
localPort = 31090
remotePort = 31090

[[proxies]]
name = "web-http"
type = "http"
localIP = "TRAEFIK_LB_IP"
localPort = 80
customDomains = ["map.bharatradar.com", "bharatradar.com"]

[[proxies]]
name = "web-https"
type = "https"
localIP = "TRAEFIK_LB_IP"
localPort = 443
customDomains = ["map.bharatradar.com", "bharatradar.com"]

[[proxies]]
name = "mlat-map"
type = "http"
localIP = "MLAT_MAP_CLUSTER_IP"
localPort = 80
customDomains = ["mlat.bharatradar.com"]
EOF

# Get cluster IPs:
# sudo kubectl get svc -n bharatradar

# 3. Create systemd service
sudo tee /etc/systemd/system/frpc.service << 'EOF'
[Unit]
Description=FRP Client (BharatRadar Hub)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c /etc/frpc.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable frpc
sudo systemctl start frpc

# 4. Verify
sudo systemctl status frpc
journalctl -u frpc -f
```

### FRP Client Proxies

| Proxy Name | Local Target | Remote | Purpose |
|-----------|-------------|--------|---------|
| `feed-beast-30004` | ingest-readsb:30004 | AWS:30004 | Feeder data input |
| `feed-beast-30005` | ingest-readsb:30005 | AWS:30005 | Feeder data output |
| `feed-mlat-31090` | mlat-mlat-server:31090 | AWS:31090 | MLAT client input |
| `web-http` | Traefik LB:80 | AWS:80 (HTTP) | Map/API HTTP |
| `web-https` | Traefik LB:443 | AWS:443 (HTTPS) | Map/API HTTPS |
| `mlat-map` | mlat-map:80 | AWS (HTTP vhost) | MLAT coverage map |

---

## Feeder Pi Setup

The Feeder Pi runs readsb + mlat-client and sends data to the FRP server. It is **not** part of the Kubernetes cluster.

### Prerequisites

- Raspberry Pi with RTL-SDR dongle
- Raspberry Pi OS installed
- readsb + tar1090 already installed (e.g., via Wiedehopf's scripts)

### readsb Configuration

Edit `/etc/default/readsb`:

```bash
RECEIVER_OPTIONS="--device 0 --device-type rtlsdr --gain auto --ppm 0 --dcfilter --enable-biastee --uuid-file /etc/bharat-radar-id --preamble-threshold 58"
DECODER_OPTIONS="--lat 18.480718 --lon 73.898235 --max-range 450 --fix"
NET_OPTIONS="--net --net-ingest --net-receiver-id --net-ri-port 30001 --net-ro-port 30002 --net-sbs-port 30003 --net-bi-port 30004,30104,31008,30105 --net-bo-port 30005 --net-beast-reduce-interval 0.5"
JSON_OPTIONS="--json-location-accuracy 2"
ADAPTIVE_DYNAMIC_RANGE=yes
```

Restart readsb:

```bash
sudo systemctl restart readsb
```

### MLAT Feeder Bridge

This sends beast output from local readsb to the FRP server:

```bash
sudo tee /etc/systemd/system/bharat-feeder.service << 'EOF'
[Unit]
Description=Bharat Radar Beast Feeder Bridge
After=network.target readsb.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/readsb --net-only --uuid-file /etc/bharat-radar-id \
  --net-connector 127.0.0.1,30005,beast_in \
  --net-connector feed.bharatradar.com,30004,beast_reduce_plus_out
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bharat-feeder
sudo systemctl start bharat-feeder
```

### MLAT Client

```bash
sudo tee /etc/systemd/system/bharat-mlat.service << 'EOF'
[Unit]
Description=Bharat Radar MLAT Client
After=network.target readsb.service

[Service]
Type=simple
ExecStart=/usr/bin/mlat-client \
  --input-type beast \
  --input-connect 127.0.0.1:30005 \
  --server feed.bharatradar.com:31090 \
  --user BR-YOUR_UUID_HERE \
  --lat 18.480718 \
  --lon 73.898235 \
  --alt 630 \
  --results beast,connect,127.0.0.1:30105
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bharat-mlat
sudo systemctl start bharat-mlat
```

### Verify Feeder

```bash
# Check readsb is receiving
sudo journalctl -u readsb --since "1 min ago" | grep -i "hex:"

# Check feeder is connected
sudo journalctl -u bharat-feeder --since "1 min ago"

# Check MLAT client
sudo journalctl -u bharat-mlat --since "1 min ago"
sudo systemctl status bharat-feeder bharat-mlat
```

### Disk Maintenance

The feeder installer sets up automated daily disk cleanup at 3am. To run manually:

```bash
sudo bharatradar-cleanup
```

---

## SSL/TLS Configuration

### Let's Encrypt with Nginx (on FRP Server)

The `setup-frps.sh` script handles SSL automatically. For manual setup:

```bash
sudo scripts/install/setup-nginx-ssl.sh \
  --domain bharatradar.com \
  --email admin@example.com
```

### Cloudflare Edge Certificates

If using Cloudflare proxy (orange cloud):
1. **Flexible SSL** - Cloudflare handles HTTPS, connects via HTTP
2. **Full SSL** - Requires valid certificate on origin server
3. **DNS-only mode** - Recommended with origin SSL for immediate coverage

> **Note:** On free Cloudflare plans, use DNS-only mode. The FRP server handles SSL via Let's Encrypt.

### Certificate Auto-Renewal

```bash
sudo systemctl status certbot.timer
sudo certbot renew --dry-run
sudo certbot certificates
```

---

## Cluster Management

### Deploy Infrastructure

```bash
cd infra
kustomize build manifests/default | kubectl apply -f -
```

### Join an Aggregator Node

```bash
# Get token from Hub
sudo cat /var/lib/rancher/k3s/server/node-token

# On aggregator Pi
curl -sfL https://get.k3s.io | \
  K3S_URL=https://HUB_IP:6443 \
  K3S_TOKEN=$TOKEN \
  sh -
```

### View Cluster Info

```bash
kubectl get nodes -o wide
kubectl get pods -n bharatradar -o wide
kubectl top nodes
kubectl top pods -n bharatradar
```

### Failover Test

```bash
# Cordon Hub to force pods onto aggregator
kubectl cordon bharat-radar

# Delete pods to trigger rescheduling
kubectl delete pod -n bharatradar -l app=ingest
kubectl delete pod -n bharatradar -l app=hub

# Verify pods are on aggregator
kubectl get pods -n bharatradar -o wide

# Uncordon Hub when done
kubectl uncordon bharat-radar
```

### Update Images

```bash
# Pull latest images on all nodes
sudo k3s crictl pull ghcr.io/bharatradar/readsb:latest

# Restart deployments
kubectl rollout restart deployment -n bharatradar
```

---

## Configuration Reference

### Configuration Files

| File | Location | Description |
|------|----------|-------------|
| K3s config | `/etc/rancher/k3s/k3s.yaml` | Kubernetes admin config |
| K3s server token | `/var/lib/rancher/k3s/server/node-token` | Token for joining nodes |
| FRP server | `/etc/frps.toml` | FRP server configuration (AWS) |
| FRP client | `/etc/frpc.toml` | FRP client configuration (Hub) |
| Platform config | `/etc/bharatradar/config.env` | Generated platform config |

### Kubernetes Namespace

All resources are in the `bharatradar` namespace.

### imagePullSecrets

All deployments that pull from GHCR include:

```yaml
imagePullSecrets:
- name: ghcr-secret
```

### Node Selectors

| Deployment | Selector | Reason |
|-----------|----------|--------|
| `history` | `kubernetes.io/arch: amd64` | Upstream image is amd64 only |

### Environment Variables

| Variable | Location | Description |
|----------|----------|-------------|
| `BASE_DOMAIN` | `/etc/bharatradar/config.env` | `bharatradar.com` |
| `READSB_LAT` | `/etc/bharatradar/config.env` | Receiver latitude |
| `READSB_LON` | `/etc/bharatradar/config.env` | Receiver longitude |
| `TIMEZONE` | `/etc/bharatradar/config.env` | `Asia/Kolkata` |

### Subdomains

| Subdomain | Service | Description |
|-----------|---------|-------------|
| `map.*` | planes-readsb | tar1090 map interface |
| `mlat.*` | mlat-map | MLAT coverage visualization |
| `history.*` | history | Historical flight data |
| `api.*` / `*` (root) | api | Main web API |
| `feed.*` | AWS FRP server | Feeder endpoint (ports 30004, 31090) |

---

## Troubleshooting

### K3s Issues

```bash
# Check K3s status
sudo systemctl status k3s

# View K3s logs
sudo journalctl -u k3s -f

# Reset K3s (WARNING: destroys cluster)
sudo k3s-uninstall.sh
```

### FRP Issues

```bash
# Check FRP client (on Hub)
sudo systemctl status frpc
journalctl -u frpc -f

# Check FRP server (on AWS)
sudo systemctl status frps
journalctl -u frps -f

# Test connectivity from Feeder Pi
nc -zv feed.bharatradar.com 30004
nc -zv feed.bharatradar.com 31090
```

### Pod Issues

```bash
# View all pods
kubectl get pods -n bharatradar

# View pod logs
kubectl logs -n bharatradar <pod-name>

# Describe pod for events
kubectl describe pod -n bharatradar <pod-name>

# Restart a deployment
kubectl rollout restart deployment/<name> -n bharatradar
```

### Common Problems

| Problem | Solution |
|---------|----------|
| `ImagePullBackOff` on GHCR images | Check `ghcr-secret` exists and has valid PAT: `kubectl get secret ghcr-secret -n bharatradar` |
| `readsb` crash loop | Ensure `READSB_DEVICE_TYPE=none` is set for network-only mode |
| `mlat-server` crash | Python 3.13 compatibility - pkg_resources stub is installed via initContainer |
| SSL handshake failure | Check certificate covers the subdomain |
| FRP `vhost https port is not set` | Add `vhostHTTPSPort = 8443` to `/etc/frps.toml` |
| nginx 502 Bad Gateway | Check frps is running and vhost ports are configured |
| No aircraft on map | Check feeder is connected: check `readsb` logs on Pi for hex codes |
| MLAT map stuck on "Loading regions..." | Check nginx proxy in mlat-server-sync-map: `curl https://mlat.bharatradar.com/api/0/mlat-server/0A/sync.json` |
| `peers: {}` on MLAT map | Normal with single feeder - requires multiple receivers for peer sync |

### FRP Connection Troubleshooting

```bash
# On Feeder Pi: Check connectivity to FRP server
nc -zv feed.bharatradar.com 30004
nc -zv feed.bharatradar.com 31090

# On Feeder Pi: Check readsb is producing data
sudo journalctl -u readsb --since "1 min ago" | grep "hex:"

# On Feeder Pi: Check bharat-feeder is connecting
sudo journalctl -u bharat-feeder --since "1 min ago"

# On Hub: Check frpc can reach FRP server
nc -zv 13.48.249.103 7000

# On Hub: Check frpc logs
journalctl -u frpc --since "5 minutes ago"

# On AWS: Check frps logs
journalctl -u frps --since "5 minutes ago"

# On AWS: Check if frps is listening
sudo ss -tlnp | grep 7000
sudo ss -tlnp | grep 8080
```

---

## Useful Commands

### K3s / Kubernetes

```bash
# kubectl is available via symlink
sudo k3s kubectl get pods -n bharatradar
# or
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get pods -n bharatradar

# Common commands
kubectl get pods -n bharatradar                    # List pods
kubectl get svc -n bharatradar                     # List services
kubectl get ingress -n bharatradar                 # List ingresses
kubectl get nodes -o wide                          # List nodes with IPs
kubectl logs -n bharatradar -l app=planes          # View plane logs
kubectl describe pod <name> -n bharatradar         # Pod details
kubectl exec -it <pod> -n bharatradar -- bash      # Shell into pod
kubectl top nodes                                  # Node resource usage
kubectl top pods -n bharatradar                    # Pod resource usage
kubectl cordon <node>                              # Prevent scheduling
kubectl uncordon <node>                            # Allow scheduling
kubectl drain <node> --ignore-daemonsets           # Evict all pods
```

### Building Images

```bash
# Build single image (amd64 only)
docker buildx build --push -t ghcr.io/bharatradar/readsb:latest build/readsb/

# Build multi-arch
docker buildx build --push --platform linux/amd64,linux/arm64 \
  -t ghcr.io/bharatradar/readsb:latest build/readsb/

# Build all images
for img in readsb docker-tar1090-uuid mlat-server mlat-server-sync-map api; do
  docker buildx build --push --platform linux/amd64,linux/arm64 \
    -t ghcr.io/bharatradar/$img:latest build/$img/
done
```

### FRP

```bash
sudo systemctl {start|stop|restart|status} frpc   # FRP client (Hub)
sudo systemctl {start|stop|restart|status} frps   # FRP server (AWS)
journalctl -u frpc -f                              # FRP client logs
journalctl -u frps -f                              # FRP server logs
cat /etc/frpc.toml                                 # View frpc config
cat /etc/frps.toml                                 # View frps config
```

### Nginx (on FRP Server)

```bash
sudo systemctl {start|stop|restart|reload|status} nginx
sudo nginx -t                    # Test config
sudo nginx -T                    # Show full config
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### SSL

```bash
sudo certbot certificates         # List certificates
sudo certbot renew --dry-run      # Test renewal
sudo ls -la /etc/letsencrypt/live/
```

### Feeder Pi

```bash
sudo systemctl status readsb              # ADS-B receiver
sudo systemctl status bharat-feeder       # Beast feeder bridge
sudo systemctl status bharat-mlat         # MLAT client
sudo journalctl -u readsb -f              # Live readsb logs
sudo journalctl -u bharat-feeder -f       # Live feeder logs
sudo journalctl -u bharat-mlat -f         # Live MLAT logs
cat /etc/default/readsb                   # readsb configuration
cat /etc/bharat-radar-id                  # Feeder UUID

# Disk cleanup
sudo bharatradar-cleanup                  # Manual cleanup
sudo systemctl status bharatradar-cleanup.timer  # Timer status
```

---

## Updating

```bash
# Update repository
cd infra && git pull origin main

# Rebuild and push images (if Dockerfiles changed)
for img in readsb docker-tar1090-uuid mlat-server mlat-server-sync-map api; do
  docker buildx build --push --platform linux/amd64,linux/arm64 \
    -t ghcr.io/bharatradar/$img:latest build/$img/
done

# Reapply manifests
kustomize build manifests/default | kubectl apply -f -

# Restart deployments to pull new images
kubectl rollout restart deployment -n bharatradar
```

---

## Support

- **GitHub:** https://github.com/bharatradar/infra
- **Issues:** https://github.com/bharatradar/infra/issues
- **Original Project:** https://github.com/adsblol/infra
