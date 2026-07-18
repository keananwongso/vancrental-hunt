"""CLI: `housing-scraper run [--source X] [--skip-llm] [--no-browser]` / `housing-scraper report`."""

from __future__ import annotations

import argparse

from . import db
from .config import load_criteria
from .extract import normalize
from .models import Listing
from .report import render
from .score import score_all
from .sources.craigslist import CraigslistSource
from .sources.livrent import LivRentSource
from .sources.wesbrook import WesbrookSource


def main() -> None:
    parser = argparse.ArgumentParser(prog="housing-scraper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="scrape sources, score, and build report.html")
    run.add_argument("--source", action="append", help="only these sources (repeatable)")
    run.add_argument("--skip-llm", action="store_true", help="skip DeepSeek extraction (uncached free-text listings stay partial)")
    run.add_argument("--no-browser", action="store_true")
    sub.add_parser("report", help="re-render report.html from the DB without scraping")
    srv = sub.add_parser("serve", help="run the local dashboard with a Scrape-now button")
    srv.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()

    if args.cmd == "serve":
        from .server import serve
        serve(args.port)
        return

    criteria = load_criteria()
    conn = db.connect()

    if args.cmd == "report":
        stored = db.all_listings(conn)
        listings = [l for l, _ in stored]
        score_all(listings, criteria)
        out = render(listings, criteria, new_ids=set(), conn=conn)
        print(f"report: {out}")
        return

    known_urls = {l.url for l, _ in db.all_listings(conn)}
    available = {
        "wesbrook": WesbrookSource(),
        "livrent": LivRentSource(),
        "craigslist": CraigslistSource(known_urls=known_urls),
    }
    try:
        from .sources.apify_actors import apify_sources
        available.update(apify_sources())
    except Exception as e:
        print(f"apify sources unavailable: {e}")

    names = args.source or list(available)
    new_ids: set[str] = set()
    for name in names:
        source = available[name]
        print(f"{name}: fetching…")
        raws = source.fetch(criteria)
        print(f"{name}: {len(raws)} listings")
        for raw in raws:
            try:
                listing = normalize(raw, conn, use_llm=not args.skip_llm)
            except Exception as e:
                print(f"  {name}: normalize failed for {raw.url}: {e}")
                continue
            if db.upsert(conn, listing):
                new_ids.add(listing.id)
        conn.commit()

    listings = [l for l, _ in db.all_listings(conn)]
    score_all(listings, criteria)
    for l in listings:
        db.upsert(conn, l)  # persist scores
    conn.commit()

    matches = [l for l in listings if l.match_score >= 40]
    out = render(matches, criteria, new_ids, open_browser=not args.no_browser, conn=conn)
    print(f"{len(listings)} listings in DB, {len(matches)} shown, {len(new_ids)} new → {out}")
