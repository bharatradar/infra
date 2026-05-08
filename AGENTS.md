# AGENTS.md - bharatradar/infra

> **Versions:** Installer v5.7.0 | Docs v5.7.0 | API Image v5.7.0

## Quick Verification
kustomize build manifests/default  # Build and preview final manifests

## Architecture
Data flow: user → ingest → hub → planes
              └→ mlat ↗

### Nodes
| Node | IP | Role | OS | Arch |
|------|----|------|----|------|
| Hub | 192.168.200.10 | K3s server (MASTER) | Ubuntu 24.04 (Core i7) | amd64 |
| HA Server | 192.168.200.186 | K3s server (BACKUP) | Ubuntu 24.04 | amd64 |
| br-aggrigator | 192.168.200.15 | K3s agent + Shared Services (PostgreSQL, Redis, InfluxDB, MinIO) | Debian 12 (Raspberry Pi) | arm64 |
| Feeder Pi | 192.168.200.127 | RTL-SDR readsb + mlat-client (not K3s) | Raspberry Pi OS | arm64 |

Services (manifests/default):
- ingest/     Public ADS-B ingest via LoadBalancer (ghcr.io/bharatradar/readsb)
- external/   External feeds (cnvr.io)
- hub/        Aggregation layer
- mlat/       MLAT server ✅ running (ghcr.io/bharatradar/mlat-server)
- reapi/      REST API backend (readsb with --net-api-port=30152)
- planes/     Public map ✅ running (ghcr.io/bharatradar/docker-tar1090-uuid)
- mlat-map/   MLAT sync UI ✅ running (nginx proxies /api/0/mlat-server/)
- api/        Main web API ✅ running v5.7.0 (ghcr.io/bharatradar/api:5.7.0)
- history/    Historical data ✅ running (amd64 only, dummy rclone secret)
- website/    Homepage
- command-center/webapp  Command Center web app ✅ running v2026.05.07.06
- telegram-bot/  Telegram bot with LLM routing (groq + MCP tools)
- resources.yaml  Namespace, Services, Ingresses, NetworkPolicies

## Conventions
- bases/     Reusable Kubernetes components
- */base/    Inherits from bases/
- */default/ Adds env vars, image overrides, patches
- namePrefix used for service isolation (e.g., ingest-readsb, hub-readsb)
- build/     Custom Dockerfiles for multi-arch images

## Custom Images

All images built from forked source repos via centralized CI in `bharatradar/infra`.
Fork repos (bharatradar/*) hold source code only — no CI workflows.

### Source Forks (built by infra CI)
| Fork | Upstream | Branch | Image | Platforms |
|------|----------|--------|-------|-----------|
| `bharatradar/readsb` | wiedehopf/readsb | `dev` | `ghcr.io/bharatradar/readsb` | amd64, arm64 |
| `bharatradar/docker-tar1090` | sdr-enthusiasts/docker-tar1090 | `main` | `ghcr.io/bharatradar/docker-tar1090` | amd64, arm64 |
| `bharatradar/mlat-server` | adsblol/mlat-server | `master` | `ghcr.io/bharatradar/mlat-server` | amd64 |
| `bharatradar/mlat-server-sync-map` | adsblol/mlat-server-sync-map | `master` | `ghcr.io/bharatradar/mlat-server-sync-map` | amd64 |
| `bharatradar/api` | adsblol/api | `main` | `ghcr.io/bharatradar/api` | amd64 |
| `bharatradar/webapp` | local build | — | `ghcr.io/bharatradar/webapp` | amd64, arm64 |
| `bharatradar/history` | adsblol/history | `main` | `ghcr.io/bharatradar/history` | amd64 |
| `bharatradar/website` | adsblol/website | `main` | `ghcr.io/bharatradar/website` | amd64, arm64 |

### Wrapper Images (built by infra CI)
- `docker-tar1090-uuid` ← `docker-tar1090` fork + uuid binaries from `readsb` fork (multi-arch)
- `mlat-server-sync-map` ← fork image + custom nginx proxy (amd64)
- `api` ← fork image + patch.py for v2 routes, MY_DOMAIN, Redis (amd64)

## Deployment
- Manual: `kustomize build manifests/default | kubectl apply -f -`
- Namespace: `bharatradar`
- GHCR pull secret: `ghcr-secret` in `bharatradar` namespace (required for all deployments)
- History has `nodeSelector: kubernetes.io/arch: amd64`

## API (v5.7.0)
- Image: `ghcr.io/bharatradar/api:5.7.0`
- OpenAPI schema: 18 paths (4 v0 + 14 v2)
- v2 endpoints: /v2/pia, /v2/mil, /v2/ladd, /v2/squawk/{squawk}, /v2/sqk/{squawk}, /v2/type/{aircraft_type}, /v2/reg/{registration}, /v2/registration/{registration}, /v2/hex/{icao_hex}, /v2/icao/{icao_hex}, /v2/callsign/{callsign}, /v2/point/{lat}/{lon}/{radius}, /v2/lat/{lat}/lon/{lon}/dist/{radius}, /v2/closest/{lat}/{lon}/{radius}
- Path parameters work in Swagger UI "Try it out" mode
- ReAPI backend: `reapi-readsb.bharatradar.svc.cluster.local:30152`
- Requires `--net-api-port=30152` on reapi-readsb deployment

## Feeder
- Feeder Pi (192.168.200.127) connects directly to `feed.bharatradar.com:30004`
- Uses `bharat-feeder` systemd service (readsb --net-only --net-connector)
- Uses `bharat-mlat` systemd service (mlat-client → feed.bharatradar.com:31090)
- Not part of Kubernetes cluster

## FRP
- Server on AWS EC2 (13.48.249.103): frps
- Client on Hub (192.168.200.10): frpc
- Proxies: TCP 30004/30005/31090 + HTTP/HTTPS for web + mlat-map

## Owner
@bharatradar/sre

## Key Notes
- All images are multi-arch (amd64 + arm64) for Pi compatibility
- UUID tracking enabled via custom docker-tar1090-uuid image (rId in aircraft.json)
- MLAT map has nginx reverse proxy for `/api/0/mlat-server/` endpoints
- Peers: {} on MLAT map is normal with single feeder (requires multiple receivers)
- `my.bharatradar.com/` redirects to `map.bharatradar.com/?filter_uuid=<uuid>` based on IP lookup from Redis `beast:clients`
- API image built from `build/api/` which patches `ghcr.io/bharatradar/api` fork at runtime
- v2 route registration bug fixed in v5.0.0 by removing broken decorator pattern
- ReAPI port (--net-api-port=30152) required for v2 endpoints to fetch aircraft data

## TODO / Future Enhancements

### High Priority
- ~~**Remove FRP tunnel**~~ → **Alternative: Feeder direct connect or FRP on both nodes**
  - Current: FRP tunnel masks feeder IPs, breaking `my.bharatradar.com` UUID lookup
  - Options: (a) Move feeders to direct cluster IP, (b) Run frpc on HA Server too for full failover, (c) Use a public load balancer instead of FRP
  - Note: `my.bharatradar.com` now redirects correctly to `map.bharatradar.com`, but UUID filter only works when API sees real feeder IP
- **Feeder self-registration script**: A bash script for feeders to get their UUID without relying on IP lookup:
  1. Script runs on feeder Pi
  2. Queries Redis or API for all connected feeders
  3. Matches local MAC address or hostname to UUID
  4. Prints personalized map URL (`map.bharatradar.com/?filter_uuid=<uuid>`)
  5. Useful for feeders behind CGNAT, proxies, or when FRP is active
- **FRP Client on HA Server**: Currently frpc only runs on Primary Hub. If Primary fails, the FRP tunnel dies even though K3s fails over. Need to run frpc on HA Server with Keepalived VIP binding so the tunnel always follows the active node.

### Medium Priority
- **Shared Storage for PVCs**: Use Longhorn, NFS, or Ceph to replace local-path provisioner.
  - Affected pods: `planes-readsb` (planes-state PVC), `mlat-mlat-server` (mlat PVC)
  - Currently: PVCs are node-bound; pods cannot reschedule to HA Server during failover
  - With shared storage: Full stateful failover including map history and MLAT sync state
- **DaemonSet for Beast/MLAT**: Run `ingest-readsb` and `mlat-mlat-server` as DaemonSets on both Hub nodes.
  - Eliminates 30-60s pod reschedule window during node failover
  - Each node runs its own local instance; VIP determines which one receives traffic
  - Requires shared storage for state persistence across nodes
- **Traefik IP Forwarding**: Configure Traefik to preserve `X-Real-IP` and `X-Forwarded-For` headers from the AWS nginx proxy.
  - Currently: API sees Traefik pod IPs (`10.42.x.x`) instead of real client IPs
  - Fix: Enable `forwardedHeaders.trustedIPs` in Traefik Helm values to trust the AWS server IP
  - This would at least fix IP lookups for direct API calls (though FRP still masks feeder IPs)

### Low Priority / Infrastructure
- **Automated API Image Rebuild**: The `api:5.0.0` image relies on runtime patches (`build/api/patch.py`) to replace hardcoded `adsb.lol` references.
  - Current workaround: Patched `app.py` mounted via ConfigMap in the deployment
  - Proper fix: Build a custom image in CI/CD that applies patches at build time
  - Reference: `build/api/Dockerfile` + `build/api/patch.py`
- **Version Pinning**: K3s installer downloads latest stable by default.
  - Pin to a specific version (e.g., `v1.35.4+k3s1`) for reproducible deployments
  - Update the installer to use `INSTALL_K3S_VERSION` env var consistently across all roles
- **AWS nginx Config as Code**: The nginx server blocks on the AWS EC2 server are manually configured.
  - Templatize the nginx config in the repo (e.g., `scripts/aws/nginx-subdomains.conf`)
  - Add certbot expansion commands to the frp-server installer role
  - Document subdomain addition procedure in install.md
- **Keepalived Interface Selection**: The HA Server auto-detected `wlp3s0` (WiFi) instead of the wired interface.
  - Add `KEEPALIVED_INTERFACE` override option to the config
  - Default to the interface with the default route, but allow explicit override
- **Cleanup: Remove haproxy references**: The old haproxy deployment was removed from the architecture but some docs still mention it.
  - Audit all docs and remove stale haproxy references
  - Update architecture diagrams to show direct LoadBalancer services instead

## Adding a New Subdomain (e.g., cortex.bharatradar.com)

When adding a new web service that needs its own subdomain, you need to configure THREE things:

### 1. DNS (Cloudflare)
Add an A record pointing to your public server IP:
```
cortex.bharatradar.com → 13.48.249.103
```

### 2. SSL Certificate (Let's Encrypt via Certbot)
On the **public-facing server** (AWS EC2 or wherever frps/nginx runs):
```bash
# Install certificate
sudo certbot --nginx -d cortex.bharatradar.com --non-interactive --agree-tos --email your-email@example.com

# Auto-renewal is set up by certbot
```

### 3. nginx Configuration
On the **public-facing server**, add a server block:

```nginx
server {
    listen 80;
    server_name cortex.bharatradar.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name cortex.bharatradar.com;

    ssl_certificate /etc/letsencrypt/live/cortex.bharatradar.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cortex.bharatradar.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;  # FRP vhost HTTP port
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable and reload:
```bash
sudo ln -sf /etc/nginx/sites-available/cortex /etc/nginx/sites-enabled/cortex
sudo nginx -t && sudo systemctl reload nginx
```

### 4. FRP Tunnel (frpc.toml on local server)
On the **local K3s server** (Hub at 192.168.200.10), add to `/etc/frpc.toml`:
```toml
# In the [[proxies]] section with the web-ui proxy, add the domain:
customDomains = [
    "map.bharatradar.com",
    "mlat.bharatradar.com",
    # ... other domains ...
    "cortex.bharatradar.com"  # <-- ADD HERE
]
```

Then restart frpc:
```bash
sudo systemctl restart frpc
```

### 5. K3s Ingress
Create a Kubernetes Ingress in your manifests:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cortex-webapp
  namespace: bharatradar
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
spec:
  ingressClassName: traefik
  rules:
    - host: cortex.bharatradar.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: cortex-webapp
                port:
                  number: 8081
```

Apply it:
```bash
kubectl apply -f manifests/default/cortex-webapp/ingress.yaml
```

### Summary Checklist
- [ ] DNS A record added to Cloudflare
- [ ] SSL certificate issued via certbot
- [ ] nginx server block configured
- [ ] Domain added to frpc.toml customDomains
- [ ] K3s Ingress created
- [ ] Service deployed and running in K3s

### Troubleshooting
- **ERR_CERT_COMMON_NAME_INVALID**: SSL cert doesn't include the subdomain. Re-run certbot.
- **404 from nginx**: frpc.toml doesn't have the domain in customDomains.
- **404 from K3s**: Ingress host doesn't match, or service name/port is wrong.
- **Connection refused**: frpc isn't running, or K3s service isn't exposing the right port.

## Optional Components (Future)

These components are already defined in your manifests but require CRDs to be installed:

### 1. Monitoring (Prometheus + Grafana)

Web dashboards for cluster monitoring (CPU, memory, request rates).

**Already defined:**
- `api/base/api.yaml` (line 80) has ServiceMonitor

**Install:**
```bash
# Add Helm repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Install kube-prometheus-stack
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.service.type=LoadBalancer \
  --set prometheus.prometheusSpec.retention=30d
```

**Access:** Get Grafana URL:
```bash
kubectl get svc -n monitoring -l app.kubernetes.io/name=grafana
```

**Public URL:** https://grafana.bharatradar.com/login
- Username: admin
- Password: (get from secret)

**Troubleshooting:**
- If 404 from FRP: Add subdomain to `customDomains` in `/etc/frpc.toml` on K3s server
- Restart frpc after config change: `sudo systemctl restart frpc`

**Subdomain format for FRP** - needs to match exactly between:
1. AWS nginx server_name
2. AWS certbot certificate
3. K3s frpc.toml customDomains
4. K3s Ingress host
```

**Default login:** admin / prom-operator (change after first login)

---

### 2. FluxCD (GitOps Auto-Deploy)

Auto-deploys when new images are pushed to GHCR.

**Already defined in manifests:**
- `api/default/flux.yaml` - ImagePolicy, ImageRepository, ImageUpdateAutomation
- `history/default/flux.yaml` - FluxCD for history
- `mlat-map/default/flux.yaml` - FluxCD for mlat-map

**Install:**
```bash
# Install FluxCD
kubectl apply -f https://fluxcd.io/install.sh

# Verify
flux check
```

**Configure existing flux.yaml files:**
```bash
# Apply existing flux resources
kubectl apply -f manifests/default/api/default/flux.yaml
kubectl apply -f manifests/default/history/default/flux.yaml
kubectl apply -f manifests/default/mlat-map/default/flux.yaml
```

---

### 3. cert-manager (TLS - AWS Migration Only)

For moving TLS from AWS EC2 to K3s (future).

**Already defined:**
- `resources.yaml` (line 166) has Certificate resource

**When migrating off AWS:**
```bash
# Install cert-manager CRDs
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/release/cert-manager.yaml

# Create ClusterIssuer (Let's Encrypt)
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: your-email@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: traefik
EOF

# Update ingress to use cert-manager
# Edit manifests/default/resources.yaml - add annotations:
#   cert-manager.io/cluster-issuer: letsencrypt-prod
```

**Current setup:** TLS terminates on AWS EC2 (certbot) → K3s receives plain HTTP via FRP. This is fine for now.

---

### Summary: What's Already Defined

| Component | Manifest File | Status |
|-----------|--------------|--------|
| ServiceMonitor | `api/base/api.yaml` | Needs Prometheus |
| ImagePolicy | `api/default/flux.yaml` | Needs FluxCD |
| ImageRepository | `api/default/flux.yaml` | Needs FluxCD |
| ImageUpdateAutomation | `api/default/flux.yaml` | Needs FluxCD |
| Certificate | `resources.yaml` | Needs cert-manager |
