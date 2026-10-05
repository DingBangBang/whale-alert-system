"""Tests for the incremental accumulation layer.

Covers the ``whale_transfers.tx_hash`` UNIQUE + ``INSERT OR IGNORE`` dedup and the
``scan_state`` checkpoint that lets the poller fetch only new blocks.
"""
from database import WhaleDatabase


def _row(hash_: str, block: int) -> dict:
    return {
        "hash": hash_,
        "block_number": block,
        "timestamp": 1,
        "from": "0xa",
        "to": "0xb",
        "value_eth": 1.0,
        "value_usd": 1000.0,
        "direction": "peer_to_peer",
    }


def test_tx_hash_unique_and_insert_or_ignore():
    db = WhaleDatabase(":memory:")
    assert db.insert_whale(_row("0x1", 10)) is True   # first insert
    assert db.insert_whale(_row("0x1", 10)) is False  # duplicate ignored
    assert db.count() == 1


def test_scan_state_initialises_from_existing_data():
    db = WhaleDatabase(":memory:")
    db.insert_whale(_row("0x1", 500))
    # First read initialises the checkpoint from MAX(block_number) already stored.
    assert db.get_last_scanned_block() == 500


def test_scan_state_upsert():
    db = WhaleDatabase(":memory:")
    db.set_last_scanned_block(100)
    assert db.get_last_scanned_block() == 100
    db.set_last_scanned_block(200)  # upsert of the single row
    assert db.get_last_scanned_block() == 200
