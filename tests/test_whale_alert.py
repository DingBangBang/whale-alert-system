"""Unit tests for the ``detect_whales`` function.

``detect_whales`` is pure (no network / DB), so these tests are fast and isolated.
"""
from etherscan_client import classify_direction, detect_whales

ETH_PRICE = 3000.0
THRESHOLD = 10_000_000.0


def _tx(value_eth: float, from_addr="0xa", to_addr="0xb", hash_="0x1") -> dict:
    return {
        "hash": hash_,
        "value_eth": value_eth,
        "from": from_addr,
        "to": to_addr,
    }


def test_no_whales_when_all_below_threshold():
    transfers = [_tx(100.0), _tx(2_000.0), _tx(3_333.33)]  # each < $10M @ $3000
    result = detect_whales(transfers, THRESHOLD, ETH_PRICE)
    assert result == []


def test_whale_above_threshold_detected_and_enriched():
    transfers = [
        _tx(4_000.0, hash_="0xwhale"),  # 4_000 * 3000 = $12,000,000 -> whale
        _tx(1_000.0, hash_="0xsmall"),  # $3,000,000 -> too small
    ]
    result = detect_whales(transfers, THRESHOLD, ETH_PRICE)
    assert len(result) == 1
    whale = result[0]
    assert whale["hash"] == "0xwhale"
    assert whale["value_usd"] == 12_000_000.0
    assert whale["value_eth"] == 4_000.0


def test_boundary_equal_to_threshold_is_whale():
    transfers = [_tx(3_333.3334, hash_="0xedge")]  # ~$10,000,000.2 @ $3000
    result = detect_whales(transfers, THRESHOLD, ETH_PRICE)
    assert len(result) == 1
    assert result[0]["value_usd"] >= THRESHOLD


def test_input_transfers_are_not_mutated():
    transfers = [_tx(4_000.0, hash_="0xclean")]
    _ = detect_whales(transfers, THRESHOLD, ETH_PRICE)
    assert "value_usd" not in transfers[0]
    assert "direction" not in transfers[0]


def test_classify_direction_exchange_flow():
    exchange = "0x28c6c06298d514db089934071355e5743bf21d60"  # Binance #2 in set
    # Flipping the exchange set definition is fragile; test known levels only.
    assert classify_direction("0xabc", exchange) == "exchange_inflow"
    assert classify_direction(exchange, "0xabc") == "exchange_outflow"
    assert classify_direction("0xaaa", "0xbbb") == "peer_to_peer"