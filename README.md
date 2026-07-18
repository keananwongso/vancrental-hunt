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

## How each source is actually accessed

Interesting bit: none of these use a formal public API — they're all scraping in the technical sense. What differs is *what's on the page*, which decides how much work it takes to get clean data. The tile label in the dashboard hints at the method for each.

**Wesbrook Properties — `admin-ajax`.** Their site runs on WordPress, and the "available units" list is loaded by the page through WordPress's built-in `admin-ajax.php` endpoint, which returns JSON. So we just call that same endpoint directly. This is the closest thing to a real API here — clean, structured data with no parsing guesswork.

**liv.rent — `GraphQL/SSR` (really just SSR).** liv.rent *does* have a GraphQL API (`nemesis-prod.liv.rent/graphql`), but they've disabled introspection, so the exact queries/fields aren't discoverable — we don't call it. Instead we use the fact that liv.rent is a Next.js server-side-rendered site: each listing page ships with the full listing record (price, beds, baths, availability date, furnished, pets, ...) **embedded as JSON right in the HTML** so the page renders instantly. We fetch the page and pull that embedded JSON out. Clean structured data, no LLM needed — but it's still HTML scraping, and it would break if they change their page structure. (The "GraphQL" in the label is aspirational; it's SSR-embedded JSON in practice.)

**Craigslist — `HTML + DeepSeek`.** No structured data at all — a Craigslist post is a human-written blurb ("available Sept 1, 2 bed, no pets, w/d in unit…"). We grab the raw HTML, then pass the free text to an LLM (DeepSeek) to extract structured fields — crucially the **move-in date**, which is almost always buried in prose. This is the messiest source and the reason the LLM exists. It's also rate-limited on purpose (paced a few seconds per page) so Craigslist doesn't block us.

**apartments.com, Zumper — `Apify actor`.** These actively block scrapers (Cloudflare, etc.), so instead of scraping them directly we run pre-built scrapers ("actors") on Apify, which handle the anti-bot stuff, and just fetch the results. Opt-in because Apify costs credits.

So the spectrum runs from *"call the JSON endpoint the site itself uses"* (Wesbrook) → *"read the data the page already embedded"* (liv.rent) → *"let AI read the human's paragraph"* (Craigslist) → *"pay a service to get past the bot wall"* (Apify). Same goal — structured listings — with escalating effort depending on how the site exposes its data.
