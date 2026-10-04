"""SQLite storage layer for whale alerts.

Responsible for creating the table (idempotent), inserting whale records and
providing a small read helper used by the CLI to print recent alerts.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger("whale.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whale_transfers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash     TEXT    NOT NULL,
    block_number INTEGER,
    timestamp   INTEGER,
    from_address TEXT,
    to_address   TEXT,
    value_eth   REAL,
    value_usd   REAL,
    direction   TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS eth_price_ticks (
    ts         INTEGER PRIMARY KEY,
    price_usd  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS address_profiles (
    address          TEXT PRIMARY KEY,
    direction        TEXT,
    eth_balance      REAL,
    usdt_balance     REAL,
    usdc_balance     REAL,
    dai_balance      REAL,
    stablecoin_usd   REAL,
    tx_count_24h     INTEGER,
    tx_count_7d      INTEGER,
    total_txs_observed INTEGER,
    last_active_ts   INTEGER,
    inflow_eth       REAL,
    outflow_eth      REAL,
    net_eth_flow     REAL,
    avg_tx_value_eth REAL,
    max_tx_value_eth REAL,
    is_contract      INTEGER,
    address_tag      TEXT,
    eth_usd_exposure REAL,
    tokens_json      TEXT,
    profiled_at      TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_whale_tx_hash ON whale_transfers(tx_hash);
"""


class WhaleDatabase:
    """Thin wrapper around a SQLite connection for whale records."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or config.DB_PATH
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def insert_whale(self, whale: Dict[str, Any]) -> bool:
        """Insert one whale record. Returns ``True`` if inserted, ``False`` if it was
        a duplicate (same tx_hash already present)."""
        try:
            cur = self.conn.execute(
                """
                INSERT INTO whale_transfers
                    (tx_hash, block_number, timestamp, from_address, to_address,
                     value_eth, value_usd, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    whale.get("hash", ""),
                    whale.get("block_number"),
                    whale.get("timestamp"),
                    whale.get("from", ""),
                    whale.get("to", ""),
                    whale.get("value_eth"),
                    whale.get("value_usd"),
                    whale.get("direction"),
                ),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            logger.debug("Duplicate whale tx_hash ignored: %s", whale.get("hash"))
            self.conn.rollback()
            return False

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM whale_transfers")
        return int(cur.fetchone()[0])

    # --- Price ticks ----------------------------------------------------------
    def record_price_tick(self, price_usd: float, ts: Optional[int] = None) -> None:
        """Store an ETH/USD price sample keyed by unix timestamp (idempotent upsert)."""
        ts = int(ts) if ts is not None else int(__import__("time").time())
        self.conn.execute(
            "INSERT OR REPLACE INTO eth_price_ticks (ts, price_usd) VALUES (?, ?)",
            (ts, float(price_usd)),
        )
        self.conn.commit()

    # --- Address profiles -----------------------------------------------------
    def get_whale_addresses(self) -> List[str]:
        """Distinct ``from``/``to`` addresses seen in whale transfers."""
        cur = self.conn.execute(
            "SELECT from_address FROM whale_transfers UNION SELECT to_address FROM whale_transfers"
        )
        return sorted({row[0] for row in cur.fetchall() if row[0]})

    def get_profiled_addresses(self) -> set:
        cur = self.conn.execute("SELECT address FROM address_profiles")
        return {row[0] for row in cur.fetchall()}

    def upsert_address_profile(self, profile: Dict[str, Any]) -> None:
        """Insert or replace one address profile record."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO address_profiles (
                address, direction, eth_balance, usdt_balance, usdc_balance,
                dai_balance, stablecoin_usd, tx_count_24h, tx_count_7d,
                total_txs_observed, last_active_ts, inflow_eth, outflow_eth,
                net_eth_flow, avg_tx_value_eth, max_tx_value_eth, is_contract,
                address_tag, eth_usd_exposure, tokens_json, profiled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.get("address"),
                profile.get("direction"),
                profile.get("eth_balance"),
                profile.get("usdt_balance"),
                profile.get("usdc_balance"),
                profile.get("dai_balance"),
                profile.get("stablecoin_usd"),
                profile.get("tx_count_24h"),
                profile.get("tx_count_7d"),
                profile.get("total_txs_observed"),
                profile.get("last_active_ts"),
                profile.get("inflow_eth"),
                profile.get("outflow_eth"),
                profile.get("net_eth_flow"),
                profile.get("avg_tx_value_eth"),
                profile.get("max_tx_value_eth"),
                1 if profile.get("is_contract") else 0,
                profile.get("address_tag"),
                profile.get("eth_usd_exposure"),
                profile.get("tokens_json"),
                profile.get("profiled_at"),
            ),
        )
        self.conn.commit()

    def profile_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM address_profiles")
        return int(cur.fetchone()[0])

    def recent_alerts(self, limit: int = 5) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM whale_transfers ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]