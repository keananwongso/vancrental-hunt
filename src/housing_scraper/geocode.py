"""Geocode listing addresses via Nominatim (OpenStreetMap). Free, no API key needed."""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request

from . import db
from .models import Listing

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "housing-scraper/0.1 (keananwongso7@gmail.com)"
_RATE_DELAY = 1.1  # Nominatim requires ≥1s between requests

log = logging.getLogger(__name__)


def geocode_listings(listings: list[Listing], conn, progress=None) -> None:
    """Populate lat/lng/maps_url on each listing in place. Caches results in DB.

    `progress(done, total)` is called after each listing (if given) so a caller
    can report progress — geocoding uncached addresses is slow (~1s each).
    """
    total = len(listings)
    for i, listing in enumerate(listings, 1):
        _geocode_one(listing, conn)
        if progress:
            progress(i, total)


def _geocode_one(listing: Listing, conn) -> None:
    addr = listing.address
    if not addr:
        if listing.area:
            listing.maps_url = (
                "https://maps.google.com/?q="
                + urllib.parse.quote(listing.area + " Vancouver BC")
            )
        return

    key = addr.strip().lower()
    cached = db.geocode_get(conn, key)

    if cached == "failed":
        if listing.area:
            listing.maps_url = (
                "https://maps.google.com/?q="
                + urllib.parse.quote(listing.area + " Vancouver BC")
            )
        return

    if cached is not None:
        listing.lat, listing.lng = cached
        listing.maps_url = "https://maps.google.com/?q=" + urllib.parse.quote(addr)
        return

    # Cache miss — call Nominatim
    lat, lng = _nominatim_lookup(addr)
    db.geocode_put(conn, key, lat, lng)
    conn.commit()

    if lat is not None:
        listing.lat = lat
        listing.lng = lng
        listing.maps_url = "https://maps.google.com/?q=" + urllib.parse.quote(addr)
    else:
        if listing.area:
            listing.maps_url = (
                "https://maps.google.com/?q="
                + urllib.parse.quote(listing.area + " Vancouver BC")
            )

    time.sleep(_RATE_DELAY)


def geocode_address(address: str, conn=None) -> tuple[float, float] | None:
    """Geocode a single free-text address to (lat, lng), or None. Uses the DB
    cache if a connection is given. Public entry point for 'center on this address'."""
    key = address.strip().lower()
    if conn is not None:
        cached = db.geocode_get(conn, key)
        if cached == "failed":
            return None
        if cached is not None:
            return cached
    lat, lng = _nominatim_lookup(address)
    # Nominatim can't resolve marketing building names ("Granite Terrace III,
    # 3313 Shrum Lane, …") and returns nothing for the whole query. If the
    # first segment is a building name (no street number) but a later segment
    # starts with one, retry from the street number on.
    if lat is None:
        stripped = _strip_building_name(address)
        if stripped and stripped != address:
            lat, lng = _nominatim_lookup(stripped)
    if conn is not None:
        db.geocode_put(conn, key, lat, lng)   # cache under the original key
        conn.commit()
    if lat is not None and lng is not None:
        return (lat, lng)
    return None


def _strip_building_name(address: str) -> str | None:
    """If `address` starts with a building-name segment (no digits) before a
    segment that begins with a street number, drop the prefix and return the
    rest. Otherwise return None. e.g. 'Granite Terrace III, 3313 Shrum Lane, …'
    -> '3313 Shrum Lane, …'."""
    parts = [p.strip() for p in address.split(",")]
    for i, part in enumerate(parts):
        if part[:1].isdigit():          # first segment starting with a number
            if i == 0:
                return None             # already starts with the street number
            return ", ".join(parts[i:])
    return None


def _nominatim_lookup(address: str) -> tuple[float | None, float | None]:
    query = (
        address
        if "vancouver" in address.lower() or ", bc" in address.lower()
        else address + ", Vancouver BC"
    )
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1"})
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            results = json.loads(resp.read())
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        log.warning("Nominatim lookup failed for %r: %s", address, e)
    return None, None


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))
