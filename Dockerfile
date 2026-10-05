# Whale Alert System — Python checker image
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY config.py etherscan_client.py database.py whale_alert.py backfill.py ./
COPY src ./src
# Committed snapshot database: seeded into ./data on first boot so the dashboard
# is populated out-of-the-box (see docker-entrypoint.sh).
COPY seed ./seed
RUN mkdir -p data

# Bootstrap script: seeds the DB, does a one-time backfill, then starts polling.
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Environment values (API key, threshold, poll interval) are injected at runtime by
# docker-compose via `env_file`. No secrets are baked into this image.
ENV WHALE_DB_PATH=/app/data/whale_alert.db

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "whale_alert.py"]