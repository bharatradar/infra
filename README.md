# bharatradar/infra

BharatRadar ADS-B/MLAT aggregator platform. Aggregates [ADS-B](https://github.com/wiedehopf/readsb) & [MLAT](https://github.com/wiedehopf/mlat-server) data from multiple feeders and serves a public map interface.

> **Version:** 6.0.0
> **GitHub:** https://github.com/bharatradar/infra

## Why?

Community-driven ADS-B aggregation, built as a fork of [adsblol/infra](https://github.com/adsblol/infra). All upstream images are mirrored to `ghcr.io/bharatradar/` with multi-arch support (amd64 + arm64) for Raspberry Pi compatibility.

## Quick Start: Set Up a Feeder

If you just want to feed data to BharatRadar (no server/cluster setup needed), run this on your Raspberry Pi:

```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-feeder | sudo bash
```

This auto-detects your SDR, installs readsb + mlat-client, and connects to `feed.bharatradar.com`. Setup takes under 15 minutes. [Full docs →](https://bharatradar.com/docs/get-started/become-a-feeder/)

## Architecture

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
│       192.168.200.10 (MASTER)               │
│       VIP: 192.168.200.150                  │
         │                                             │
         │  ingest  hub  planes  api  mlat  mlat-map   │
         │  telegram-bot  flight-tracker  schedule-downloader
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

### Data Flow

```
Feeder Pi (readsb + mlat-client)
    |
    ├── beast_reduce_plus_out ──→ feed.bharatradar.com:30004 (AWS FRP server)
    │                                                          |
    │                                                          └── TCP tunnel ──→ Hub ingest-readsb:30004
    |
    └── mlat-client ──→ feed.bharatradar.com:31090 (AWS FRP server)
                                                         |
                                                         └── TCP tunnel ──→ Hub mlat-mlat-server:31090

Hub Cluster:
  user ──→ Cloudflare ──→ AWS nginx ──→ FRP tunnel ──→ Traefik (K3s) ──→ Service pods
```

### Nodes

| Node | IP | OS | Role | Arch | K3s |
|------|----|----|------|------|-----|
| **Primary Hub** | 192.168.200.10 | Ubuntu 24.04 (Core i7) | K3s server, MASTER keepalived | amd64 | Yes |
| **HA Server** | 192.168.200.186 | Ubuntu 24.04 (Core i5) | K3s server, BACKUP keepalived | amd64 | Yes |
| **br-aggrigator** | 192.168.200.15 | Debian 12 (Raspberry Pi) | K3s agent, shared services | arm64 | Yes |
| **Feeder Pi** | 192.168.200.127 | Raspberry Pi OS | RTL-SDR + readsb + mlat-client (not K3s) | arm64 | No |

### Services

| Component | Image | Namespace | Ports | Notes |
|-----------|-------|-----------|-------|-------|
| **ingest-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004, 30005 | Receives feeder data via FRP (LoadBalancer) |
| **hub-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004, 30005 | Aggregates ingest data |
| **planes-readsb** | `ghcr.io/bharatradar/docker-tar1090-uuid` | bharatradar | 80, 30152 | Public tar1090 map with UUID tracking |
| **mlat-mlat-server** | `ghcr.io/bharatradar/mlat-server` | bharatradar | 31090, 30104, 150 | MLAT processing |
| **mlat-map** | `ghcr.io/bharatradar/mlat-server-sync-map` | bharatradar | 80 | MLAT coverage map (nginx proxies API) |
| **reapi-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30152, 80 | REST API data feed (v2 endpoints) |
| **external-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004 | External feeds (cnvr.io) |
| **api** | `ghcr.io/bharatradar/api` | bharatradar | 8080, 80 | Main web API (patched for MY_DOMAIN) |
| **history** | `ghcr.io/bharatradar/history` | bharatradar | 8080, 80 | Historical data (amd64 only) |
| **website** | `ghcr.io/bharatradar/website` | bharatradar | 80 | Homepage |

## Cluster Credentials

> **Warning:** Credentials are stored locally on the Hub node at `/root/bharatradar-secrets.yaml`. Do not commit secrets to GitHub — push protection will block them.

### Required Secrets

| Secret | Keys Required |
|--------|--------------|
| `ghcr-secret` | docker-username, docker-password (GitHub PAT) |
| `flight-db-credentials` | password |
| `redis-credentials` | password |
| `telegram-bot-credentials` | telegram_token, telegram_chat_id, groq_api_key, cloudflare_keys |
| `influxdb-credentials` | token |
| `bharatradar-rclone` | rclone.conf (optional, for history cloud sync) |

### Create Secrets on K3s

```bash
# Example: create all secrets after namespace deletion/recovery
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io --docker-username=<GH_USER> \
  --docker-password=<GH_PAT> --docker-email=<EMAIL> -n bharatradar

kubectl create secret generic flight-db-credentials \
  --from-literal=password=<DB_PASSWORD> -n bharatradar

kubectl create secret generic redis-credentials \
  --from-literal=password=<REDIS_PASSWORD> -n bharatradar

kubectl create secret generic telegram-bot-credentials \
  --from-literal=telegram_token=<TOKEN> \
  --from-literal=telegram_chat_id=<CHAT_ID> \
  --from-literal=groq_api_key=<GROQ_KEY> \
  --from-literal=cloudflare_keys=<JSON> -n bharatradar

kubectl create secret generic influxdb-credentials \
  --from-literal=token=<INFLUX_TOKEN> -n bharatradar
```

### Shared Services (192.168.200.15)

| Service | Port | Purpose |
|---------|------|---------|
| **PostgreSQL** | 5432 | K3s external datastore |
| **Redis** | 6379 | Cache for API, feeder lookups |
| **InfluxDB** | 8086 | Metrics storage |
| **MinIO** | 9000 (API), 9001 (Console) | S3-compatible storage for history |

### Schedule Downloader

K3s CronJob that downloads flight schedules from FlightRadar24 and stores in PostgreSQL.

| Component | Image | Schedule | Trigger |
|-----------|-------|---------|---------|
| **schedule-downloader** | `ghcr.io/bharatradar/schedule-downloader` | Daily 22:00 UTC | Manual via CLI |

#### Configuration

Schedule time and enabled status are stored in `download_config` table:

```sql
-- View current config
SELECT * FROM download_config;

-- Update schedule time (HH:MM:SS)
UPDATE download_config SET schedule_time = '22:00:00', updated_at = NOW() WHERE id = 1;

-- Enable/Disable
UPDATE download_config SET enabled = TRUE/FALSE, updated_at = NOW() WHERE id = 1;
```

#### Manual Trigger

```bash
# Trigger manually
./scripts/triggers/trigger-downloader.sh

# Check status
kubectl get jobs -n bharatradar -l app=schedule-downloader
kubectl logs -n bharatradar job/schedule-downloader-manual

# Delete after completion
kubectl delete job schedule-downloader-manual -n bharatradar
```

#### Files

| Path | Description |
|------|-------------|
| `scripts/db/downloader/` | Dockerfile, requirements, source code |
| `scripts/triggers/trigger-downloader.sh` | Manual trigger script |
| `manifests/default/schedule-downloader-cronjob.yaml` | K3s CronJob manifest |

> **Note:** The CronJob reads `schedule_time` from the database - update there to change schedule without redeploying.

### Custom Images

All images built from forked source repos via centralized CI in `bharatradar/infra`.
Fork repos hold source code only — no CI workflows.

#### Source Forks (built by infra CI)
| Fork | Upstream | Branch | Image | Platforms |
|------|----------|--------|-------|-----------|
| [bharatradar/readsb](https://github.com/bharatradar/readsb) | wiedehopf/readsb | `dev` | `ghcr.io/bharatradar/readsb` | amd64, arm64 |
| [bharatradar/docker-tar1090](https://github.com/bharatradar/docker-tar1090) | sdr-enthusiasts/docker-tar1090 | `main` | `ghcr.io/bharatradar/docker-tar1090` | amd64, arm64 |
| [bharatradar/mlat-server](https://github.com/bharatradar/mlat-server) | adsblol/mlat-server | `master` | `ghcr.io/bharatradar/mlat-server` | amd64 |
| [bharatradar/mlat-server-sync-map](https://github.com/bharatradar/mlat-server-sync-map) | adsblol/mlat-server-sync-map | `master` | `ghcr.io/bharatradar/mlat-server-sync-map` | amd64 |
| [bharatradar/api](https://github.com/bharatradar/api) | adsblol/api | `main` | `ghcr.io/bharatradar/api` | amd64 |
| [bharatradar/history](https://github.com/bharatradar/history) | adsblol/history | `main` | `ghcr.io/bharatradar/history` | amd64 |
| [bharatradar/website](https://github.com/bharatradar/website) | adsblol/website | `main` | `ghcr.io/bharatradar/website` | amd64, arm64 |

#### Wrapper Images (built by infra CI)
| Image | Base | Purpose |
|-------|------|---------|
| `ghcr.io/bharatradar/docker-tar1090-uuid` | `docker-tar1090` fork + uuid binaries from `readsb` fork | tar1090 with UUID tracking (`rId` in aircraft.json) |
| `ghcr.io/bharatradar/mlat-server-sync-map` | `mlat-server-sync-map` fork + nginx proxy | MLAT coverage map with `/api/0/mlat-server/` reverse proxy |
| `ghcr.io/bharatradar/api` | `api` fork + patch.py | REST API with v2 routes, MY_DOMAIN support, Redis integration |

### DNS Records

All subdomains point to your AWS EC2 (FRP server) public IP.

| Type | Name | Value | Proxy | Purpose |
|------|------|-------|-------|---------|
| `A` | `bharatradar.com` | `<AWS_IP>` | Proxied | Homepage (website) |
| `A` | `map.bharatradar.com` | `<AWS_IP>` | Proxied | Live map (planes-readsb) |
| `A` | `my.bharatradar.com` | `<AWS_IP>` | Proxied | Personalized feeder map |
| `A` | `mlat.bharatradar.com` | `<AWS_IP>` | Proxied | MLAT coverage map |
| `A` | `history.bharatradar.com` | `<AWS_IP>` | Proxied | Historical flight data |
| `A` | `api.bharatradar.com` | `<AWS_IP>` | Proxied | REST API |
| `A` | `feed.bharatradar.com` | `<AWS_IP>` | **DNS only** | Feeder endpoint (ports 30004, 31090) |
| `A` | `ws.bharatradar.com` | `<AWS_IP>` | Proxied | WebSocket (future) |

> **Note:** `feed.bharatradar.com` must be **DNS only** (not proxied by Cloudflare) because it handles raw TCP connections for ADS-B beast feeds. All other subdomains use Cloudflare proxy for TLS termination and CDN.

### Subdomains

| Subdomain | Service | Notes |
|-----------|---------|-------|
| `map.bharatradar.com` | planes-readsb | tar1090 map interface |
| `my.bharatradar.com` | api | Personalized feeder map (IP-based lookup from Redis beast:clients) |
| `mlat.bharatradar.com` | mlat-map | MLAT coverage visualization |
| `history.bharatradar.com` | history | Historical flight data |
| `api.bharatradar.com` | api | Main web API (OpenAPI docs at `/docs`) |
| `bharatradar.com` | website | Homepage |
| `feed.bharatradar.com` | AWS FRP server | Feeder endpoint (ports 30004, 31090) |

## Known Limitations

### 1. FRP Tunnel & Feeder IP Tracking
All feeder connections route through the FRP tunnel (AWS EC2 → Hub), which means the ingest server sees the internal tunnel IP (`10.42.x.x`) instead of the feeder's real public IP. This breaks the `my.bharatradar.com` IP-based feeder lookup — it falls back to generic map redirect or single-feeder mode. **Fix:** Remove FRP and have feeders connect directly to the cluster (see TODO).

### 2. PVC-Bound Pods Cannot Fail Over
`planes-readsb` and `mlat-mlat-server` use `local-path` PersistentVolumes which are bound to a specific node. During a Primary Hub failure, these pods cannot reschedule to the HA Server because the PVC data is local to the failed node. **Fix:** Use shared network storage (NFS, Longhorn, etc.) or run as DaemonSet (see TODO).

### 3. Traefik Does Not Forward X-Real-IP
When the FRP tunnel terminates at the Hub, Traefik forwards requests to backend pods but the `X-Real-IP` header from the AWS nginx proxy is not preserved. The API app sees the Traefik pod's internal IP instead of the feeder's real IP. This compounds the FRP IP tracking issue.

### 4. API Image Patches Applied at Runtime
The `api` image (`ghcr.io/bharatradar/api`) is built from our fork `bharatradar/api` with runtime patches (`build/api/patch.py`). The patches replace hardcoded `adsb.lol` references with `MY_DOMAIN` and fix v2 route registration. These patches must be reapplied if the fork image changes.

### 5. History Pod is amd64-Only
The `history` image does not have an arm64 build. It will fail to run on Raspberry Pi nodes. The deployment has `nodeSelector: kubernetes.io/arch: amd64` to prevent scheduling on arm64 nodes.

### 6. MLAT Peers Show `{}` with Single Feeder
The MLAT map shows `"peers": {}` when there is only one feeder. This is normal — MLAT requires at least 3 receivers to triangulate positions. The sync map will populate peers as more feeders join.

## How?

### Prerequisites - What You Need Before Starting

| Item | Specification | Why |
|------|---------------|-----|
| **Shared Services Node** | Raspberry Pi 4+ or any Linux machine, 4GB+ RAM, 32GB+ SDD/SSD, Debian 12 or Raspberry Pi OS | Runs PostgreSQL, Redis, InfluxDB, MinIO |
| **AWS EC2 (or cloud VPS)** | Ubuntu 22.04+, 2GB+ RAM, public IP, ports 80/443/7000/30004/31090 open | Runs FRP server + nginx for tunneling |
| **Primary Hub** | Ubuntu 24.04, amd64 (Intel/AMD CPU), 4GB+ RAM, 50GB+ storage | K3s server, runs all ADS-B/MLAT services |
| **HA Server** (optional) | Same as Primary Hub | Backup K3s server for failover |
| **Feeder Pi** | Raspberry Pi 3B+ or newer, RTL-SDR dongle, Raspberry Pi OS | Sends ADS-B data to your server |
| **Domain** | Cloudflare account with A records pointing to your AWS IP | DNS for all subdomains |

### Exact Install Order (DO NOT SKIP)

> **CRITICAL: You must install in this exact order. Each step provides credentials needed for the next.**

```
Step 1: DNS Setup ──────────► Step 2: Shared Services ──────► Step 3: AWS FRP Server
(Cloudflare)                    (br-aggrigator Pi)                (AWS EC2)

Step 4: Primary Hub ─────────► Step 5: HA Server (optional) ──► Step 6: Feeder Pi
(K3s cluster)                     (backup node)                    (data feed)
```

### Step-by-Step Instructions

#### Step 1: DNS Records (Cloudflare)

1. **Log in to Cloudflare** and select your domain
2. **Create these A records** (replace `<AWS_IP>` with your AWS EC2 public IP):

| Type | Name | Value | Proxy (Cloudflare) |
|------|------|-------|-------------------|
| A | bharatradar.com | `<AWS_IP>` | Proxied (orange) |
| A | map.bharatradar.com | `<AWS_IP>` | Proxied |
| A | api.bharatradar.com | `<AWS_IP>` | Proxied |
| A | mlat.bharatradar.com | `<AWS_IP>` | Proxied |
| A | history.bharatradar.com | `<AWS_IP>` | Proxied |
| A | my.bharatradar.com | `<AWS_IP>` | Proxied |
| A | feed.bharatradar.com | `<AWS_IP>` | **DNS only** (grey cloud) |
| A | cortex.bharatradar.com | `<AWS_IP>` | Proxied |

> **IMPORTANT:** `feed.bharatradar.com` MUST be DNS only (grey cloud). Cloudflare proxy blocks raw TCP connections needed for ADS-B beast feeds.

#### Step 2: Shared Services (br-aggrigator Pi — 192.168.200.15)

**What this does:** Installs PostgreSQL (database), Redis (caching), InfluxDB (metrics), and MinIO (file storage) on a Raspberry Pi or any Linux machine.

**Run this on your Shared Services node:**
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services
```

**When it finishes, SAVE these credentials shown on screen:**
- PostgreSQL host, port, username, password, database name
- Redis host and password
- MinIO endpoint, access key, and secret key

**You'll need these for Step 4 (Primary Hub).**

For manual database setup or re-initialization, see [scripts/db/README.md](scripts/db/README.md).

#### Manual Database Reset

Create these A records in your DNS provider (Cloudflare recommended). Replace `<AWS_IP>` with your FRP server's public IP.

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

> **Critical:** `feed.bharatradar.com` must be **DNS only** (grey cloud). Cloudflare proxy blocks raw TCP connections for ADS-B beast feeds.

#### Step 1: Shared Services (br-aggrigator Pi — 192.168.200.15)

```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services
```

This installs PostgreSQL, Redis, InfluxDB, and MinIO. **Save the credentials shown at the end** — you'll need the PostgreSQL connection string for the Hub.

For manual database setup or re-initialization, see [scripts/db/README.md](scripts/db/README.md).

#### Manual Database Reset

To drop and recreate all data:

```bash
# SSH to the database server
ssh bharatradar@192.168.200.15

# Drop all tables
PGPASSWORD='raga@098' psql -h localhost -U flight_db_user -d flight_db -c "
DROP TABLE IF EXISTS airports CASCADE;
DROP TABLE IF EXISTS runways CASCADE;
DROP TABLE IF EXISTS flights_in_air CASCADE;
DROP TABLE IF EXISTS arrivals_log CASCADE;
DROP TABLE IF EXISTS departures_log CASCADE;
DROP TABLE IF EXISTS flight_events CASCADE;
DROP TABLE IF EXISTS ground_ops CASCADE;
DROP TABLE IF EXISTS flight_schedules CASCADE;
DROP TABLE IF EXISTS api_users CASCADE;
DROP TABLE IF EXISTS api_keys CASCADE;
DROP TABLE IF EXISTS feeders CASCADE;
DROP TABLE IF EXISTS feeder_daily_stats CASCADE;
DROP TABLE IF EXISTS feeder_achievements CASCADE;
DROP TABLE IF EXISTS coverage_gaps CASCADE;
DROP TABLE IF EXISTS user_alerts CASCADE;
DROP TABLE IF EXISTS web_subscriptions CASCADE;
DROP TABLE IF EXISTS ai_enrichment_audit CASCADE;
DROP TABLE IF EXISTS ai_insights_log CASCADE;
"

# Recreate schema and data
PGPASSWORD='raga@098' psql -h localhost -U flight_db_user -d flight_db -f schema.sql
PGPASSWORD='raga@098' psql -h localhost -U flight_db_user -d flight_db -f seed-airports.sql
PGPASSWORD='raga@098' psql -h localhost -U flight_db_user -d flight_db -f seed-runways.sql
```

See [scripts/db/README.md](scripts/db/README.md) for full documentation.

### Step 3: AWS FRP Server Setup (AWS EC2)

**What this does:** Sets up the FRP (Fast Reverse Proxy) server that tunnels traffic from feeders and web visitors to your local K3s cluster. Also sets up nginx for SSL/TLS.

**Run these commands on your AWS EC2 instance:**

#### 3.1 Install FRP Server
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- frp-server
```

**SAVE the FRP token shown on screen** — you'll need it for the Hub (Step 4).

#### 3.2 Install nginx (if not already installed by FRP script)
```bash
sudo apt update && sudo apt install -y nginx
```

#### 3.3 Install Certbot and Obtain SSL Certificates
```bash
sudo apt install -y certbot python3-certbot-nginx

# Expand certificate to include ALL subdomains
sudo certbot certonly --cert-name bharatradar.com \
  -d bharatradar.com \
  -d api.bharatradar.com \
  -d feed.bharatradar.com \
  -d history.bharatradar.com \
  -d map.bharatradar.com \
  -d mlat.bharatradar.com \
  -d my.bharatradar.com \
  -d ws.bharatradar.com \
  --nginx --non-interactive --agree-tos -m admin@your-domain.com
```

#### 4. Configure nginx Server Blocks
Create `/etc/nginx/sites-enabled/bharat-radar-subdomains`:

```nginx
# HTTPS - Map subdomain
server {
    listen 443 ssl http2;
    server_name map.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTPS - API subdomain
server {
    listen 443 ssl http2;
    server_name api.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTPS - MLAT subdomain
server {
    listen 443 ssl http2;
    server_name mlat.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTPS - History subdomain
server {
    listen 443 ssl http2;
    server_name history.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTPS - My subdomain
server {
    listen 443 ssl http2;
    server_name my.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTPS - Feed subdomain (web interface only, not beast/mlat TCP)
server {
    listen 443 ssl http2;
    server_name feed.bharatradar.com;
    ssl_certificate /etc/letsencrypt/live/bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### 5. Test and Reload nginx
```bash
sudo nginx -t && sudo systemctl reload nginx
```

#### 6. Auto-Renewal
Certbot automatically installs a systemd timer. Verify:
```bash
sudo systemctl status certbot.timer
```

#### 7. When Adding New Subdomains
If you add a new subdomain (e.g., `stats.bharatradar.com`):
```bash
# Expand the certificate
sudo certbot certonly --cert-name bharatradar.com \
  --expand -d bharatradar.com \
  -d api.bharatradar.com \
  -d feed.bharatradar.com \
  -d history.bharatradar.com \
  -d map.bharatradar.com \
  -d mlat.bharatradar.com \
  -d my.bharatradar.com \
  -d ws.bharatradar.com \
  -d stats.bharatradar.com \
  --nginx --non-interactive --agree-tos

# Add nginx server block
# Reload nginx
```

### Step 4: Primary Hub (K3s Server)

**What this does:** Installs K3s Kubernetes cluster, Keepalived for virtual IP failover, FRP client for tunneling, and deploys all ADS-B/MLAT services.

**You need these before starting:**
- Credentials from Step 2 (PostgreSQL, Redis, MinIO)
- FRP token from Step 3

**On the Primary Hub node (192.168.200.10), create `/tmp/hub.env`:**
```bash
cat > /tmp/hub.env << 'EOF'
ROLE=hub
BASE_DOMAIN=bharatradar.com
READSB_LAT=18.480718   # Your latitude (Pune = 18.480718)
READSB_LON=73.898235   # Your longitude
TIMEZONE=Asia/Kolkata
REDIS_HOST=192.168.200.15
REDIS_PORT=6379
REDIS_PASSWORD=<REDIS_PASSWORD_FROM_STEP_2>
GHCR_USERNAME=your-github-username
GHCR_PASSWORD=your-github-pat-with-packages-read-scope
USE_EXTERNAL_DB=true
DB_HOST=192.168.200.15
DB_PORT=5432
DB_DBNAME=k3s
DB_DBUSER=k3s
DB_DBPASS=<DB_PASSWORD_FROM_STEP_2>
MINIO_ENDPOINT=192.168.200.15:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=<MINIO_PASSWORD_FROM_STEP_2>
FRP_ENABLED=true
FRP_SERVER=<YOUR_AWS_PUBLIC_IP>
FRP_TOKEN=<FRP_TOKEN_FROM_STEP_3>
KEEPALIVED_ENABLED=true
KEEPALIVED_VIP=192.168.200.150
EOF
```

**Install:**
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/hub.env hub
```

**After installation, verify:**
```bash
sudo kubectl get pods -n bharatradar
sudo kubectl get nodes
```

#### Step 5: HA Server (Optional - for failover)

**What this does:** Adds a second K3s server that takes over if the Primary fails. Keeps the virtual IP (192.168.200.150) alive.
Create `/tmp/ha.env`:
```bash
cat > /tmp/ha.env << 'EOF'
ROLE=ha-server
BASE_DOMAIN=bharatradar.com
DB_HOST=192.168.200.15
DB_PORT=5432
DB_DBNAME=k3s
DB_DBUSER=k3s
DB_DBPASS=<from-shared-services>
K3S_CLUSTER_TOKEN=K10...your-token...
PRIMARY_HUB_IP=192.168.200.10
KEEPALIVED_ENABLED=true
KEEPALIVED_VIP=192.168.200.150
KEEPALIVED_STATE=BACKUP
KEEPALIVED_PRIORITY=90
EOF
```

Install:
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/ha.env ha-server
```

#### Step 6: Feeder Pi (Data Source)

**What this does:** Installs readsb (ADS-B receiver) and mlat-client on a Raspberry Pi with RTL-SDR dongle. Sends flight data to your server via the FRP tunnel.

**On your Raspberry Pi with RTL-SDR dongle:**

**Recommended - Standalone one-line installer (auto-detects SDR):**
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-feeder | sudo bash
```

**Alternative - via main installer:**
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- feeder
```

**The installer will prompt for:**
- Your latitude/longitude (antenna location)
- Feeder name (appears on MLAT map)
- SDR device selection (usually auto-detected)

**After installation, verify:**
```bash
sudo systemctl status readsb
sudo journalctl -u readsb --since "1 minute" | grep "hex:"
```

### Post-Install Verification

```bash
# Check all pods are running
kubectl get pods -n bharatradar

# Check nodes
kubectl get nodes -o wide

# Check VIP on Primary
ip addr show | grep 192.168.200.150

# Test endpoints
curl -s -o /dev/null -w "%{http_code}" https://map.bharatradar.com/
curl -s -o /dev/null -w "%{http_code}" https://api.bharatradar.com/
curl -s -o /dev/null -w "%{http_code}" https://mlat.bharatradar.com/syncmap/
curl -s -o /dev/null -w "%{http_code}" https://history.bharatradar.com/
curl -s -o /dev/null -w "%{http_code}" https://my.bharatradar.com/

# Check aircraft data
curl -s https://map.bharatradar.com/data/aircraft.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Aircraft: {len(d.get(chr(97)+chr(105)+chr(114)+chr(99)+chr(114)+chr(97)+chr(102)+chr(116)),[]))}')"
```

### Failover Test

```bash
# On Primary Hub: simulate failure
sudo systemctl stop k3s keepalived

# On HA Server: verify VIP moved
ip addr show | grep 192.168.200.150

# Restore Primary
sudo systemctl start k3s keepalived
```

### Useful Commands

```bash
# View all services
kubectl get svc -n bharatradar

# View logs
kubectl logs -n bharatradar deployment/api-api -c api --tail=50

# Restart a deployment
kubectl rollout restart deployment/api-api -n bharatradar

# Check ingress
kubectl get ingress -n bharatradar

# Check keepalived status
sudo systemctl status keepalived

# Check FRP client
sudo systemctl status frpc

# Re-run installer (auto-resumes from last checkpoint)
sudo bharatradar-install hub
```

### Feeder Pi Disk Cleanup

```bash
# Run cleanup manually
sudo bharatradar-cleanup

# View cleanup timer status
sudo systemctl status bharatradar-cleanup.timer

# View cleanup logs
sudo journalctl -u bharatradar-cleanup --since "today"

# Disable auto-cleanup (runs daily at 3am)
sudo systemctl disable --now bharatradar-cleanup.timer
```

## Manual Deploy (kustomize)

```bash
# Preview manifests
kustomize build manifests/default

# Deploy to cluster
kustomize build manifests/default | kubectl apply -f -

# Verify
kubectl get pods -n bharatradar
```

Full installer docs: [install.md](install.md)

## Release Workflow

Standard procedure for fixing issues, building new images, tagging, and redeploying to K3s.

### Prerequisites

- Docker with buildx multi-arch support
- kubectl configured for local preview
- SSH access to Hub (192.168.200.10) with sudo
- Git tag version format: `vYYYY.MM.DD.XX` (e.g., `v2025.05.07.01`)

### Step-by-Step

#### 1. Fix the Issue

Edit source code in `build/<component>/` or manifests in `manifests/default/`.

#### 2. Build New Image

```bash
# Set version
VERSION=v2025.05.07.01
COMPONENT=telegram-bot  # or flight-tracker, ai-agents, schedule-downloader, etc.

# Build multi-arch and push to GHCR
cd /Users/Shared/bharatradar/infra
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/bharatradar/${COMPONENT}:${VERSION} \
  --label "org.opencontainers.image.source=https://github.com/bharatradar/infra" \
  --label "org.opencontainers.image.version=${VERSION}" \
  --push \
  -f build/${COMPONENT}/Dockerfile \
  build/${COMPONENT}/
```

#### 3. Update Manifest Image Tag

Edit `manifests/default/<component>.yaml` and update the image reference:

```yaml
image: ghcr.io/bharatradar/telegram-bot:v2025.05.07.01
```

#### 4. Test Locally (Optional)

```bash
# Preview manifests before applying
kustomize build manifests/default | less

# Or validate against cluster dry-run
kustomize build manifests/default | kubectl apply --dry-run=client -f -
```

#### 5. Commit and Tag

```bash
cd /Users/Shared/bharatradar/infra
git add -A
git commit -m "fix: description of what was fixed

- Change 1
- Change 2

Images:
- ghcr.io/bharatradar/${COMPONENT}:${VERSION}"

# Create annotated tag
git tag -a ${VERSION} -m "Release ${VERSION}: description of changes"
```

#### 6. Deploy to K3s

```bash
cd /Users/Shared/bharatradar/infra
kustomize build manifests/default | \
  sshpass -p 'raga@098' ssh \
  -o StrictHostKeyChecking=no \
  bharatradar@192.168.200.10 \
  'sudo kubectl apply -f -'
```

#### 7. Verify Rollout

```bash
sshpass -p 'raga@098' ssh \
  -o StrictHostKeyChecking=no \
  bharatradar@192.168.200.10 \
  'sudo kubectl get pods -n bharatradar -w'
```

#### 8. Push to Remote

```bash
git push origin main
git push origin --tags
```

### Multi-Image Release

If multiple images change in one release:

```bash
VERSION=v2025.05.07.01

# Build all changed images
for component in telegram-bot flight-tracker ai-agents; do
  docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -t ghcr.io/bharatradar/${component}:${VERSION} \
    --push \
    -f build/${component}/Dockerfile \
    build/${component}/
done

# Update all manifest tags
# Edit manifests/default/*.yaml

# Commit, tag, deploy
git add -A
git commit -m "release ${VERSION}: update all custom images"
git tag -a ${VERSION} -m "Release ${VERSION}: multi-image update"

# Deploy
kustomize build manifests/default | \
  sshpass -p 'raga@098' ssh \
  -o StrictHostKeyChecking=no \
  bharatradar@192.168.200.10 \
  'sudo kubectl apply -f -'
```

### Emergency Rollback

```bash
# Revert manifest to previous tag
# Edit manifests/default/<component>.yaml

# Or rollback deployment directly on K3s
sshpass -p 'raga@098' ssh \
  -o StrictHostKeyChecking=no \
  bharatradar@192.168.200.10 \
  'sudo kubectl rollout undo deployment/<component> -n bharatradar'
```

## Future Improvements

### ETA Calculation: Option C (Hybrid Historical + Real-time)

The current ETA model (Option B) uses altitude-based descent profiles + airport-specific historical buffers. This is a significant improvement over raw distance/speed, but there's a **Option C** that would add even more accuracy:

**Option C: Hybrid Historical + Real-time**
- Aircraft-type-specific descent profiles (737 vs A380 descent at different rates)
- Real-time congestion factor (time-of-day traffic at destination airport)
- ML-based delay prediction using 30+ days of historical approach data
- Weather integration (headwinds/tailwinds affect ground speed)
- Turnaround prediction for connecting flights

**Requirements for Option C:**
- 30+ days of `arrivals_log` + `flight_events` data per airport
- Aircraft type mapping (from adsbdb or FR24)
- Basic weather API integration
- Simple regression model (can be rule-based initially)

**Current Status:** Option B is deployed (v2025.05.07.05). Option C is documented here for future implementation when historical data volume supports it.

## Command Center (Webapp) Auth

This section documents how to set up Google OAuth authentication for the Command Center.

### Prerequisites

- Google Cloud Project with OAuth 2.0 enabled
- OAuth consent screen configured
- Credentials created (OAuth 2.0 Client ID)

### Configuration Steps

#### 1. Create Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Navigate to **APIs & Services** → **Credentials**
3. Click **Create Credentials** → **OAuth client ID**
4. Application type: **Web application**
5. Name: `bharatradar-webapp`
6. Add **Authorized JavaScript origins**:
   - `https://bharatradar.com`
   - `https://bharat-radar.vellur.in` (legacy)
7. Add **Authorized redirect URIs**:
   - `https://bharatradar.com/command_center/auth/callback`
   - `https://bharatradar.com/auth/callback`
   - `https://bharat-radar.vellur.in/auth/callback` (legacy)
8. Click **Create**
9. Copy the **Client ID** and **Client secret**

#### 2. Create K8s Secret (on Hub server)

The OAuth credentials are stored as a Kubernetes Secret to avoid committing secrets to git:

```bash
# Create the secret (run on K3s server)
kubectl create secret generic google-oauth-credentials \
  --namespace=bharatradar \
  --from-literal=GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com \
  --from-literal=GOOGLE_CLIENT_SECRET=your-client-secret \
  --type=Opaque
```

#### 3. Update Webapp Deployment

The deployment references the secret via `envFrom`:

```yaml
envFrom:
  - secretRef:
      name: google-oauth-credentials
```

Also set the redirect URI via env var:

```yaml
- name: GOOGLE_REDIRECT_URI
  value: "https://bharatradar.com/command_center/auth/callback"
```

#### 4. Example: Deploy/Update Webapp

```bash
# SSH to hub server
ssh bharatradar@192.168.200.10

# Apply the manifest
sudo kubectl apply -f manifests/default/webapp.yaml

# Restart to pick up changes
sudo kubectl rollout restart deployment/webapp -n bharatradar

# Verify it's running
sudo kubectl get pods -n bharatradar -l app=webapp
```

#### 5. Test Authentication

1. Visit: `https://bharatradar.com/command_center/auth/google`
2. You should be redirected to Google login
3. After login, you'll be redirected to Command Center dashboard

### Troubleshooting

**Error: redirect_uri_mismatch**

This means the redirect URI in Google Cloud Console doesn't match what's being sent by the app.

- Verify the redirect URI in Google Console is exactly: `https://bharatradar.com/command_center/auth/callback`
- Wait 5 minutes for Google Console changes to propagate
- Clear browser cache or use incognito window

**Error: Access blocked: This app's request is invalid**

- The client ID may be restricted to specific domains
- Go to Google Console → OAuth consent screen → Test users
- Or make the app public (requires verification for sensitive scopes)

### GitHub Push Protection

When committing K8s manifests, never include actual secret values. The push will be blocked.

**Correct approach:**
- Use `envFrom` to reference a K8s Secret
- Create the Secret via CLI (not in YAML)
- Or use external secrets operator (not covered here)

```yaml
# WRONG - will block git push
env:
  - name: GOOGLE_CLIENT_SECRET
    value: "GOCSPX-xxx"

# CORRECT - loads from secret
envFrom:
  - secretRef:
      name: google-oauth-credentials
```

## Where?

- **GitHub:** https://github.com/bharatradar/infra
- **Images:** https://github.com/orgs/bharatradar/packages
- **Original Project:** https://github.com/adsblol/infra
