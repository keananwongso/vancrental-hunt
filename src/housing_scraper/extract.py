"""Normalize RawListings into Listings.

Structured sources (wesbrook, livrent) are mapped directly.
Free-text sources (craigslist, apify) go through DeepSeek JSON extraction,
cached in SQLite by URL hash so re-runs cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime

from openai import OpenAI

from . import db
from .models import Listing, MoveIn, RawListing

EXTRACT_PROMPT = """You extract rental listing data. Given a listing's raw text, return JSON:
{
  "price": monthly rent in CAD as a number (null if unknown),
  "beds": number of bedrooms (null if unknown),
  "baths": number of bathrooms (null if unknown),
  "sqft": integer square feet or null,
  "move_in_date": "YYYY-MM-DD" if a specific availability/move-in date is stated (assume year 2026 if missing), "now" if available immediately, "flexible" if negotiable, null if not mentioned,
  "furnished": true/false/null,
  "pets": true/false/null,
  "address": street address or cross-street if stated, else null,
  "area": neighborhood name if identifiable (e.g. "UBC", "Kitsilano", "Mount Pleasant"), else null,
  "contact_method": "phone"/"email"/"platform"/null,
  "scam_signals": array of strings, each a red flag present in the text, chosen ONLY from:
    ["wire_or_gift_payment", "deposit_before_viewing", "landlord_abroad", "no_viewing_offered",
     "urgency_pressure", "too_good_price", "off_platform_contact", "excessive_personal_info"]
    (empty array if none)
}
Return ONLY the JSON object."""


def _client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set (put it in .env)")
    return OpenAI(api_key=key, base_url="https://api.deepseek.com")


def _parse_move_in(value) -> MoveIn:
    if value in ("now", "flexible"):
        return value
    if not value:
        return None
    value = str(value).split(" ")[0].split("T")[0]
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return "now" if d.year <= 1971 or d <= date.today() else d


def _num(value) -> float | None:
    if value is None:
        return None
    m = re.search(r"[\d.]+", str(value).replace(",", ""))
    return float(m.group()) if m else None


def normalize(raw: RawListing, conn, use_llm: bool = True) -> Listing:
    if raw.source in ("wesbrook", "livrent"):
        return _from_structured(raw)
    return _from_text(raw, conn, use_llm)


def _from_structured(raw: RawListing) -> Listing:
    d = raw.data
    avail = d.get("available")
    if isinstance(avail, str) and avail.lower() == "now":
        move_in: MoveIn = "now"
    elif avail and not re.match(r"\d{4}-\d{2}-\d{2}", str(avail)):
        # wesbrook style: "Sep 1, 2026"
        try:
            move_in = datetime.strptime(avail, "%b %d, %Y").date()
        except ValueError:
            move_in = None
    else:
        move_in = _parse_move_in(avail)
    beds = _num(d.get("beds"))
    baths = _num(d.get("baths"))
    return Listing(
        source=raw.source,
        url=raw.url,
        title=raw.title,
        address=d.get("address"),
        area=d.get("area"),
        price=_num(d.get("rate") or d.get("price")),
        beds=beds,
        baths=baths,
        sqft=int(_num(d.get("sqft")) or 0) or None,
        move_in_date=move_in,
        furnished=d.get("furnished"),
        pets=d.get("pets"),
        images=raw.images,
        description=raw.text or None,
        contact_method="platform",
    )


def _from_text(raw: RawListing, conn, use_llm: bool) -> Listing:
    url_hash = hashlib.sha1(raw.url.encode()).hexdigest()
    extracted = db.cache_get(conn, url_hash)
    if extracted is None and use_llm and raw.text:
        resp = _client().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": raw.text[:8000]},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        extracted = json.loads(resp.choices[0].message.content)
        db.cache_put(conn, url_hash, extracted)
        conn.commit()
    extracted = extracted or {}
    return Listing(
        source=raw.source,
        url=raw.url,
        title=raw.title,
        address=extracted.get("address"),
        area=extracted.get("area") or raw.data.get("location"),
        price=_num(extracted.get("price")) or _num(raw.data.get("price")),
        beds=_num(extracted.get("beds")),
        baths=_num(extracted.get("baths")),
        sqft=int(_num(extracted.get("sqft")) or 0) or None,
        move_in_date=_parse_move_in(extracted.get("move_in_date")),
        furnished=extracted.get("furnished"),
        pets=extracted.get("pets"),
        images=raw.images,
        description=raw.text or None,
        contact_method=extracted.get("contact_method"),
        scam_flags=list(extracted.get("scam_signals") or []),
    )
