"""Etherscan API client + pure whale-detection logic.

The module is deliberately split into two concerns:

* ``fetch_*`` helpers talk to the public Etherscan API (network I/O).
* ``detect_whales`` / ``classify_direction`` are pure functions with no I/O, which
  makes them trivial to unit-test (see ``tests/test_whale_alert.py``).

The client uses the **Etherscan API V2** endpoint (V1 is deprecated and now returns
a migration error) and requires the free ``proxy/eth_getBlockByNumber`` endpoint to
read raw ETH transfers, because ``account/txlistinternal`` is a Pro-only endpoint.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import requests

import config

logger = logging.getLogger("whale.etherscan")

# Known exchange / custodian addresses used to classify capital flow direction.
# These are partial well-known public addresses. The set is intentionally small so
# that the dashboard's "inflow / outflow / peer-to-peer" split is meaningful while
# remaining maintainable.
EXCHANGE_ADDRESSES = {
    # Binance hot / deposit wallets
    "0x28c6c06298d514db089934071355e5743bf21d60",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
    "0x503828976d22510aad0201ac7ec88293211d23da",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
}


def get_eth_price_usd(api_key: str, timeout: int = 15) -> float:
    """Return the current ETH/USD price from the Etherscan ``ethprice`` endpoint."""
    payload = {
        "module": "stats",
        "action": "ethprice",
    }
    data = _get(payload, api_key=api_key, timeout=timeout)
    try:
        return float(data["result"]["ethusd"])
    except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - network data
        logger.error("Could not parse ETH price from Etherscan response: %s", exc)
        return 0.0


def get_latest_block(api_key: str, timeout: int = 15) -> int:
    """Return the latest Ethereum block number via the Etherscan proxy endpoint."""
    payload = {
        "module": "proxy",
        "action": "eth_blockNumber",
    }
    data = _get(payload, api_key=api_key, timeout=timeout)
    try:
        return int(data["result"], 16)  # result is a hex-encoded block number
    except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover
        logger.error("Could not read latest block from Etherscan: %s", exc)
        raise
def fetch_block_transfers_single(api_key: str, block_number: int) -> List[Dict[str, Any]]:
    """Return the value-bearing ETH transfers of a single block."""
    payload = {
        "module": "proxy",
        "action": "eth_getBlockByNumber",
        "tag": hex(block_number),
        "boolean": "true",
    }
    data = _get(payload, api_key=api_key)
    result = data.get("result")
    # Etherscan may return a plain string (rate-limit / error) instead of an object.
    if not isinstance(result, dict):
        return []
    ts_raw = int(result.get("timestamp", "0x0") or "0x0", 16)

    transfers: List[Dict[str, Any]] = []
    for tx in result.get("transactions", []) or []:
        # Skip contract creations (no `to`) and zero-value calls.
        if not tx.get("to"):
            continue
        try:
            value_wei = int(tx.get("value", "0x0") or "0x0", 16)
        except (TypeError, ValueError):
            value_wei = 0
        if value_wei <= 0:
            continue
        transfers.append(
            {
                "hash": tx.get("hash", ""),
                "block_number": block_number,
                "timestamp": ts_raw,
                "from": tx.get("from", "").lower(),
                "to": tx.get("to", "").lower(),
                "value_wei": value_wei,
                "value_eth": value_wei / 1e18,
            }
        )
    return transfers


def fetch_block_transfers(api_key: str, start_block: int, end_block: int) -> List[Dict[str, Any]]:
    """Read raw ETH transfers from a range of blocks via ``proxy/eth_getBlockByNumber``.

    Returns a list of normalised transfer records. Only externally-visible
    ``value`` transfers (i.e. ETH actually moving between addresses) are kept; each
    block costs one API request, so keep the window small on the free tier.
    """
    transfers: List[Dict[str, Any]] = []
    for block in range(start_block, end_block + 1):
        transfers.extend(fetch_block_transfers_single(api_key, block))
    return transfers


def classify_direction(from_addr: str, to_addr: str) -> str:
    """Classify a transfer as exchange inflow / outflow or peer-to-peer.

    Returns one of ``"exchange_inflow"``, ``"exchange_outflow"``, ``"peer_to_peer"``.
    """
    from_l = (from_addr or "").lower()
    to_l = (to_addr or "").lower()
    if to_l in EXCHANGE_ADDRESSES:
        return "exchange_inflow"
    if from_l in EXCHANGE_ADDRESSES:
        return "exchange_outflow"
    return "peer_to_peer"
def detect_whales(
    transfers: List[Dict[str, Any]],
    threshold_usd: float,
    eth_price_usd: float,
) -> List[Dict[str, Any]]:
    """Filter ``transfers`` down to whale-sized ones.

    A record is a 'whale' when its USD value (computed from ``value_eth`` and the
    supplied ``eth_price_usd``) is greater than or equal to ``threshold_usd``.

    Each returned record is enriched with ``value_usd`` and ``direction`` while the
    input records are never modified.

    Args:
        transfers: list of transfer dicts each containing at least ``value_eth``,
            ``from`` and ``to``.
        threshold_usd: minimum USD value to qualify.
        eth_price_usd: price of ETH in USD used for conversion.

    Returns:
        New list of whale transfer records (each with ``value_usd`` and ``direction``).
    """
    whales: List[Dict[str, Any]] = []
    for tx in transfers:
        value_eth = float(tx.get("value_eth", 0.0) or 0.0)
        value_usd = value_eth * eth_price_usd
        if value_usd >= threshold_usd:
            enriched = dict(tx)
            enriched["value_usd"] = round(value_usd, 2)
            enriched["direction"] = classify_direction(tx.get("from", ""), tx.get("to", ""))
            whales.append(enriched)
    return whales


def scan_recent_whales(api_key: str, threshold_usd: float, window: int) -> List[Dict[str, Any]]:
    """Run one full scan: fetch price + latest block + recent transfers, detect whales.

    This is the high-level orchestration used by the CLI entry-point.
    """
    eth_price_usd = get_eth_price_usd(api_key)
    latest = get_latest_block(api_key)
    start = max(latest - window, 0)

    logger.info(
        "Scanning blocks %s..%s (ETH/USD = %s, threshold = $%s)",
        start, latest, eth_price_usd, threshold_usd,
    )
    transfers = fetch_block_transfers(api_key, start, latest)
    whales = detect_whales(transfers, threshold_usd=threshold_usd, eth_price_usd=eth_price_usd)
    return whales


def _get(payload: Dict[str, str], api_key: str, timeout: int = 15, retries: int = 5) -> Dict[str, Any]:
    """Perform a single GET against the Etherscan V2 API with a rate-limit break.

    Retries transparently when Etherscan returns a rate-limit message (free tier is
    ~5 req/s; concurrent callers often trigger it).
    """
    params: Dict[str, str] = {
        **payload,
        "chainid": str(config.CHAIN_ID),
        "apikey": api_key,
    }
    for attempt in range(1, retries + 1):
        try:
            time.sleep(config.BASE_RATE_LIMIT_DELAY)
            resp = requests.get(config.ETHERSCAN_API_BASE_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:  # pragma: no cover - network flakiness
            logger.error("Etherscan request failed for %s: %s", payload.get("action"), exc)
            if attempt == retries:
                raise
            time.sleep(attempt)  # back-off before retrying
            continue

        result = data.get("result")
        if isinstance(result, str) and "rate limit" in result.lower():
            logger.warning("Rate-limited on %s (attempt %s); backing off…",
                           payload.get("action"), attempt)
            time.sleep(max(attempt, 1))
            continue
        return data
    raise RuntimeError(f"Etherscan request failed after {retries} retries for {payload.get('action')}")