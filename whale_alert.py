"""Whale Alert System — main entry point.

Polls the Etherscan API for large ETH transfers, writes any whale-sized
transactions into the SQLite database and prints an alert to the terminal.

Usage
-----
Local run (single scan):   python whale_alert.py --once
Local run (continuous):    python whale_alert.py
Inside Docker (compose):   python whale_alert.py
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import List

import config
from database import WhaleDatabase
from etherscan_client import get_eth_price_usd, scan_recent_whales

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("whale.main")


def print_alerts(whales: List[dict], threshold_usd: float) -> None:
    """Print whale alerts to the terminal in a readable block."""
    if not whales:
        return
    print("\n" + "=" * 72)
    print(f"  🐋  {len(whales)} 笔巨鲸交易触发（阈值 ≥ ${threshold_usd:,.0f} USD）")
    print("=" * 72)
    for tx in whales:
        print(
            f"  [{tx['direction']}] {tx.get('value_eth', 0):,.2f} ETH "
            f"≈ ${tx.get('value_usd', 0):,.2f} USD"
        )
        print(f"    from: {tx.get('from', '')}")
        print(f"    to:   {tx.get('to', '')}")
        print(f"    hash: {tx.get('hash', '')}")
    print("=" * 72 + "\n")


def run_scan(db: WhaleDatabase, once: bool) -> None:
    """Execute one scan cycle: fetch, detect, persist and print."""
    api_key = config.ETHERSCAN_API_KEY
    if not api_key:
        logger.error("ETHERSCAN_API_KEY not set. Add it to 'environment .env'.")
        if once:
            raise SystemExit(1)
        return

    try:
        whales = scan_recent_whales(
            api_key=api_key,
            threshold_usd=config.WHALE_THRESHOLD_USD,
            window=config.SCAN_BLOCK_WINDOW,
        )
    except Exception as exc:  # pragma: no cover - network resilience
        logger.error("Scan failed: %s", exc)
        if once:
            raise
        return

    saved = 0
    for whale in whales:
        if db.insert_whale(whale):
            saved += 1

    print_alerts(whales, config.WHALE_THRESHOLD_USD)
    price = 0.0
    try:
        price = get_eth_price_usd(config.ETHERSCAN_API_KEY)
        db.record_price_tick(price)  # sample for the dashboard ETH-price curve
    except Exception:  # pragma: no cover
        pass
    logger.info("入库 %s 笔新巨鲸交易（累计 %s 笔），ETH = $%s", saved, db.count(), price)

    _profile_new_addresses(db, api_key)


def _profile_new_addresses(db: WhaleDatabase, api_key: str) -> None:
    """Profile whale addresses added this cycle (never blocks the main loop)."""
    try:
        from src.address_profiler import profile_whale_addresses
        count = profile_whale_addresses(api_key, db)
        if count:
            logger.info("Profiled %s new whale addresses (total %s).", count, db.profile_count())
    except Exception as exc:  # pragma: no cover
        logger.warning("Address profiling skipped: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="On-chain whale behaviour alert system")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scan and exit (useful for tests / cron).",
    )
    args = parser.parse_args()

    db = WhaleDatabase()
    logger.info("Using database: %s", db.path)

    try:
        if args.once:
            run_scan(db, once=True)
            return
        while True:
            run_scan(db, once=False)
            logger.info("Sleeping for %ss before next scan…", config.POLL_INTERVAL_SECONDS)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        db.close()


if __name__ == "__main__":
    main()