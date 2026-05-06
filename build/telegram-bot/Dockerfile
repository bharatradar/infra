FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Install pywebpush for web push notifications
RUN pip install --no-cache-dir pywebpush cryptography

COPY bot.py config.py bot_router_mcp_client.py core_ai_pipeline.py mcp_server.py delay_predictor.py watchdog.py start.sh utils.py /app/
RUN mkdir -p /app/logs
RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Default: start both bot and watchdog
CMD ["/app/start.sh"]