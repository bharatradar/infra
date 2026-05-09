# Installation Troubleshooting Guide

Common issues encountered during BharatRadar infra installation and how to fix them.

---

## 1. Services Get Wrong DB_HOST / REDIS_HOST (FRP Token Leak)

### Symptom
Pods fail to connect to PostgreSQL, Redis, or InfluxDB with connection errors. Check the ConfigMap:
```bash
kubectl get configmap flight-tracker-config -n bharatradar -o yaml
```
If `DB_HOST`, `REDIS_HOST`, or `INFLUXDB_URL` contain a long random string (the FRP token) instead of an IP address (e.g. `192.168.200.11`), this is the bug.

### Root Cause
In `scripts/roles/hub.sh`, the `templating_generate_kustomization` function was called with parameters in the wrong order:

```bash
# BUG: FRP_TOKEN passed as 8th param (shared_host), REDIS_HOST as 9th (ignored)
templating_generate_kustomization \
    ... \
    "${FRP_TOKEN:-}" "${REDIS_HOST:-192.168.200.12}"
```

The 8th parameter (`shared_host`) is used to replace `SHARED_SERVICES_HOST` placeholders in all manifests. When the FRP token is passed instead of the Redis IP, every manifest gets the FRP token as its database/redis/influx host.

### Fix
1. **Swap parameters** in `hub.sh` so `$8` = `REDIS_HOST`, `$9` = `FRP_TOKEN`:
   ```bash
   templating_generate_kustomization \
       "${SCRIPT_DIR}/../manifests/default" \
       "$BASE_DOMAIN" "$READSB_LAT" "$READSB_LON" "$TIMEZONE" \
       "${FRP_SERVER:-}" "$API_SALT" "${REDIS_HOST:-192.168.200.12}" "${FRP_TOKEN:-}"
   ```

2. **Update `templating.sh`** to use `$9` for the FRP token:
   ```bash
   local shared_host="${8:-${REDIS_HOST:-192.168.200.12}}"
   local frp_token_arg="${9:-}"
   # ...
   templating_generate_frpc_config "$frp_server" "$domain" "${frp_token_arg:-}"
   ```

3. **Patch running manifests** — fix the ConfigMap and env vars on affected deployments:
   ```bash
   # Fix ConfigMap
   kubectl edit configmap flight-tracker-config -n bharatradar
   # Change DB_HOST, REDIS_HOST, INFLUXDB_URL from FRP token to actual IP

   # Fix deployments with hardcoded env vars
   kubectl set env deployment/telegram-bot -n bharatradar DB_HOST=<correct_ip> REDIS_HOST=<correct_ip>
   kubectl set env deployment/ai-agents -n bharatradar DB_HOST=<correct_ip> REDIS_HOST=<correct_ip>
   ```

4. **Restart affected deployments**:
   ```bash
   kubectl rollout restart deployment/flight-tracker -n bharatradar
   kubectl rollout restart deployment/telegram-bot -n bharatradar
   kubectl rollout restart deployment/ai-agents -n bharatradar
   ```

### Verification
Check that the ConfigMap now shows the correct IP:
```bash
kubectl get configmap flight-tracker-config -n bharatradar -o yaml | grep -E "DB_HOST|REDIS_HOST|INFLUXDB_URL"
```

Check pod logs for connection errors:
```bash
kubectl logs -n bharatradar deployment/flight-tracker --tail=20
```

---

## 2. InfluxDB 401 Unauthorized (Token Mismatch)

### Symptom
flight-tracker logs show repeated 401 errors:
```
WARNING - Failed to write telemetry to InfluxDB for XXX: (401)
Reason: Unauthorized
HTTP response body: b'{"code":"unauthorized","message":"unauthorized access"}'
```

InfluxDB server logs show:
```
Unauthorized log_id=... error="authorization not found"
```

### Root Cause
The shared-services installer checks if InfluxDB is already onboarded via `GET /api/v2/setup`. If it returns `"allowed": false` (already set up), the installer **skips re-initialization** and keeps the existing token. If InfluxDB was set up during an earlier deployment attempt with a different `INFLUXDB_ADMIN_TOKEN`, the token in the credentials file and Kubernetes secret won't match what InfluxDB actually has stored.

### Detection
Test the token from the credentials file:
```bash
influx query "from(bucket:\"raga_flight_radar_db\") |> range(start:-1h)" \
  --org bharatradar --token <token_from_credentials>
# Returns: 401 Unauthorized if mismatched
```

Check the credentials file:
```bash
cat /etc/bharatradar/credentials/shared-services-*.txt
```

### Fix — Reset InfluxDB

**WARNING:** This destroys all existing InfluxDB data.

1. Stop InfluxDB and wipe data:
   ```bash
   sudo systemctl stop influxdb
   sudo rm -rf /var/lib/influxdb/engine /var/lib/influxdb/influxd.bolt /var/lib/influxdb/.influxdbv2
   sudo systemctl start influxdb
   sleep 3
   ```

2. Verify setup is needed:
   ```bash
   curl -s http://localhost:8086/api/v2/setup
   # Should return: {"allowed": true}
   ```

3. Onboard with the correct token (from `config.env` or credentials file):
   ```bash
   curl -s -X POST http://localhost:8086/api/v2/setup \
     -H "Content-Type: application/json" \
     -d '{
       "username": "admin",
       "password": "<INFLUXDB_ADMIN_TOKEN>",
       "org": "bharatradar",
       "bucket": "raga_flight_radar_db",
       "token": "<INFLUXDB_ADMIN_TOKEN>"
     }'
   ```

4. Verify the token works:
   ```bash
   influx query "from(bucket:\"raga_flight_radar_db\") |> range(start:-1h)" \
     --org bharatradar --token <INFLUXDB_ADMIN_TOKEN>
   ```

### Prevention
If you want to update the InfluxDB token without wiping data, use the API to create a new token:
```bash
# First get a working session by authenticating with the existing admin password
# Or use an existing valid token to create a new one:
curl -s -X POST http://localhost:8086/api/v2/authorizations \
  -H "Authorization: Token <existing_valid_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "orgID": "<org_id>",
    "permissions": [{"action": "read", "resource": {"type": "buckets"}}],
    "token": "<new_token_value>"
  }'
```

Then update the Kubernetes secret and credentials file with the new token.

---

## 3. Cortex-Webapp Not Deployed (Deployment Skipped Silently)

### Symptom
`cortex.bharatradar.com` returns 404 from AWS nginx (before we deployed the ingress) or the cortex-webapp pod doesn't exist:
```bash
kubectl get pods -n bharatradar -l app=cortex-webapp
# No resources found
```

### Root Cause
The `deploy_component` function in `hub.sh` supports:
- **Kustomize directories**: `component/default/` with `kustomization.yaml`
- **Single YAML files**: `component.yaml`

It does **not** support directories with multiple standalone YAML files (like `cortex-webapp/deployment.yaml` + `cortex-webapp/ingress.yaml`). The function silently skips the component (logged as "Skipping cortex-webapp - not found") because neither check matches.

The `|| true` on the deploy call means you won't notice the skip:
```bash
deploy_component "cortex-webapp" || true   # failure is swallowed
```

### Fix — Manual Deployment

1. Create the Google OAuth secret (required by cortex-webapp):
   ```bash
   kubectl create secret generic google-oauth-credentials -n bharatradar \
     --from-literal=GOOGLE_CLIENT_ID=<your_client_id> \
     --from-literal=GOOGLE_CLIENT_SECRET=<your_client_secret>
   ```

2. Apply the deployment with correct shared services host and DB password:
   ```bash
   sed "s/SHARED_SERVICES_HOST/<shared_ip>/g; s/flight_db_password/<db_password>/g" \
     manifests/default/cortex-webapp/deployment.yaml | kubectl apply -f -
   ```

3. Apply the ingress:
   ```bash
   kubectl apply -f manifests/default/cortex-webapp/ingress.yaml
   ```

4. **Fix the ingress** to match other service ingresses (remove TLS annotations since AWS nginx terminates TLS):
   ```bash
   kubectl annotate ingress cortex-webapp -n bharatradar \
     traefik.ingress.kubernetes.io/router.entrypoints- \
     traefik.ingress.kubernetes.io/router.tls-
   ```

### Verification
```bash
# Check pod is running
kubectl get pods -n bharatradar -l app=cortex-webapp

# Check logs
kubectl logs -n bharatradar deployment/cortex-webapp --tail=20

# Check ingress
kubectl get ingress cortex-webapp -n bharatradar

# Check from browser
curl -s https://cortex.bharatradar.com | head -5
```

### Permanent Fix
Convert the cortex-webapp manifest directory to a kustomize structure or consolidate into a single YAML file so `deploy_component` can find it.

---

## 4. cortex.bharatradar.com 404 (Missing AWS nginx Config)

### Symptom
Accessing `https://cortex.bharatradar.com` returns `404 Not Found` from `nginx/1.24.0 (Ubuntu)`.

### Root Cause
The AWS nginx or FRP server doesn't have a server block for `cortex.bharatradar.com`.

### Fix
Configure the AWS EC2 server:

1. **DNS** — Add A record in Cloudflare:
   ```
   cortex.bharatradar.com → <AWS_EC2_IP>
   ```

2. **SSL Certificate** — Run certbot on the AWS server:
   ```bash
   sudo certbot --nginx -d cortex.bharatradar.com \
     --non-interactive --agree-tos --email your-email@example.com
   ```

3. **nginx config** — Add server block on AWS:
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
           proxy_pass http://127.0.0.1:8080;
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

4. **FRP** — Add `cortex.bharatradar.com` to `customDomains` in `/etc/frpc.toml` on the K3s server:
   ```toml
   customDomains = [
       # ...
       "cortex.bharatradar.com"
   ]
   ```
   Restart frpc:
   ```bash
   sudo systemctl restart frpc
   ```

5. **K3s** — Create ingress and deploy cortex-webapp (see Section 3).

---

## 5. General Troubleshooting

### Pod stuck in `ContainerCreating`
```bash
kubectl describe pod <pod-name> -n bharatradar | grep -A10 Events
```
Common causes: image pull failure (check GHCR credentials), resource limits, or PersistentVolume issues.

### Image pull failure
```bash
kubectl describe pod <pod-name> -n bharatradar | grep -i "error\|fail\|pull"
```
Verify the `ghcr-secret` exists and has valid credentials:
```bash
kubectl get secret ghcr-secret -n bharatradar
```

### Check all pod logs at once
```bash
for pod in $(kubectl get pods -n bharatradar -o name); do
    echo "=== $pod ==="
    kubectl logs $pod -n bharatradar --tail=5
done
```

### Reset K3s cluster
If you need a completely fresh K3s install:
```bash
# On the server
sudo /usr/local/bin/k3s-uninstall.sh

# Also clean up the PostgreSQL datastore
sudo -u postgres psql -c "DROP DATABASE IF EXISTS k3s;"
sudo -u postgres psql -c "CREATE DATABASE k3s OWNER k3s;"

# Re-run the hub installer
```

### Check service connectivity from within a pod
```bash
kubectl exec -n bharatradar deployment/flight-tracker -- \
  bash -c "apt-get update -qq && apt-get install -y -qq postgresql-client && psql -h \$DB_HOST -U \$DB_USER -d \$DB_NAME -c 'SELECT 1;'"
```

### Verify FRP tunnel
```bash
# On K3s server
sudo systemctl status frpc
sudo journalctl -u frpc --no-pager -n 20

# On AWS server
ssh ubuntu@<aws_ip> "sudo systemctl status frps"
```

---

## Quick Reference

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| DB/REDIS/INFLUXDB host is a long random string | FRP token passed as shared_host in templating | Swap params in hub.sh, patch configs |
| InfluxDB 401 Unauthorized | Token mismatch between config and InfluxDB | Reset InfluxDB and re-onboard |
| cortex.bharatradar.com 404 | Missing nginx/FRP/K3s config | Configure all layers (DNS→nginx→FRP→K3s) |
| cortex-webapp not deployed | deploy_component doesn't support multi-file dirs | Apply manifests manually |
| Pod stuck in ContainerCreating | Image pull or resource issue | Check pod events, verify ghcr-secret |
