#!/bin/bash
# BharatRadar Telegram Bot + Watchdog Startup Script
# Runs both bot polling and watchdog in same container

set -eo pipefail

echo "=========================================="
echo "BharatRadar v1.0.36 Starting..."
echo "=========================================="

# Function to handle shutdown nicely
cleanup() {
    echo "Received shutdown signal, stopping processes..."
    kill 0 2>/dev/null
}
trap cleanup SIGTERM SIGINT

# Check if BOT_ONLY env is set (for debugging single process)
if [ "${BOT_ONLY:-false}" = "true" ]; then
    echo ">>> BOT_ONLY mode - starting bot.py only"
    exec python bot.py
fi

# Check if WATCHDOG_ONLY env is set
if [ "${WATCHDOG_ONLY:-false}" = "true" ]; then
    echo ">>> WATCHDOG_ONLY mode - starting watchdog.py only"
    exec python watchdog.py
fi

# Default: Start both processes
echo ">>> Starting Telegram Bot Polling..."
python bot.py &
BOT_PID=$!

echo ">>> Starting Watchdog Service..."
python watchdog.py &
WATCHDOG_PID=$!

echo ">>> Both services started!"
echo "   Bot PID: $BOT_PID"
echo "   Watchdog PID: $WATCHDOG_PID"
echo "=========================================="

# Wait for any process to exit
# If one dies, we should kill the other and exit
wait $BOT_PID
EXIT_CODE=$?
echo "Bot process exited with code $EXIT_CODE"

echo "Stopping Watchdog..."
kill $WATCHDOG_PID 2>/dev/null || true

exit $EXIT_CODE