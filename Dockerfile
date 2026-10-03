# Whale Alert System — Python checker image
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY config.py etherscan_client.py database.py whale_alert.py backfill.py ./
RUN mkdir -p data

# Environment values (API key, threshold, poll interval) are injected at runtime by
# docker-compose via `env_file`. No secrets are baked into this image.
ENV WHALE_DB_PATH=/app/data/whale_alert.db

CMD ["python", "whale_alert.py"]