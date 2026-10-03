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

    def recent_alerts(self, limit: int = 5) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM whale_transfers ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]