"""Whale address profiler.

For every whale address observed in ``whale_transfers`` this module queries the
Etherscan V2 API to build a rich "on-chain identity card" and stores it in the
``address_profiles`` table. A senior on-chain analyst would want to know:

* current ETH balance and its USD exposure
* major stablecoin (USDT / USDC / DAI) holdings
* transaction velocity (24h / 7d) and activity recency
* inbound vs outbound ETH flows and typical / largest transfer size
* whether the address is a smart contract (e.g. an exchange/relayer)
* a human-readable address tag (Etherscan Metadata) when available

Usage
-----
python -m src.address_profiler --force     # re-profile all whale addresses
python -m src.address_profiler --limit 20  # profile at most 20 already-known ones
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the project root importable when launched directly (e.g. `python -m src.address_profiler`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from database import WhaleDatabase  # noqa: E402
from etherscan_client import _get, get_eth_price_usd  # noqa: E402

logger = logging.getLogger("whale.address_profiler")

# Canonical ERC-20 contracts on Ethereum mainnet + their decimals.
# source: stablecoin token metadata (Goldsky). Amounts use `decimals` for scaling.
ERC20_TOKENS: Dict[str, Dict[str, float]] = {
    "USDT": {
        "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "decimals": 6.0,
        "usd_factor": 1.0,  # ~1 USDT = 1 USD
    },
    "USDC": {
        "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "decimals": 6.0,
        "usd_factor": 1.0,
    },
    "DAI": {
        "address": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "decimals": 18.0,
        "usd_factor": 1.0,
    },
}


# --- Low level Etherscan V2 calls --------------------------------------------
def fetch_eth_balance(api_key: str, address: str) -> float:
    """Current ETH balance (wei -> ETH)."""
    data = _get({"module": "account", "action": "balance", "address": address}, api_key=api_key)
    return _to_eth_float(data.get("result"))


def fetch_token_balance(api_key: str, address: str, token: str) -> float:
    """ERC-20 token balance in token units for ``token`` (USDT/USDC/DAI)."""
    cfg = ERC20_TOKENS[token]
    data = _get(
        {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": cfg["address"],
            "address": address,
        },
        api_key=api_key,
    )
    return _to_float_scaled(data.get("result"), cfg["decimals"])


def fetch_txlist(api_key: str, address: str, page: int = 1, offset: int = 100, sort: str = "desc") -> List[Dict[str, Any]]:
    """Recent transactions for an address.

    ``sort="desc"`` (default) returns the *newest* transactions first — important
    for velocity stats and feeds, since Etherscan otherwise returns oldest-first.
    """
    data = _get(
        {
            "module": "account",
            "action": "txlist",
            "address": address,
            "page": page,
            "offset": offset,
            "sort": sort,
        },
        api_key=api_key,
    )
    result = data.get("result")
    return result if isinstance(result, list) else []


def fetch_address_tag(api_key: str, address: str) -> str:
    """Return a human-readable label for the address (Etherscan Metadata), if any."""
    try:
        data = _get({"module": "account", "action": "getaddresstag", "address": address}, api_key=api_key)
        result = data.get("result")
        if isinstance(result, dict):
            return result.get("NameTag") or result.get("Tag") or ""
        return ""
    except Exception:  # pragma: no cover - tag lookup is best effort
        return ""


def probe_is_contract(api_key: str, address: str) -> Optional[bool]:
    """Best-effort check whether the address holds contract code (is a smart contract)."""
    try:
        data = _get(
            {"module": "contract", "action": "getsourcecode", "address": address}, api_key=api_key
        )
        result = data.get("result")
        # Etherscan returns a dict with the source when the address is a contract.
        if isinstance(result, dict) and result.get("SourceCode"):
            return True
        return False
    except Exception:  # pragma: no cover
        return None


# --- Scaling helpers ----------------------------------------------------------
def _to_eth_float(raw: Any) -> float:
    """wei -> ETH."""
    return _to_float_scaled(raw, 18.0)


def _to_float_scaled(raw: Any, decimals: float) -> float:
    try:
        return float(str(raw).strip()) / (10.0 ** decimals)
    except (TypeError, ValueError):
        return 0.0


def _sum_wei(rows: List[Dict[str, Any]], key: str, match: str) -> float:
    total = 0
    for row in rows:
        if str(row.get(key, "")).lower() == match.lower():
            try:
                total += float(row.get("value", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total / 1e18


def profile_address(api_key: str, address: str, direction: str, eth_price_usd: float) -> Dict[str, Any]:
    """Build the full on-chain profile for a single address.

    The profile is a senior-analyst oriented view: balances, stablecoin holdings,
    tx velocity, capital-flow skew, contract-ness and a human label.
    """
    addr = address.lower()
    txs = fetch_txlist(api_key, addr)
    now = int(time.time())
    day = 86400

    tx_24h = sum(1 for t in txs if int(t.get("timeStamp", 0) or 0) >= now - day)
    tx_7d = sum(1 for t in txs if int(t.get("timeStamp", 0) or 0) >= now - 7 * day)
    timestamps = [int(t.get("timeStamp", 0) or 0) for t in txs if t.get("timeStamp")]
    last_active = max(timestamps) if timestamps else None

    inflow_eth = _sum_wei(txs, "to", addr)
    outflow_eth = _sum_wei(txs, "from", addr)
    values_eth = [float(t.get("value", 0) or 0) / 1e18 for t in txs if t.get("to")]
    values_eth = [v for v in values_eth if v > 0]

    eth_balance = fetch_eth_balance(api_key, addr)
    token_balances = {token: fetch_token_balance(api_key, addr, token) for token in ERC20_TOKENS}
    stablecoin_usd = sum(
        token_balances[token] * ERC20_TOKENS[token]["usd_factor"] for token in ERC20_TOKENS
    )

    profile = {
        "address": addr,
        "direction": direction,
        "eth_balance": round(eth_balance, 6),
        "usdt_balance": round(token_balances["USDT"], 6),
        "usdc_balance": round(token_balances["USDC"], 6),
        "dai_balance": round(token_balances["DAI"], 6),
        "stablecoin_usd": round(stablecoin_usd, 2),
        "tx_count_24h": tx_24h,
        "tx_count_7d": tx_7d,
        "total_txs_observed": len(txs),
        "last_active_ts": last_active,
        "inflow_eth": round(inflow_eth, 4),
        "outflow_eth": round(outflow_eth, 4),
        "net_eth_flow": round(inflow_eth - outflow_eth, 4),
        "avg_tx_value_eth": round((sum(values_eth) / len(values_eth)) if values_eth else 0.0, 4),
        "max_tx_value_eth": round(max(values_eth) if values_eth else 0.0, 4),
        "is_contract": probe_is_contract(api_key, addr),
        "address_tag": fetch_address_tag(api_key, addr),
        "eth_usd_exposure": round(eth_balance * eth_price_usd, 2),
        "tokens_json": json.dumps(token_balances),
        "profiled_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    return profile


def profile_whale_addresses(api_key: str, db: WhaleDatabase, limit: Optional[int] = None, force: bool = False) -> int:
    """Profile all (or new) whale addresses in the DB.

    Returns the number of addresses actually profiled this run. When ``force`` is
    False only addresses without an existing profile are fetched (fast incremental).
    """
    addresses = db.get_whale_addresses()
    if not force:
        known = db.get_profiled_addresses()
        addresses = [a for a in addresses if a not in known]
    if limit:
        addresses = addresses[:limit]

    if not addresses:
        logger.info("No whale addresses to profile (all already profiled; use --force).")
        return 0

    eth_price = get_eth_price_usd(api_key)
    logger.info("Profiling %s addresses (ETH/USD = %s)…", len(addresses), eth_price)

    for i, address in enumerate(addresses, 1):
        # A direction hint for the profile comes from the first whale row involving it.
        hint = _direction_hint(db, address)
        try:
            profile = profile_address(api_key, address, hint, eth_price)
            db.upsert_address_profile(profile)
            logger.info("[%s/%s] %s tag=%r eth=%s txs24h=%s contracts=%s",
                        i, len(addresses), address[:12], profile["address_tag"],
                        profile["eth_balance"], profile["tx_count_24h"], profile["is_contract"])
        except Exception as exc:  # pragma: no cover - keep going on per-address failure
            logger.warning("Failed to profile %s: %s", address[:12], exc)
    return len(addresses)


def _direction_hint(db: WhaleDatabase, address: str) -> str:
    row = db.conn.execute(
        "SELECT direction FROM whale_transfers WHERE from_address=? OR to_address=? LIMIT 1",
        (address, address),
    ).fetchone()
    return row[0] if row else "peer_to_peer"


def main() -> None:
    parser = argparse.ArgumentParser(description="Whale address profiler")
    parser.add_argument("--force", action="store_true", help="re-profile already-known addresses")
    parser.add_argument("--limit", type=int, default=None, help="max addresses to profile")
    parser.add_argument("--address", type=str, default=None, help="profile a single address")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    api_key = config.ETHERSCAN_API_KEY
    if not api_key:
        raise SystemExit("ETHERSCAN_API_KEY not set in 'environment .env'")

    db = WhaleDatabase()
    try:
        if args.address:
            profile = profile_address(api_key, args.address, "peer_to_peer", get_eth_price_usd(api_key))
            db.upsert_address_profile(profile)
            print(json.dumps(profile, indent=2, ensure_ascii=False))
            return
        profiled = profile_whale_addresses(api_key, db, limit=args.limit, force=args.force)
        logger.info("Profiled %s addresses (total in DB: %s)", profiled, db.profile_count())
    finally:
        db.close()


if __name__ == "__main__":
    main()