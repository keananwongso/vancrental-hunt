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

`serve` opens a landing page: describe what you want in plain English ("2 bed 2 bath near UBC, September move-in, under $3,500") and an LLM turns it into a search, asking a quick question only if something essential is missing. Prefer knobs? There's a manual params panel too. Either way it drops you into the results dashboard, where you filter by beds, price, distance, source, and move-in window, and hit **Scrape now** (live per-source progress). The button uses the free sources; apartments.com and Zumper cost Apify credits so they're off unless you turn them on.

You can also paste any address to re-center the distance filter, and search "within X km" of it.

Other commands:

```sh
uv run housing-scraper run --source wesbrook --source livrent   # just these
uv run housing-scraper run --skip-llm                           # skip the LLM pass
uv run housing-scraper report                                   # rebuild from saved data, no scraping
```

The plain-text search writes `criteria.yaml` for you; you can also edit that file directly (price, beds, move-in dates, areas, distance center).

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

Everything's saved in `data/listings.db` and persists across runs: new listings get a **NEW** badge, ones that disappear get marked **GONE** (greyed, not deleted). Results stream in as each source finishes, so fast ones show up right away. LLM and geocoding results are cached, so re-runs are cheap. Listings are geocoded so you can filter by distance.
