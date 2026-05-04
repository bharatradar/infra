# bharatradar/infra

BharatRadar ADS-B/MLAT aggregator platform. Aggregates [ADS-B](https://github.com/wiedehopf/readsb) & [MLAT](https://github.com/wiedehopf/mlat-server) data from multiple feeders and serves a public map interface.

> **Version:** 5.3.1 (installer) / 3.3.0 (docs) / 5.0.0 (API image)
> **GitHub:** https://github.com/bharatradar/infra

## Why?

Community-driven ADS-B aggregation, built as a fork of [adsblol/infra](https://github.com/adsblol/infra). All upstream images are mirrored to `ghcr.io/bharatradar/` with multi-arch support (amd64 + arm64) for Raspberry Pi compatibility.

## Architecture

```
                    Cloudflare (DNS only)
                            |
                            v
                    AWS EC2 frps + nginx
                 (feed.bharat-radar.vellur.in)
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
         │  192.168.200.187            │
         │  PostgreSQL, Redis, MinIO   │
         └─────────────────────────────┘

    FEEDER PI (not K3s)
    192.168.200.127
    readsb → feed.bharat-radar.vellur.in:30004
    mlat-client → feed.bharat-radar.vellur.in:31090
```

### Data Flow

```
Feeder Pi (readsb + mlat-client)
    |
    ├── beast_reduce_plus_out ──→ feed.bharat-radar.vellur.in:30004 (AWS FRP server)
    │                                                          |
    │                                                          └── TCP tunnel ──→ Hub ingest-readsb:30004
    |
    └── mlat-client ──→ feed.bharat-radar.vellur.in:31090 (AWS FRP server)
                                                         |
                                                         └── TCP tunnel ──→ Hub mlat-mlat-server:31090

Hub Cluster:
  user ──→ Cloudflare ──→ AWS nginx ──→ FRP tunnel ──→ Traefik (K3s) ──→ Service pods
```

### Nodes

| Node | IP | OS | Role | Arch | K3s |
|------|----|----|------|------|-----|
| **Primary Hub** | 192.168.200.145 | Ubuntu 24.04 (Core i7) | K3s server, MASTER keepalived | amd64 | Yes |
| **HA Server** | 192.168.200.186 | Ubuntu 24.04 (Core i5) | K3s server, BACKUP keepalived | amd64 | Yes |
| **br-aggrigator** | 192.168.200.187 | Debian 12 (Raspberry Pi) | K3s agent, shared services | arm64 | Yes |
| **Feeder Pi** | 192.168.200.127 | Raspberry Pi OS | RTL-SDR + readsb + mlat-client (not K3s) | arm64 | No |

### Services

| Component | Image | Namespace | Ports | Notes |
|-----------|-------|-----------|-------|-------|
| **ingest-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004, 30005 | Receives feeder data via FRP (LoadBalancer) |
| **hub-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004, 30005 | Aggregates ingest data |
| **planes-readsb** | `ghcr.io/bharatradar/docker-tar1090-uuid` | bharatradar | 80, 30152 | Public tar1090 map with UUID tracking |
| **mlat-mlat-server** | `ghcr.io/bharatradar/mlat-server` | bharatradar | 31090, 30104, 150 | MLAT processing |
| **mlat-map** | `ghcr.io/bharatradar/mlat-server-sync-map` | bharatradar | 80 | MLAT coverage map (nginx proxies API) |
| **reapi-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30152 | REST API data feed (v2 endpoints) |
| **external-readsb** | `ghcr.io/bharatradar/readsb` | bharatradar | 30004 | External feeds (cnvr.io) |
| **api** | `ghcr.io/bharatradar/api:5.0.0` | bharatradar | 8080, 80 | Main web API (patched for MY_DOMAIN) |
| **history** | `ghcr.io/bharatradar/history` | bharatradar | 8080, 80 | Historical data (amd64 only) |
| **website** | nginx | bharatradar | 80 | Homepage |

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

#### Wrapper Images (built by infra CI)
| Image | Base | Purpose |
|-------|------|---------|
| `ghcr.io/bharatradar/docker-tar1090-uuid` | `docker-tar1090` fork + uuid binaries from `readsb` fork | tar1090 with UUID tracking (`rId` in aircraft.json) |
| `ghcr.io/bharatradar/mlat-server-sync-map` | `mlat-server-sync-map` fork + nginx proxy | MLAT coverage map with `/api/0/mlat-server/` reverse proxy |
| `ghcr.io/bharatradar/api` | `api` fork + patch.py | REST API with v2 routes, MY_DOMAIN support, Redis integration |

### Subdomains

| Subdomain | Service | Notes |
|-----------|---------|-------|
| `map.bharat-radar.vellur.in` | planes-readsb | tar1090 map interface |
| `my.bharat-radar.vellur.in` | api | Personalized feeder map (IP-based lookup from Redis beast:clients) |
| `mlat.bharat-radar.vellur.in` | mlat-map | MLAT coverage visualization |
| `history.bharat-radar.vellur.in` | history | Historical flight data |
| `api.bharat-radar.vellur.in` | api | Main web API (OpenAPI docs at `/docs`) |
| `bharat-radar.vellur.in` | website | Homepage |
| `feed.bharat-radar.vellur.in` | AWS FRP server | Feeder endpoint (ports 30004, 31090) |

## Known Limitations

### 1. FRP Tunnel & Feeder IP Tracking
All feeder connections route through the FRP tunnel (AWS EC2 → Hub), which means the ingest server sees the internal tunnel IP (`10.42.x.x`) instead of the feeder's real public IP. This breaks the `my.bharat-radar.vellur.in` IP-based feeder lookup — it falls back to generic map redirect or single-feeder mode. **Fix:** Remove FRP and have feeders connect directly to the cluster (see TODO).

### 2. PVC-Bound Pods Cannot Fail Over
`planes-readsb` and `mlat-mlat-server` use `local-path` PersistentVolumes which are bound to a specific node. During a Primary Hub failure, these pods cannot reschedule to the HA Server because the PVC data is local to the failed node. **Fix:** Use shared network storage (NFS, Longhorn, etc.) or run as DaemonSet (see TODO).

### 3. Traefik Does Not Forward X-Real-IP
When the FRP tunnel terminates at the Hub, Traefik forwards requests to backend pods but the `X-Real-IP` header from the AWS nginx proxy is not preserved. The API app sees the Traefik pod's internal IP instead of the feeder's real IP. This compounds the FRP IP tracking issue.

### 4. API Image Patches Applied at Runtime
The `api` image (`ghcr.io/bharatradar/api:5.0.0`) is built from our fork `bharatradar/api` with runtime patches (`build/api/patch.py`). The patches replace hardcoded `adsb.lol` references with `MY_DOMAIN` and fix v2 route registration. These patches must be reapplied if the fork image changes.

### 5. History Pod is amd64-Only
The `history` image does not have an arm64 build. It will fail to run on Raspberry Pi nodes. The deployment has `nodeSelector: kubernetes.io/arch: amd64` to prevent scheduling on arm64 nodes.

### 6. MLAT Peers Show `{}` with Single Feeder
The MLAT map shows `"peers": {}` when there is only one feeder. This is normal — MLAT requires at least 3 receivers to triangulate positions. The sync map will populate peers as more feeders join.

## How?

### Prerequisites

1. **AWS EC2 Server** (or any cloud VPS) with public IP — runs FRP server + nginx reverse proxy
2. **Primary Hub** — Ubuntu 24.04, amd64, internet access
3. **HA Server** (optional) — Ubuntu 24.04, amd64, same network as Primary
4. **br-aggrigator Pi** — Debian 12 or Raspberry Pi OS, arm64, for shared services
5. **Feeder Pi** — Raspberry Pi OS, arm64, RTL-SDR dongle
6. **Cloudflare DNS** — A records pointing to AWS EC2 IP for all subdomains

### AWS Server Setup (FRP + nginx + Certbot)

#### 1. Install FRP Server
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- frp-server
```

#### 2. Install nginx
```bash
sudo apt update && sudo apt install -y nginx
```

#### 3. Install Certbot and Obtain Certificates
```bash
sudo apt install -y certbot python3-certbot-nginx

# Expand certificate to include ALL subdomains
sudo certbot certonly --cert-name bharat-radar.vellur.in \
  -d bharat-radar.vellur.in \
  -d api.bharat-radar.vellur.in \
  -d feed.bharat-radar.vellur.in \
  -d history.bharat-radar.vellur.in \
  -d map.bharat-radar.vellur.in \
  -d mlat.bharat-radar.vellur.in \
  -d my.bharat-radar.vellur.in \
  -d ws.bharat-radar.vellur.in \
  --nginx --non-interactive --agree-tos -m admin@your-domain.com
```

#### 4. Configure nginx Server Blocks
Create `/etc/nginx/sites-enabled/bharat-radar-subdomains`:

```nginx
# HTTPS - Map subdomain
server {
    listen 443 ssl http2;
    server_name map.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
    server_name api.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
    server_name mlat.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
    server_name history.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
    server_name my.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
    server_name feed.bharat-radar.vellur.in;
    ssl_certificate /etc/letsencrypt/live/bharat-radar.vellur.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bharat-radar.vellur.in/privkey.pem;

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
If you add a new subdomain (e.g., `stats.bharat-radar.vellur.in`):
```bash
# Expand the certificate
sudo certbot certonly --cert-name bharat-radar.vellur.in \
  --expand -d bharat-radar.vellur.in \
  -d api.bharat-radar.vellur.in \
  -d feed.bharat-radar.vellur.in \
  -d history.bharat-radar.vellur.in \
  -d map.bharat-radar.vellur.in \
  -d mlat.bharat-radar.vellur.in \
  -d my.bharat-radar.vellur.in \
  -d ws.bharat-radar.vellur.in \
  -d stats.bharat-radar.vellur.in \
  --nginx --non-interactive --agree-tos

# Add nginx server block
# Reload nginx
```

### Cluster Setup

#### Step 1: Shared Services (br-aggrigator Pi)
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- shared-services
```
This installs PostgreSQL, Redis, InfluxDB, and MinIO. Save the credentials shown at the end.

#### Step 2: Primary Hub
Create `/tmp/hub.env`:
```bash
BASE_DOMAIN="bharat-radar.vellur.in"
REDIS_HOST="192.168.200.187"
REDIS_PORT="6379"
REDIS_PASSWORD="your-redis-password"
DB_CONNECTION_STRING="postgres://k3s:your-password@192.168.200.187:5432/k3s"
GHCR_USERNAME="your-github-username"
GHCR_PASSWORD="your-github-pat"
FRP_ENABLED="true"
FRP_SERVER="13.48.249.103"
FRP_TOKEN="your-frp-token"
KEEPALIVED_ENABLED="true"
KEEPALIVED_VIP="192.168.200.150"
```

Install:
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/hub.env hub
```

#### Step 3: HA Server (Optional but Recommended)
Create `/tmp/ha.env`:
```bash
DB_CONNECTION_STRING="postgres://k3s:your-password@192.168.200.187:5432/k3s"
K3S_CLUSTER_TOKEN="K10...your-token..."
PRIMARY_HUB_IP="192.168.200.145"
BASE_DOMAIN="bharat-radar.vellur.in"
KEEPALIVED_ENABLED="true"
KEEPALIVED_VIP="192.168.200.150"
KEEPALIVED_STATE="BACKUP"
KEEPALIVED_PRIORITY="90"
```

Install:
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- --conf-file /tmp/ha.env ha-server
```

#### Step 4: Feeder Pi
```bash
curl -Ls https://raw.githubusercontent.com/bharatradar/infra/main/scripts/bharatradar-install | sudo bash -s -- feeder
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
curl -s -o /dev/null -w "%{http_code}" https://map.bharat-radar.vellur.in/
curl -s -o /dev/null -w "%{http_code}" https://api.bharat-radar.vellur.in/
curl -s -o /dev/null -w "%{http_code}" https://mlat.bharat-radar.vellur.in/syncmap/
curl -s -o /dev/null -w "%{http_code}" https://history.bharat-radar.vellur.in/
curl -s -o /dev/null -w "%{http_code}" https://my.bharat-radar.vellur.in/

# Check aircraft data
curl -s https://map.bharat-radar.vellur.in/data/aircraft.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Aircraft: {len(d.get(chr(97)+chr(105)+chr(114)+chr(99)+chr(114)+chr(97)+chr(102)+chr(116)),[]))}')"
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

## Where?

- **GitHub:** https://github.com/bharatradar/infra
- **Images:** https://github.com/orgs/bharatradar/packages
- **Original Project:** https://github.com/adsblol/infra
