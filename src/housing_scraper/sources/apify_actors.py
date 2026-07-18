"""Apify pay-per-result actors for sites we can't scrape directly.

Requires APIFY_TOKEN in .env. Each actor is best-effort: a failure (schema
drift, actor gone, out of credit) skips that source rather than aborting the run.
Actor inputs are search URLs so filters live in one place.
"""

from __future__ import annotations

import os
from datetime import timedelta

from apify_client import ApifyClient

from ..config import Criteria
from ..models import RawListing
from .base import Source, _noop

ZUMPER_URL = (
    "https://www.zumper.com/apartments-for-rent/vancouver-bc/2-bedrooms?price-max={max_price}"
)
APARTMENTS_URL = (
    "https://www.apartments.com/vancouver-bc/2-bedrooms-{max_price_k}k/"
)

ACTORS = {
    "zumper": {
        "actor_id": "scrapemind/zumpercom-scraper",
        "input": lambda c: {
            "startUrls": [{"url": ZUMPER_URL.format(max_price=int(c.max_price))}],
            "maxItems": 150,
        },
    },
    "apartments_com": {
        "actor_id": "parseforge/apartments-com-scraper",
        "input": lambda c: {
            "startUrls": [{"url": APARTMENTS_URL.format(max_price_k=int(c.max_price // 1000))}],
            "maxItems": 150,
        },
    },
}


class ApifyActorSource(Source):
    def __init__(self, name: str, actor_id: str, input_builder):
        self.name = name
        self.actor_id = actor_id
        self.input_builder = input_builder

    def fetch(self, criteria: Criteria, progress=_noop) -> list[RawListing]:
        client = ApifyClient(os.environ["APIFY_TOKEN"])
        run = client.actor(self.actor_id).call(
            run_input=self.input_builder(criteria), run_timeout=timedelta(minutes=10)
        )
        items = client.dataset(run["defaultDatasetId"]).list_items().items
        raws = []
        for item in items:
            url = item.get("url") or item.get("listingUrl") or item.get("detailUrl")
            if not url:
                continue
            title = item.get("title") or item.get("name") or item.get("propertyName") or url
            # dump every scalar field into text; DeepSeek sorts out the schema drift
            text = "\n".join(
                f"{k}: {v}" for k, v in item.items()
                if isinstance(v, (str, int, float)) and len(str(v)) < 2000
            )
            raws.append(
                RawListing(
                    source=self.name,
                    url=url,
                    title=str(title),
                    text=text,
                    data={"price": item.get("price") or item.get("rent")},
                    images=[i for i in (item.get("photos") or item.get("images") or [])
                            if isinstance(i, str)][:5],
                )
            )
        return raws


def apify_sources() -> dict[str, ApifyActorSource]:
    if not os.environ.get("APIFY_TOKEN"):
        raise RuntimeError("APIFY_TOKEN not set — skipping Apify sources")
    return {
        name: ApifyActorSource(name, spec["actor_id"], spec["input"])
        for name, spec in ACTORS.items()
    }
