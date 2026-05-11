#!/bin/bash
# BharatRadar Schedule Downloader - Manual Trigger Script
# Use this script to manually trigger a schedule download job in K3s
#
# Usage:
#   ./trigger-downloader.sh              # Trigger with default job name
#   ./trigger-downloader.sh custom-run   # Trigger with custom job name
#
# To check status:
#   sudo kubectl get jobs -n bharatradar -l app=schedule-downloader
#   sudo kubectl logs -n bharatradar job/schedule-downloader-manual

set -e

NAMESPACE="bharatradar"
JOB_NAME="${1:-schedule-downloader-manual}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "BharatRadar Schedule Downloader - Manual Trigger"
echo "=========================================="
echo "Namespace: $NAMESPACE"
echo "Job Name: $JOB_NAME"
echo ""

# Check if cronjob exists (to verify the system is deployed)
if ! sudo kubectl get cronjob schedule-downloader -n "$NAMESPACE" &>/dev/null; then
    echo "ERROR: CronJob 'schedule-downloader' not found in namespace '$NAMESPACE'"
    echo "Make sure the schedule-downloader is deployed:"
    echo "  kubectl apply -f manifests/default/schedule-downloader-cronjob.yaml"
    exit 1
fi

# Delete any existing manual job with the same name
sudo kubectl delete job "$JOB_NAME" -n "$NAMESPACE" 2>/dev/null || true

# Create manual job using the manifest with --manual flag
echo "Creating manual job '$JOB_NAME'..."

# Create the job manifest with the specified name
cat <<EOF | sudo kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB_NAME
  namespace: $NAMESPACE
  labels:
    app: schedule-downloader
    run-type: manual
spec:
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: schedule-downloader
        run-type: manual
    spec:
      restartPolicy: OnFailure
      serviceAccountName: schedule-downloader
      imagePullSecrets:
      - name: ghcr-secret
      containers:
      - name: downloader
        image: ghcr.io/bharatradar/schedule-downloader:v1.0.5
        imagePullPolicy: IfNotPresent
        command: ["python", "-u", "route_schedule_downloader.py"]
        args: ["--manual"]
        env:
        - name: DB_HOST
          value: "127.0.0.1"
        - name: DB_PORT
          value: "5432"
        - name: DB_NAME
          value: "flight_db"
        - name: DB_USER
          value: "flight_db_user"
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: flight-db-credentials
              key: password
        - name: AIRLINES_FILE
          value: "/opt/bharatradar/flight_radar/data/airlines.csv"
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
EOF

echo ""
echo "Job created! Check status with:"
echo "  sudo kubectl get jobs -n $NAMESPACE $JOB_NAME"
echo "  sudo kubectl logs -n $NAMESPACE job/$JOB_NAME -f"
echo ""
echo "To delete the job after completion:"
echo "  sudo kubectl delete job $JOB_NAME -n $NAMESPACE"