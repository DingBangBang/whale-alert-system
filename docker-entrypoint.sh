#!/usr/bin/env bash
#
# Bootstrap entrypoint for the whale-checker container.
#
# Goal: `docker compose up -d --build` on a fresh clone should immediately show a
# populated, continuously-updating Grafana dashboard. It does this in three steps:
#
#   1. Seed  — copy the committed snapshot (seed/whale_alert.db) into the live
#              database on first boot, so the dashboard has data before any API
#              key is even configured.
#   2. Fill  — one-time historical backfill of the newest BOOTSTRAP_BLOCKS blocks
#              (default 2000) once, so a fresh clone shows a rich dataset. Skipped
#              on later restarts and when no ETHERSCAN_API_KEY is configured.
#   3. Poll  — hand over to the normal polling loop (whale_alert.py) so the
#              dashboard keeps updating with new blocks.
#
set -euo pipefail

cd /app
mkdir -p /app/data

DB_PATH="${WHALE_DB_PATH:-/app/data/whale_alert.db}"
SEED_DB="/app/seed/whale_alert.db"
BACKFILL_MARKER="/app/data/.bootstrap_backfilled"

# --- 1) Seed the live database from the committed snapshot --------------------
if [ ! -s "$DB_PATH" ] && [ -f "$SEED_DB" ]; then
  cp "$SEED_DB" "$DB_PATH"
  echo "[entrypoint] seeded $DB_PATH from snapshot $SEED_DB"
else
  echo "[entrypoint] existing database found at $DB_PATH; keeping current data"
fi

# --- 2) One-time historical backfill -----------------------------------------
if [ -n "${ETHERSCAN_API_KEY:-}" ]; then
  if [ ! -f "$BACKFILL_MARKER" ] || [ "${FORCE_BACKFILL:-0}" = "1" ]; then
    echo "[entrypoint] backfilling the newest ${BOOTSTRAP_BLOCKS:-2000} blocks (one-time)…"
    if python backfill.py --blocks "${BOOTSTRAP_BLOCKS:-2000}" --no-profile; then
      touch "$BACKFILL_MARKER"
    else
      echo "[entrypoint] backfill failed (API quota / network); continuing to the polling loop"
    fi
  else
    echo "[entrypoint] backfill already completed on a previous boot; skipping"
  fi
else
  echo "[entrypoint] ETHERSCAN_API_KEY not set — serving the snapshot only;"
  echo "[entrypoint] add a key to 'environment .env' for live, continuously-updating data"
fi

# --- 3) Hand over to the continuous polling loop -----------------------------
echo "[entrypoint] starting: $*"
exec "$@"
