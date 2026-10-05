"""Centralised configuration loader.

Reads environment variables from the local env file (the file the user created is
literally named ``environment .env``) and exposes typed configuration values.

Priority for locating the env file:
  1. A file named ``environment .env`` in the project root (created by the user).
  2. A standard ``.env`` file in the project root.
  3. Plain OS environment variables (this is what happens inside Docker, where
     docker-compose injects the values via ``env_file``).

All downstream modules import the singletons in this module so they can be easily
mocked in tests.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# The user pre-created this file with real values, so we prefer it.
_PREFERRED_ENV_FILE = PROJECT_ROOT / "environment .env"
_STANDARD_ENV_FILE = PROJECT_ROOT / ".env"

if _PREFERRED_ENV_FILE.exists():
    load_dotenv(dotenv_path=str(_PREFERRED_ENV_FILE))
elif _STANDARD_ENV_FILE.exists():
    load_dotenv(dotenv_path=str(_STANDARD_ENV_FILE))


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# --- API ---------------------------------------------------------------------
# Etherscan migrated to the V2 endpoint (V1 now returns a deprecation notice).
ETHERSCAN_API_BASE_URL = os.getenv(
    "ETHERSCAN_API_BASE_URL", "https://api.etherscan.io/v2/api"
)
# Chain id for the network to monitor. 1 = Ethereum Mainnet.
CHAIN_ID = _get_int("CHAIN_ID", 1)
# NOTE: set ETHERSCAN_API_KEY in "environment .env". No default is provided on
# purpose so a missing key fails loudly instead of silently returning nothing.
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()

# --- Threshold ----------------------------------------------------------------
# The minimum transfer size (in USD) that qualifies as a "whale" transaction.
# (Lowered from $10M during the optimisation pass to capture more whale activity.)
WHALE_THRESHOLD_USD = _get_int("WHALE_THRESHOLD_USD", 500_000)

# --- Runtime ------------------------------------------------------------------
# How long the checker sleeps between poll cycles.
POLL_INTERVAL_SECONDS = _get_int("POLL_INTERVAL_SECONDS", 60)

# Number of newest blocks scanned per cycle. Each block is one Etherscan request,
# so this trades fresh-data coverage against the free-tier daily call budget.
# (Etherscan free: ~5 req/s, 100k req/day. 20 blk x 1440 polls ≈ 29k req/day.)
SCAN_BLOCK_WINDOW = _get_int("SCAN_BLOCK_WINDOW", 20)

# Incremental mode: the poller resumes from the last scanned block and only fetches
# *new* blocks, accumulating data over time. If the node/database has been offline
# long enough that the backlog exceeds this many blocks, the scan is capped to this
# span (the newest SCAN_MAX_BLOCK_SPAN blocks) to protect the free-tier budget.
SCAN_MAX_BLOCK_SPAN = _get_int("SCAN_MAX_BLOCK_SPAN", 2000)

BASE_RATE_LIMIT_DELAY = 0.25  # seconds between Etherscan requests (respects ~5 req/s).

# --- Storage -----------------------------------------------------------------
# Shared with the Grafana container via a bind-mount volume `./data:/data`.
DB_PATH = os.getenv("WHALE_DB_PATH", str(PROJECT_ROOT / "data" / "whale_alert.db"))

# --- Backfill ----------------------------------------------------------------
# Number of blocks fetched concurrently during a backfill page. Lower this to 1 on
# strict free tiers to avoid HTTP 429 rate limiting at the cost of speed.
BACKFILL_WORKERS = _get_int("BACKFILL_WORKERS", 3)