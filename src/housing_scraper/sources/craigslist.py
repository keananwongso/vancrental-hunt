"""Craigslist Vancouver — static search HTML + throttled detail fetches.

Search URL redirects to www.craigslist.org/search/area/vancouver. Detail pages
are fetched only for URLs not already in the DB (they never change materially),
paced to avoid Craigslist's block layer. Move-in date lives in free text, so
listings go through the DeepSeek extractor downstream.
"""

from __future__ import annotations

import re
import time

from parsel import Selector

from ..config import Criteria
from ..models import RawListing
from .base import Source, http_session, _noop

SEARCH = "https://vancouver.craigslist.org/search/apa"
LIST_DELAY_S = 3.0
DETAIL_DELAY_S = 3.0
MAX_DETAILS = 60


class CraigslistSource(Source):
    name = "craigslist"

    def __init__(self, known_urls: set[str] | None = None):
        self.known_urls = known_urls or set()

    def fetch(self, criteria: Criteria, progress=_noop) -> list[RawListing]:
        session = http_session()
        seen: dict[str, RawListing] = {}
        for query in criteria.craigslist_queries:
            params = {
                "min_bedrooms": int(criteria.beds),
                "max_bedrooms": int(criteria.beds) + 1,
                "min_price": int(criteria.min_price),
                "max_price": int(criteria.max_price),
            }
            if query:
                params["query"] = query
            resp = session.get(SEARCH, params=params, allow_redirects=True)
            for li in Selector(resp.text).css("li.cl-static-search-result"):
                url = li.css("a::attr(href)").get()
                if not url or url in seen:
                    continue
                seen[url] = RawListing(
                    source=self.name,
                    url=url,
                    title=li.attrib.get("title", ""),
                    data={
                        "price": _price(li.css(".price::text").get()),
                        "location": (li.css(".location::text").get() or "").strip(),
                    },
                )
            time.sleep(LIST_DELAY_S)

        todo = [(u, r) for u, r in seen.items() if u not in self.known_urls]
        total = min(len(todo), MAX_DETAILS)
        fetched = 0
        results = []
        for url, raw in todo:
            if fetched >= MAX_DETAILS:
                print(f"  craigslist: detail cap {MAX_DETAILS} reached, {len(todo) - fetched} deferred to next run")
                break
            time.sleep(DETAIL_DELAY_S)
            try:
                self._enrich(session, raw)
                fetched += 1
            except Exception as e:
                print(f"  craigslist: skip {url}: {e}")
                continue
            results.append(raw)
            progress(fetched, total)
        return results

    def _enrich(self, session, raw: RawListing) -> None:
        html = session.get(raw.url).text
        sel = Selector(html)
        body = " ".join(sel.css("#postingbody ::text").getall())
        body = re.sub(r"\s+", " ", body).replace("QR Code Link to This Post", "").strip()
        attrs = " | ".join(
            a.strip() for a in sel.css(".mapAndAttrs .attrgroup ::text").getall() if a.strip()
        )
        raw.text = f"TITLE: {raw.title}\nLOCATION: {raw.data.get('location')}\nPRICE: {raw.data.get('price')}\nATTRS: {attrs}\nBODY: {body}"
        raw.images = sel.css("#thumbs a::attr(href), .gallery img::attr(src)").getall()[:5]
        # reply/contact hints for scam scoring
        raw.data["has_phone"] = bool(re.search(r"\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}", body))


def _price(text: str | None) -> float | None:
    if not text:
        return None
    digits = re.sub(r"[^\d.]", "", text)
    return float(digits) if digits else None
