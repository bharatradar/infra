FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py db.py agents.py user_alert_watchdog.py start.sh ./

RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1

# Run both agents in parallel using bash -c
CMD ["bash", "-c", "python user_alert_watchdog.py & python agents.py & wait"]