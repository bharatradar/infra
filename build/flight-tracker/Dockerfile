FROM python:3.11-slim

LABEL maintainer="bharatradar"
LABEL description="BharatRadar Flight Tracker - K8s Deployment"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py config.py db.py utils.py fr24_data.py binCraft_decoder.py ./
COPY data/ ./data/

# Create logs directory
RUN mkdir -p /app/logs

# Environment setup
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import asyncio; print('OK')" || exit 1

# Run flight tracker
CMD ["python", "-u", "main.py"]