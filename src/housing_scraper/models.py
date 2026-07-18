"""Canonical listing schema shared by all sources."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# move_in_date is either an ISO date, "now", "flexible", or None (unstated)
MoveIn = date | Literal["now", "flexible"] | None


class Listing(BaseModel):
    id: str = ""  # sha1 of url, set in __init__ if absent
    source: str
    url: str
    title: str
    address: str | None = None
    area: str | None = None  # neighborhood, e.g. "Wesbrook Village"
    price: float | None = None  # CAD/month
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    move_in_date: MoveIn = None
    furnished: bool | None = None
    pets: bool | None = None
    images: list[str] = Field(default_factory=list)
    description: str | None = None
    contact_method: str | None = None  # "platform", "email", "phone", ...
    posted_at: datetime | None = None
    scraped_at: datetime = Field(default_factory=datetime.now)

    # scoring (filled by score.py)
    match_score: float = 0.0
    scam_score: float = 0.0
    scam_flags: list[str] = Field(default_factory=list)

    # display fields (filled by report.py)
    in_window: bool = False
    move_in_label: str = ""
    badge: str = ""

    # geo fields (filled by geocode pass in report.py)
    lat: float | None = None
    lng: float | None = None
    maps_url: str | None = None
    dist_km: float | None = None

    def model_post_init(self, __context) -> None:
        if not self.id:
            self.id = hashlib.sha1(self.url.encode()).hexdigest()[:16]


class RawListing(BaseModel):
    """Unnormalized listing as fetched; extract.py turns this into a Listing."""

    source: str
    url: str
    title: str = ""
    text: str = ""  # free text blob (description, attrs) for LLM extraction
    data: dict = Field(default_factory=dict)  # structured fields if the source has them
    images: list[str] = Field(default_factory=list)
