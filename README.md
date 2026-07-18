# vancrental-hunt

> it was such a hassle to research housing options through multiple various sites every other day. luckily Fable exists lol.

A Vancouver rental finder, aimed at UBC/Wesbrook.

Rental sites bury the move-in date in free text, so listings for the month you actually want get lost under everything else. This pulls listings from a few sites, uses an LLM to read the real move-in date out of the text, scores each one against your criteria, flags sketchy ones, and shows them ranked in a local dashboard.

## Setup

```sh
uv sync
cp .env.example .env    # add your LLM API key (APIFY_TOKEN optional)
```

## Run

```sh
uv run housing-scraper serve    # dashboard at localhost:5173 — this is the main one
uv run housing-scraper run      # scrape once from the terminal instead
```

`serve` gives you a dashboard with filters (beds, price, distance, source, move-in window) and a **Scrape now** button that shows live progress per source. The button uses the free sources; apartments.com and Zumper cost Apify credits so they're off unless you turn them on.

Other commands:

```sh
uv run housing-scraper run --source wesbrook --source livrent   # just these
uv run housing-scraper run --skip-llm                           # skip the LLM pass
uv run housing-scraper report                                   # rebuild from saved data, no scraping
```

Set what you're looking for (price, beds, move-in dates, area, distance center) in `criteria.yaml`.

## Sources

| Source | Notes |
|---|---|
| Wesbrook Properties | The UBC buildings. Clean data. |
| liv.rent | BC-focused, good data. |
| Craigslist | Biggest volume, messy text. Paced slowly so it doesn't get blocked. |
| apartments.com, Zumper | Via Apify (opt-in, costs credits). |

## How ranking works

A 0–100 match score from beds/baths, price, and mostly **move-in date** — anything landing in your target window floats to the top. "Available now" and unstated dates sit in the middle (worth asking about) rather than getting dropped.

Separately, a scam score flags the usual red flags: wire/gift-card payment, "landlord is overseas" stories, pushy urgency, or a price way below market. Wesbrook and liv.rent are trusted so they're rarely flagged; Craigslist is where the risk lives.

Everything's saved in `data/listings.db`, so each run marks what's new and re-runs are cheap (LLM and geocoding results are cached). Listings are geocoded so you can filter by distance.
