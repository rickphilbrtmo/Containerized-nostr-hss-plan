#!/usr/bin/env python3
"""
db.py — SQLite persistence layer for Nostr-HSS.

Write-through cache: Diameter reads from the in-memory dict (fast),
writes go to both SQLite and the dict. On startup, SQLite → dict.
"""
import sqlite3
import threading
import time
import os
import logging

log = logging.getLogger("nostr-hss-db")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "hss.db")

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not getattr(_local, "conn", None):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscribers (
            npub          TEXT PRIMARY KEY,
            registered_at INTEGER NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1,
            origin_host   TEXT,
            origin_realm  TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            npub          TEXT NOT NULL,
            pallet_id     TEXT NOT NULL,
            subscribed_at INTEGER NOT NULL,
            PRIMARY KEY (npub, pallet_id),
            FOREIGN KEY (npub) REFERENCES subscribers(npub)
        );

        CREATE TABLE IF NOT EXISTS pnr_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            npub        TEXT NOT NULL,
            pallet_id   TEXT NOT NULL,
            hype_score  REAL NOT NULL,
            top_note_id TEXT,
            fired_at    INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sub_pallet ON subscriptions(pallet_id);
        CREATE INDEX IF NOT EXISTS idx_pnr_log_npub ON pnr_log(npub);
    """)
    conn.commit()
    # Migration: add session columns to existing DBs that pre-date this change
    for col_def in ["origin_host TEXT", "origin_realm TEXT"]:
        try:
            conn.execute(f"ALTER TABLE subscribers ADD COLUMN {col_def}")
            conn.commit()
        except Exception:
            pass  # column already exists
    log.info(f"DB initialised at {DB_PATH}")


def load_into_memory(subscriptions: dict):
    """
    On startup: read all active subscriptions from SQLite
    and populate the in-memory dict used by Diameter.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.npub, s.origin_host, s.origin_realm, sub.pallet_id
        FROM subscribers s
        JOIN subscriptions sub ON s.npub = sub.npub
        WHERE s.active = 1
    """).fetchall()

    for row in rows:
        npub, pallet_id = row["npub"], row["pallet_id"]
        if npub not in subscriptions:
            subscriptions[npub] = {
                "npub": npub,
                "pallets": set(),
                "threshold": None,
                "session_id": None,
                "origin_host": row["origin_host"].encode() if row["origin_host"] else None,
                "origin_realm": row["origin_realm"].encode() if row["origin_realm"] else None,
            }
        subscriptions[npub]["pallets"].add(pallet_id)

    count = len(subscriptions)
    pallet_count = sum(len(v["pallets"]) for v in subscriptions.values())
    log.info(f"Loaded {count} subscriber(s), {pallet_count} subscription(s) from DB")
    return subscriptions


def upsert_subscriber(npub: str, origin_host=None, origin_realm=None):
    """Ensure subscriber row exists. Persists session routing data when provided."""
    if isinstance(origin_host, bytes):
        origin_host = origin_host.decode()
    if isinstance(origin_realm, bytes):
        origin_realm = origin_realm.decode()
    conn = get_conn()
    conn.execute("""
        INSERT INTO subscribers (npub, registered_at, active, origin_host, origin_realm)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(npub) DO UPDATE SET active=1,
            origin_host=COALESCE(excluded.origin_host, origin_host),
            origin_realm=COALESCE(excluded.origin_realm, origin_realm)
    """, (npub, int(time.time()), origin_host, origin_realm))
    conn.commit()


def add_subscription(npub: str, pallet_id: str):
    """Add a pallet subscription for an npub."""
    upsert_subscriber(npub)
    conn = get_conn()
    conn.execute("""
        INSERT INTO subscriptions (npub, pallet_id, subscribed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(npub, pallet_id) DO NOTHING
    """, (npub, pallet_id, int(time.time())))
    conn.commit()
    log.debug(f"DB: subscribed {npub[:16]}... → {pallet_id}")


def remove_subscription(npub: str, pallet_id: str):
    """Remove a pallet subscription."""
    conn = get_conn()
    conn.execute("""
        DELETE FROM subscriptions WHERE npub=? AND pallet_id=?
    """, (npub, pallet_id))
    # Deactivate subscriber if no pallets remain
    remaining = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE npub=?", (npub,)
    ).fetchone()[0]
    if remaining == 0:
        conn.execute("UPDATE subscribers SET active=0 WHERE npub=?", (npub,))
    conn.commit()
    log.debug(f"DB: unsubscribed {npub[:16]}... from {pallet_id}")


def log_pnr(npub: str, pallet_id: str, hype_score: float, top_note_id: str = ""):
    """Record a fired PNR in the audit log."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO pnr_log (npub, pallet_id, hype_score, top_note_id, fired_at)
        VALUES (?, ?, ?, ?, ?)
    """, (npub, pallet_id, hype_score, top_note_id, int(time.time())))
    conn.commit()


def get_subscriber_pallets(npub: str) -> list:
    """Return list of active pallet_ids for an npub."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sub.pallet_id FROM subscriptions sub
        JOIN subscribers s ON s.npub = sub.npub
        WHERE sub.npub=? AND s.active=1
    """, (npub,)).fetchall()
    return [r["pallet_id"] for r in rows]


def get_all_subscribers() -> list:
    """Return all active subscribers with their pallets."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.npub, sub.pallet_id
        FROM subscribers s
        JOIN subscriptions sub ON s.npub = sub.npub
        WHERE s.active = 1
        ORDER BY s.npub
    """).fetchall()
    result = {}
    for row in rows:
        npub = row["npub"]
        if npub not in result:
            result[npub] = {"npub": npub, "pallets": []}
        result[npub]["pallets"].append(row["pallet_id"])
    return list(result.values())


def get_pnr_history(npub: str = None, limit: int = 50) -> list:
    """Return recent PNR log entries, optionally filtered by npub."""
    conn = get_conn()
    if npub:
        rows = conn.execute("""
            SELECT * FROM pnr_log WHERE npub=?
            ORDER BY fired_at DESC LIMIT ?
        """, (npub, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM pnr_log ORDER BY fired_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]
