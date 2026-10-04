"""One-time historical backfill with automatic pagination.

Backfills a bounded block range (``startblock``..``endblock``) by iterating the
range in slices of ``--offset`` (default 1000) blocks — i.e. each "page" covers up
to 1000 blocks and the loop automatically advances until the full requested range
has been scanned. Every ETH transfer found in the range is evaluated against the
USD threshold and persisted to ``whale_alert.db``.

Design note: Etherscan's ``account/txlistinternal`` (native startblock/endblock
bulk query) is a Pro-only endpoint on the free tier, so we page over blocks using
the free ``proxy/eth_getBlockByNumber`` call (one call per block per page). Each
page confirms completion by tracking the last block of the range.

Usage
-----
python backfill.py --blocks 2500              # backfill last N blocks
python backfill.py --start 26100000 --end 26106000 --offset 1000
python backfill.py --blocks 1000 --no-profile # skip address profiling
"""
from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from database import WhaleDatabase
from etherscan_client import (
    detect_whales,
    fetch_block_transfers_single,
    get_eth_price_usd,
    get_latest_block,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("whale.backfill")

# Concurrency used per block fetch page. Keep workers modest to respect Etherscan's
# free-tier 5 req/s rate limit (each request also sleeps BASE_RATE_LIMIT_DELAY).
# Tunable via the BACKFILL_WORKERS env var (set to 1 for the strictest throttling).
MAX_WORKERS = config.BACKFILL_WORKERS

# Default number of blocks handled per request page.
DEFAULT_OFFSET = 1000


def _fetch_slice(api_key: str, start: int, end: int) -> tuple:
    """Fetch transfer records for a block range [start..end] (<= offset blocks).

    Returns ``(transfers, scanned_blocks)``. Failures on individual blocks are
    logged and skipped so a bad block cannot abort the whole backfill.
    """
    transfers = []
    scanned = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(fetch_block_transfers_single, api_key, block)
            for block in range(start, end + 1)
        ]
        for future in as_completed(futures):
            try:
                transfers.extend(future.result())
                scanned += 1
            except Exception as exc:  # pragma: no cover - network resilience
                logger.warning("Block fetch failed (%s); skipping", exc)
    return transfers, scanned


def run_backfill(
    start_block: int,
    end_block: int,
    offset: int = DEFAULT_OFFSET,
    profile: bool = True,
) -> int:
    """Backfill ``[start_block, end_block]`` in auto-paginated slices of ``offset`` blocks.

    Returns the number of newly inserted whale transfers.
    """
    api_key = config.ETHERSCAN_API_KEY
    if not api_key:
        raise SystemExit("ETHERSCAN_API_KEY not set in 'environment .env'")

    eth_price = get_eth_price_usd(api_key)
    logger.info("Backfill ETH/USD = %s (threshold = $%s)", eth_price, config.WHALE_THRESHOLD_USD)

    db = WhaleDatabase()
    inserted = 0
    cursor = start_block
    total_blocks = end_block - start_block + 1
    page_no = 0
    try:
        while cursor <= end_block:  # auto-pagination loop
            page_end = min(cursor + offset - 1, end_block)
            page_no += 1
            page_start = time.time()

            transfers, scanned = _fetch_slice(api_key, cursor, page_end)
            db.record_price_tick(eth_price)  # feed the dashboard ETH-price curve

            new_in_page = 0
            for whale in detect_whales(
                transfers, threshold_usd=config.WHALE_THRESHOLD_USD, eth_price_usd=eth_price
            ):
                if db.insert_whale(whale):
                    new_in_page += 1
            inserted += new_in_page

            logger.info(
                "Page %s: blocks %s..%s (scanned %s txs) -> +%s whales (total %s) in %.1fs",
                page_no, cursor, page_end, scanned, new_in_page, inserted, time.time() - page_start,
            )

            if page_end >= end_block:
                break
            cursor = page_end + 1  # advance to the next page

        logger.info("Backfill done: %s pages, %s blocks, +%s whales (total %s)",
                    page_no, total_blocks, inserted, db.count())

        if profile:
            _profile_addresses(db, api_key)
        return inserted
    finally:
        db.close()


def _profile_addresses(db: WhaleDatabase, api_key: str) -> None:
    from src.address_profiler import profile_whale_addresses
    try:
        profiled = profile_whale_addresses(api_key, db)
        logger.info("Backfill phase 2: profiled %s whale addresses (total %s).",
                    profiled, db.profile_count())
    except Exception as exc:  # pragma: no cover
        logger.warning("Address profiling after backfill failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical whale backfill (auto-paginated)")
    parser.add_argument("--start", type=int, default=None, help="first block (startblock)")
    parser.add_argument("--end", type=int, default=None, help="last block (endblock)")
    parser.add_argument("--blocks", type=int, default=None, help="override: last N blocks")
    parser.add_argument("--offset", type=int, default=DEFAULT_OFFSET,
                        help="blocks handled per request page (default 1000)")
    parser.add_argument("--no-profile", action="store_true", help="skip address profiling")
    args = parser.parse_args()

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

    if start_block > end_block:
        raise SystemExit(f"start_block {start_block} > end_block {end_block}")

    logger.info("Backfilling blocks %s..%s in pages of %s blocks",
                start_block, end_block, args.offset)
    run_backfill(start_block, end_block, offset=args.offset, profile=not args.no_profile)


if __name__ == "__main__":
    main()