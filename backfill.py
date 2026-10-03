"""One-time historical backfill to seed the dashboard with real whale data.

The live checker only scans a small recent window each cycle, so detecting $10M+
whale transfers can take a while. This utility concurrently scans a wider block
range and persists any whale-sized transfers into the same SQLite database.

Usage
-----
python backfill.py                         # backfill the last BACKFILL_BLOCKS blocks
python backfill.py --start 20000000 --end 20100000
"""
from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from database import WhaleDatabase
from etherscan_client import (
    detect_whales,
    fetch_block_transfers_single,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("whale.backfill")

# Concurrency used per block fetch. Keep workers modest to respect Etherscan's
# free-tier 5 req/s rate limit (each request also sleeps BASE_RATE_LIMIT_DELAY).
MAX_WORKERS = 3


def run_backfill(start_block: int, end_block: int) -> int:
    api_key = config.ETHERSCAN_API_KEY
    if not api_key:
        raise SystemExit("ETHERSCAN_API_KEY not set in 'environment .env'")

    eth_price = _eth_price_for_backfill(api_key)
    db = WhaleDatabase()
    inserted = 0
    scanned = 0
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [
                pool.submit(fetch_block_transfers_single, api_key, block)
                for block in range(start_block, end_block + 1)
            ]
            for future in as_completed(futures):
                try:
                    transfers = future.result()
                except Exception as exc:  # pragma: no cover - network resilience
                    logger.warning("Block fetch failed (%s); skipping", exc)
                    continue
                scanned += 1
                for whale in detect_whales(
                    transfers, threshold_usd=config.WHALE_THRESHOLD_USD, eth_price_usd=eth_price
                ):
                    if db.insert_whale(whale):
                        inserted += 1
                if scanned % 50 == 0:
                    logger.info("Scanned %s blocks, inserted %s whales…", scanned, inserted)
        logger.info("Backfill done: scanned %s blocks, inserted %s whales (total %s)",
                    scanned, inserted, db.count())
        return inserted
    finally:
        db.close()


def _eth_price_for_backfill(api_key: str) -> float:
    from etherscan_client import get_eth_price_usd
    price = get_eth_price_usd(api_key)
    logger.info("ETH/USD = %s for USD threshold conversion", price)
    return price


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical whale backfill")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--blocks", type=int, default=None, help="overrides --start/--end")
    args = parser.parse_args()

    from etherscan_client import get_latest_block

    latest = get_latest_block(config.ETHERSCAN_API_KEY)

    if args.end is None:
        end_block = latest
    else:
        end_block = args.end

    if args.blocks is not None:
        start_block = max(end_block - args.blocks, 0)
    elif args.start is None:
        start_block = max(end_block - config.SCAN_BLOCK_WINDOW, 0)
    else:
        start_block = args.start

    logger.info("Backfilling blocks %s..%s (%s blocks)", start_block, end_block, end_block - start_block)
    run_backfill(start_block, end_block)


if __name__ == "__main__":
    main()