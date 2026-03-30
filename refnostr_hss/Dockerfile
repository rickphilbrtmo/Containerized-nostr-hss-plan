FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev && rm -rf /var/lib/apt/lists/*

# Install python-diameter (local package — build context is supers/)
COPY /diameter /diameter/
RUN pip3.11 install --no-cache-dir python-diameter

# Install runtime dependencies
RUN pip3.11 install --no-cache-dir \
    fastapi "uvicorn[standard]" httpx pydantic \
    coincurve websocket-client requests cryptography

# Copy application
COPY nostr-hss /app/

# Setup data volume and env defaults
VOLUME ["/data"]
ENV DB_PATH=/data/hss.db \
    PALLETS_FILE=/app/pallets.json \
    SUBSCRIBERS_FILE=/app/subscribers.json

# CMD is set per-service in docker-compose
