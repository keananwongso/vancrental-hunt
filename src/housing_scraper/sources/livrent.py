"""liv.rent — parse listing records embedded in the Next.js flight payload.

Search pages (/rental-listings/city/vancouver) link to detail pages; each detail
page embeds the full listing record (availability_date, price, count_bedrooms,
count_full_bathrooms, size, furnished, allow_pets, ...) as escaped JSON.
GraphQL introspection is disabled, so we read the SSR payload instead.
"""

from __future__ import annotations

import re
import time

from ..config import Criteria
from ..models import RawListing
from .base import Source, http_session, _noop

BASE = "https://liv.rent"
CITY_URL = f"{BASE}/rental-listings/city/vancouver"
DETAIL_RE = re.compile(r'href="(/rental-listings/detail/[^"]+)"')
MAX_PAGES = 5
DETAIL_DELAY_S = 0.25  # liv.rent isn't rate-limited aggressively; keep it brisk
MAX_DETAILS = 120      # safety cap so a huge result set can't blow the run's time budget


def _field(blob: str, key: str) -> str | None:
    m = re.search(rf'"{key}":"((?:[^"\\]|\\.)*)"', blob)
    if m:
        return m.group(1).replace('\\n', '\n')
    m = re.search(rf'"{key}":([0-9.]+)', blob)
    return m.group(1) if m else None


class LivRentSource(Source):
    name = "livrent"

    def fetch(self, criteria: Criteria, progress=_noop) -> list[RawListing]:
        session = http_session()
        links: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            url = CITY_URL if page == 1 else f"{CITY_URL}?page={page}"
            html = session.get(url).text
            found = [l for l in DETAIL_RE.findall(html) if l not in links]
            if not found:
                break
            links.extend(found)
        links = links[:MAX_DETAILS]
        listings = []
        total = len(links)
        for i, link in enumerate(links, 1):
            time.sleep(DETAIL_DELAY_S)
            try:
                listings.append(self._detail(session, BASE + link))
            except Exception as e:
                print(f"  livrent: skip {link}: {e}")
            progress(i, total)
        return [l for l in listings if l]

    def _detail(self, session, url: str) -> RawListing | None:
        raw = session.get(url).text
        text = raw.replace('\\\\\\"', '"').replace('\\"', '"')
        anchor = text.find('"availability_date"')
        if anchor == -1:
            return None
        blob = text[max(0, anchor - 20000): anchor + 20000]

        avail = (_field(blob, "availability_date") or "").split(" ")[0] or None
        beds = _field(blob, "count_bedrooms")
        baths = _field(blob, "count_full_bathrooms")
        price = re.search(r'"PriceSpecification","price":([0-9.]+)', text)
        title_m = re.search(r"<title>([^<]+)</title>", raw)
        street = _field(blob, "full_street_name") or _field(blob, "street_name")
        furnished = _field(blob, "furnished")
        pets = _field(blob, "allow_pets")
        desc = _field(blob, "description")

        return RawListing(
            source=self.name,
            url=url,
            title=(title_m.group(1).strip() if title_m else street or url),
            text=desc or "",
            data={
                "price": float(price.group(1)) if price else None,
                "beds": float(beds) if beds else None,
                "baths": float(baths) if baths else None,
                "sqft": _field(blob, "size"),
                "available": avail,
                "address": street,
                "furnished": None if furnished is None else furnished not in ("0", "null", ""),
                "pets": None if pets is None else pets not in ("0", "null", ""),
                "area": None,
            },
        )
