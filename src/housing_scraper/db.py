"""SQLite persistence: listings history + LLM extraction cache."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal

from .config import DATA_DIR
from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    json TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extract_cache (
    url_hash TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    address_key TEXT PRIMARY KEY,
    lat REAL,
    lng REAL,
    failed INTEGER NOT NULL DEFAULT 0
);
-- One row per (source, run) that COMPLETED successfully. Used to decide GONE:
-- a listing is delisted only after its source has completed several runs
-- without re-seeing it, so a capped/partial fetch doesn't falsely delist.
CREATE TABLE IF NOT EXISTS run_log (
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (source, run_id)
);
"""

# How many completed runs a source must miss a listing before it's GONE.
GONE_AFTER_MISSES = 3


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "listings.db")
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, listing: Listing, seen_at: str) -> bool:
    """Insert or refresh a listing, stamping last_seen with this run's `seen_at`.
    Returns True if the listing is new (first time we've ever seen this id)."""
    cur = conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing.id,))
    is_new = cur.fetchone() is None
    conn.execute(
        """INSERT INTO listings (id, source, url, json, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET json = excluded.json, last_seen = excluded.last_seen""",
        (listing.id, listing.source, listing.url, listing.model_dump_json(), seen_at, seen_at),
    )
    return is_new


def is_new_since_last_run(conn: sqlite3.Connection, listing_id: str, this_run: str) -> bool:
    """True if this listing's first_seen is from the current run (i.e. brand new)."""
    row = conn.execute("SELECT first_seen FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return bool(row) and row[0] == this_run


def unseen_ids(conn: sqlite3.Connection, source: str, this_run: str) -> list[tuple[str, str]]:
    """(id, last_seen) for listings from `source` NOT seen in the current run.
    Whether each is actually GONE depends on is_gone() (the miss threshold)."""
    rows = conn.execute(
        "SELECT id, last_seen FROM listings WHERE source = ? AND last_seen != ?",
        (source, this_run),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def record_run(conn: sqlite3.Connection, source: str, run_id: str) -> None:
    """Log that `source` completed successfully in run `run_id`. Only completed
    runs count toward the GONE threshold — a timed-out/errored source is never
    recorded, so it can never delist its listings."""
    conn.execute(
        "INSERT OR IGNORE INTO run_log (source, run_id) VALUES (?, ?)", (source, run_id)
    )


def is_gone(conn: sqlite3.Connection, source: str, last_seen: str) -> bool:
    """True if `source` has COMPLETED at least GONE_AFTER_MISSES runs strictly
    after this listing's last_seen — i.e. it's been missed enough consecutive
    completed runs to count as delisted. run_id is an ISO timestamp, so string
    comparison is chronological."""
    (misses,) = conn.execute(
        "SELECT COUNT(*) FROM run_log WHERE source = ? AND run_id > ?", (source, last_seen)
    ).fetchone()
    return misses >= GONE_AFTER_MISSES


def update_json(conn: sqlite3.Connection, listing: Listing) -> None:
    """Persist a listing's JSON (e.g. after scoring) WITHOUT touching last_seen,
    so 'gone' listings keep their old timestamp."""
    conn.execute(
        "UPDATE listings SET json = ? WHERE id = ?",
        (listing.model_dump_json(), listing.id),
    )


def all_listings(conn: sqlite3.Connection) -> list[tuple[Listing, str]]:
    """Return (listing, last_seen) for everything in the DB."""
    rows = conn.execute("SELECT json, last_seen FROM listings").fetchall()
    return [(Listing.model_validate_json(j), ls) for j, ls in rows]


def cache_get(conn: sqlite3.Connection, url_hash: str) -> dict | None:
    row = conn.execute("SELECT json FROM extract_cache WHERE url_hash = ?", (url_hash,)).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(conn: sqlite3.Connection, url_hash: str, data: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extract_cache (url_hash, json) VALUES (?, ?)",
        (url_hash, json.dumps(data)),
    )


def geocode_get(
    conn: sqlite3.Connection, address_key: str
) -> tuple[float, float] | None | Literal["failed"]:
    """Return (lat, lng), None if not cached, or 'failed' if previously attempted with no result."""
    row = conn.execute(
        "SELECT lat, lng, failed FROM geocode_cache WHERE address_key = ?", (address_key,)
    ).fetchone()
    if row is None:
        return None
    if row[2]:
        return "failed"
    return (row[0], row[1])


def geocode_put(
    conn: sqlite3.Connection, address_key: str, lat: float | None, lng: float | None
) -> None:
    failed = 0 if (lat is not None and lng is not None) else 1
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (address_key, lat, lng, failed) VALUES (?, ?, ?, ?)",
        (address_key, lat, lng, failed),
    )
