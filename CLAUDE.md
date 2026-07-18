# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

A personal Vancouver rental aggregator, tuned for a **2bed/2bath, ~$3,500 CAD max, September move-in** search around **UBC / Wesbrook Village**. It scrapes several sources, normalizes them with an LLM, scores each listing for fit and scam-risk, geocodes them, and produces both a static `report.html` and a local dashboard with a live "Scrape now" button.

The hard problem it solves: most rental sites have no reliable move-in-date filter, so September listings drown under July/August ones. DeepSeek extracts the move-in date from free text; scoring ranks the September window to the top.

## Run it

```sh
uv sync                              # install deps
cp .env.example .env                 # add DEEPSEEK_API_KEY (required), APIFY_TOKEN (optional)

uv run housing-scraper serve         # dashboard + Scrape-now button → http://localhost:5173
uv run housing-scraper run           # one-shot scrape → report.html
uv run housing-scraper run --source wesbrook --source livrent   # subset
uv run housing-scraper run --skip-llm                           # no DeepSeek (free-text listings stay partial)
uv run housing-scraper report        # re-render from the DB without scraping
```

`serve` is the primary interface. Apify sources are **opt-in** via `POST /scrape?apify=1` (they spend credits); the plain button runs only the three free sources.

## Architecture

Pipeline, one stage per module:

```
sources/*  →  extract.normalize  →  db (SQLite dedupe + caches)  →  score  →  geocode  →  report
   fetch        LLM/structured        listings + extract_cache       fit/scam    Nominatim    html/csv/json
```

- **`models.py`** — `Listing` (canonical schema) and `RawListing` (pre-normalization). `Listing.id` is a sha1 of the URL.
- **`config.py`** — loads `criteria.yaml` + `.env`. `Criteria` holds beds/baths/price/move-in window/areas/`location` (center point for distance).
- **`sources/`** — each subclasses `Source` with `fetch(criteria) -> list[RawListing]`. `base.http_session()` gives a `curl_cffi` Chrome-impersonating session.
  - `wesbrook.py` — WordPress `admin-ajax.php` JSON (structured; 9 buildings). **Primary target.**
  - `livrent.py` — parses listing records embedded in the Next.js SSR payload (structured; GraphQL introspection is disabled).
  - `craigslist.py` — static search HTML + throttled detail pages (free text → DeepSeek). Rate-limited: ≥3s/request, 60-detail cap per run.
  - `apify_actors.py` — apartments.com / Zumper via Apify pay-per-result actors (opt-in).
- **`extract.py`** — structured sources map directly; free-text sources go through DeepSeek (OpenAI-compatible client, `deepseek-chat`, JSON mode). Extractions cached in `extract_cache` by URL hash, so re-runs are free.
- **`db.py`** — SQLite: `listings` (history + `first_seen`/`last_seen` → "new since last run"), `extract_cache`, `geocode_cache`.
- **`score.py`** — `match_score` (beds/baths 35, price 20, **move-in window 30**, area 25) and `scam_score` (FTC-style heuristics + LLM narrative signals; managed platforms discounted).
- **`geocode.py`** — Nominatim (free, no key), ≥1.1s/request, cached. Adds lat/lng + distance from `criteria.location`.
- **`report.py`** — renders `report.html`, `listings.csv`, and `web/listings.json` (the dashboard's data). `render(..., conn=conn)` enables the geocode pass.
- **`server.py`** — stdlib HTTP server. Serves `web/`, and `POST /scrape` runs the pipeline while **streaming newline-delimited JSON events** to the browser.

## The scrape event stream (server → dashboard)

`server.py` emits one JSON object per line. The dashboard (`web/index.html`, `wireScrape()`) renders a per-source "agent tile" grid from these. Event types:

- `plan {sources:[{id,method}], timeout_s}` — lay out tiles up front.
- `source {id, status: fetching|normalizing|done|error|timeout, found, done, fetch_s, took, kept, error}`.
- `heartbeat {elapsed}` — every 2s, so the UI can tell "alive but slow" from "dead".
- `stage {text}` — scoring / geocoding phases.
- `complete {total, shown, new, elapsed, sources[]}` / `failed {error}` — terminal.

**Observability invariant:** the run must always reach a terminal state. Sources fetch in parallel with a `SOURCE_TIMEOUT_S` deadline; a hung source emits `timeout` instead of wedging the run. `do_POST` wraps `_run_scrape` so any exception emits `failed`. The browser also runs a 15s watchdog on the stream. **If you add work after the source loop, keep it inside this guarantee** — don't add a blocking step that can hang without emitting an event.

## Conventions & gotchas

- **Package manager is `uv`.** Run things with `uv run …`; add deps with `uv add …`.
- **SQLite connections are not shared across threads.** Sources fetch concurrently, but all `normalize`/`db` writes happen on the main thread in `server.py`. Preserve this — don't write to the DB from a worker thread.
- **Craigslist pacing is deliberate** (≥3s/request). It's what prevents IP blocks; don't "optimize" it away. It's the long pole in every run.
- **DeepSeek/Apify cost real money/credits.** Prefer the extract cache; keep Apify opt-in.
- **Nominatim** requires a real User-Agent and ≥1s spacing (already handled in `geocode.py`). Don't parallelize it.
- Structured sources (wesbrook, livrent) skip the LLM — only add DeepSeek calls for genuinely free-text sources.

## Should this be in git?

Yes — commit `CLAUDE.md`. It's project documentation, same as `README.md`.

**Do NOT commit** (already in `.gitignore`): `.env` (API keys), `data/` (the SQLite DB), `report.html`, `listings.csv`, and `web/listings.json` (generated snapshot). If `web/listings.json` was committed earlier as mockup seed data, consider removing it from tracking so real scraped listings don't leak into history.
