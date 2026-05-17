#!/usr/bin/env bash
set -euo pipefail

# BharatRadar RapidAPI Key Updater
# Usage: ./scripts/admin/update-rapidapi-key.sh <KEY> [--append]
#   --append: Add as an additional key (rapidapi_key_N) instead of replacing the primary
#
# This script only updates the K8s secret. After updating, visit the admin console
# (https://admin.bharatradar.com) and click "Refresh from API" to sync the new key's
# rate limit data into the database.
#
# Examples:
#   ./scripts/admin/update-rapidapi-key.sh <NEW_KEY>              # Replace primary key
#   ./scripts/admin/update-rapidapi-key.sh <NEW_KEY> --append      # Add as secondary key

NEW_KEY="${1:?Usage: $0 <KEY> [--append]}"
APPEND=false
if [ "${2:-}" = "--append" ]; then
  APPEND=true
fi

KEY_HASH=$(echo -n "$NEW_KEY" | sha256sum | cut -d' ' -f1)

if [ "$APPEND" = true ]; then
  i=1
  while kubectl get secret aerodatabox-credentials -n bharatradar -o json 2>/dev/null | \
    jq -e ".data[\"rapidapi_key_$i\"]" >/dev/null 2>&1; do
    i=$((i + 1))
  done
  KEY_NAME="rapidapi_key_$i"
  echo "Adding key as $KEY_NAME..."
else
  KEY_NAME="rapidapi_key"
  echo "Updating primary key (rapidapi_key + rapidapi_key_1)..."
fi

echo "Updating K8s secret aerodatabox-credentials..."
CURRENT=$(kubectl get secret aerodatabox-credentials -n bharatradar -o json 2>/dev/null || echo "{}")
if [ "$APPEND" = true ]; then
  kubectl create secret generic aerodatabox-credentials \
    -n bharatradar \
    $(echo "$CURRENT" | jq -r '.data | to_entries[] | "--from-literal=" + .key + "=" + (.value | @base64d)' 2>/dev/null || true) \
    --from-literal="$KEY_NAME=$NEW_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  kubectl create secret generic aerodatabox-credentials \
    -n bharatradar \
    $(echo "$CURRENT" | jq -r '.data | to_entries[] | "--from-literal=" + .key + "=" + (.value | @base64d)' 2>/dev/null || true) \
    --from-literal=rapidapi_key="$NEW_KEY" \
    --from-literal=rapidapi_key_1="$NEW_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

echo "Done. Key hash: $KEY_HASH"
echo ""
echo "Next step: Visit https://admin.bharatradar.com and click 'Refresh from API'"
echo "to sync rate limit data into the database."
