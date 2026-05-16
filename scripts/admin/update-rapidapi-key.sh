#!/usr/bin/env bash
set -euo pipefail

# BharatRadar RapidAPI Key Updater
# Usage: ./scripts/admin/update-rapidapi-key.sh <NEW_KEY> [plan]
#   plan: 'pro' (default) or 'free'
#
# Updates K8s secret + DB tracking in one command.
# Run this ON the server (SSH in first).

NEW_KEY="${1:?Usage: $0 <NEW_KEY> [free|pro]}"
PLAN="${2:-pro}"

case "$PLAN" in
  pro)  LIMIT=6000; BURN=166 ;;
  free) LIMIT=600;  BURN=280 ;;
  *)    echo "Unknown plan: $PLAN (use 'free' or 'pro')"; exit 1 ;;
esac

KEY_HASH=$(echo -n "$NEW_KEY" | sha256sum | cut -d' ' -f1)

echo "Updating K8s secret aerodatabox-credentials..."
kubectl create secret generic aerodatabox-credentials \
  -n bharatradar \
  --from-literal=rapidapi_key="$NEW_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Updating download_config (limit=$LIMIT, daily_burn=$BURN)..."
PGPASSWORD=$(kubectl get secret flight-db-credentials -n bharatradar -o jsonpath='{.data.password}' | base64 -d) \
psql -h 45.88.189.38 -U flight_db_user -d flight_db -c "
  UPDATE download_config SET
    rapidapi_units_limit = $LIMIT,
    rapidapi_daily_burn = $BURN,
    rapidapi_units_used = 0,
    rapidapi_key_hash = '$KEY_HASH',
    updated_at = NOW()
  WHERE id = 1;
"

echo "Done. Plan: $PLAN (limit=$LIMIT units)."
