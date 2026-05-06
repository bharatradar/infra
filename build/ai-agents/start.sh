#!/bin/bash
# AI Agents Wrapper - runs all scheduled interval tasks
# Usage: python start.sh [mode]
#   (default) - runs all agents: user_alert_watchdog, agents.py
#   AGENTS_ONLY - runs only agents.py (janitor + watchdog + analyst)
#   USER_WATCHDOG_ONLY - runs only user_alert_watchdog.py

set -e

MODE="${1:-}"

if [ "$MODE" = "AGENTS_ONLY" ]; then
    echo ">>> AGENTS_ONLY mode - starting Forensic Janitor + APOC Watchdog + Daily Analyst..."
    exec python agents.py

elif [ "$MODE" = "USER_WATCHDOG_ONLY" ]; then
    echo ">>> USER_WATCHDOG_ONLY mode - starting user_alert_watchdog.py..."
    exec python user_alert_watchdog.py

else
    echo ">>> ALL_AGENTS mode - starting all scheduled tasks..."
    # Run all agents in parallel
    python user_alert_watchdog.py &
    pid1=$!
    python agents.py &
    pid2=$!

    # Wait for any to exit
    wait -n
    exit_code=$?

    # Kill the other on exit
    kill $pid1 2>/dev/null || true
    kill $pid2 2>/dev/null || true
    exit $exit_code
fi