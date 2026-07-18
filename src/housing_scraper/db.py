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
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "listings.db")
    conn.executescript(SCHEMA)
    return conn


def clear_listings(conn: sqlite3.Connection) -> None:
    """Drop all listings so a run starts fresh. Leaves extract_cache and
    geocode_cache intact — those just make re-runs cheap and don't affect
    which listings show up."""
    conn.execute("DELETE FROM listings")
    conn.commit()


def upsert(conn: sqlite3.Connection, listing: Listing) -> bool:
    """Insert or refresh a listing. Returns True if it's new."""
    now = datetime.now().isoformat()
    cur = conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing.id,))
    is_new = cur.fetchone() is None
    conn.execute(
        """INSERT INTO listings (id, source, url, json, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET json = excluded.json, last_seen = excluded.last_seen""",
        (listing.id, listing.source, listing.url, listing.model_dump_json(), now, now),
    )
    return is_new


def all_listings(conn: sqlite3.Connection) -> list[tuple[Listing, str]]:
    """Return (listing, first_seen) for everything in the DB."""
    rows = conn.execute("SELECT json, first_seen FROM listings").fetchall()
    return [(Listing.model_validate_json(j), fs) for j, fs in rows]


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
