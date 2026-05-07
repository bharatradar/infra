FROM python:3.11-slim

LABEL maintainer="bharatradar"
LABEL description="BharatRadar Cortex WebApp"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY web_app.py .
COPY web_app_db.py .
COPY config.py .
COPY resolver.py .
COPY auth.py .
COPY core_ai_pipeline.py .
COPY bot_router_mcp_client.py .

# Copy data directory (airports, airlines, etc.)
COPY data ./data

# Copy static files for web UI
COPY static ./static

# Expose port
EXPOSE 8081

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8081/api/filters || exit 1

# Run uvicorn
CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8081"]
