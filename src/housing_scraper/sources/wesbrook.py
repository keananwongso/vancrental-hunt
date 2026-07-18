"""Wesbrook Properties (UBC Wesbrook Village) — WordPress admin-ajax availability feed.

Each building page loads available suites via
POST /wp-admin/admin-ajax.php {action: get_available_units_html, building: <slug>, page: N}
returning {"unitOutput": <html rows>, "paginationOutput": <html>}.
"""

from __future__ import annotations

import re

from parsel import Selector

from ..config import Criteria
from ..models import RawListing
from .base import Source, http_session, _noop

BASE = "https://www.wesbrookproperties.com"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"


class WesbrookSource(Source):
    name = "wesbrook"

    def fetch(self, criteria: Criteria, progress=_noop) -> list[RawListing]:
        session = http_session()
        listings: list[RawListing] = []
        slugs = self._building_slugs(session)
        for i, slug in enumerate(slugs, 1):
            listings.extend(self._building_units(session, slug))
            progress(i, len(slugs))
        return listings

    def _building_slugs(self, session) -> list[str]:
        resp = session.get(f"{BASE}/properties/")
        slugs = re.findall(r"/properties/([a-z0-9-]+)/", resp.text)
        return sorted(set(slugs))

    def _building_units(self, session, slug: str) -> list[RawListing]:
        units: list[RawListing] = []
        page, max_pages = 1, 1
        while page <= max_pages:
            resp = session.post(
                AJAX,
                data={"action": "get_available_units_html", "building": slug, "page": page},
                headers={"Referer": f"{BASE}/properties/{slug}/"},
            )
            payload = resp.json()
            sel = Selector(payload.get("unitOutput") or "")
            for row in sel.css("div.availability_details_body"):
                field = lambda css: (row.css(f"{css} span ::text").get() or "").strip()
                suite = field(".suite")
                if not suite:
                    continue
                units.append(
                    RawListing(
                        source=self.name,
                        # no per-unit page exists; use a stable synthetic URL per suite
                        url=f"{BASE}/properties/{slug}/#unit-{re.sub(r'[^A-Za-z0-9]+', '-', suite)}",
                        title=f"{suite} — Wesbrook Village (UBC)",
                        data={
                            "building": slug,
                            "suite": suite,
                            "available": field(".avail"),
                            "rate": field(".rate"),
                            "beds": field(".bed"),
                            "baths": field(".bath"),
                            "sqft": field(".sqf"),
                            "apply_url": row.css(".action a::attr(href)").get(),
                            "area": "Wesbrook Village",
                        },
                    )
                )
            pagination = Selector(payload.get("paginationOutput") or "")
            pages = [int(p) for p in pagination.css("li::attr(data-page)").getall() if p.isdigit()]
            max_pages = max(pages) if pages else 1
            page += 1
        return units
