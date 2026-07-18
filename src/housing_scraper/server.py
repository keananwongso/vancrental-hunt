"""Local dashboard server: serves web/ and runs the scraper on POST /scrape.

`uv run housing-scraper serve` → http://localhost:5173

GET  /            -> web/index.html and static assets (incl. listings.json)
POST /scrape      -> runs a scrape, streaming progress lines back to the browser,
                     then regenerates web/listings.json so a reload shows fresh data.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import time
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .config import PROJECT_ROOT

WEB_DIR = PROJECT_ROOT / "web"
_scrape_lock = threading.Lock()


def _wait_any(pending: set, timeout: float) -> tuple[set, set]:
    """Wait up to `timeout` for at least one future to finish.

    Returns (done, still_pending). If the timeout elapses with nothing done,
    `done` is empty — the caller uses that to declare a hang.
    """
    from concurrent.futures import wait, FIRST_COMPLETED

    done, still = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
    return done, still

# A source that hasn't returned in this long is treated as hung and reported as
# timed-out, so the run can finish instead of spinning forever. Set generously:
# Craigslist alone paces ~60 pages × 3s ≈ 3+ min by design, so this must clear
# the slowest *healthy* source with margin.
SOURCE_TIMEOUT_S = 600
HEARTBEAT_S = 2.0  # keep-alive ping so the UI can tell "still alive" from "died"


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/geocode":
            self._handle_geocode(query)
            return
        if path == "/parse-search":
            self._handle_parse_search()
            return
        if path == "/apply-criteria":
            self._handle_apply_criteria()
            return
        if path != "/scrape":
            self.send_error(404)
            return
        if not _scrape_lock.acquire(blocking=False):
            self.send_error(409, "A scrape is already running")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            use_apify = "apify=1" in query  # opt-in: Apify actors spend credits
            try:
                self._run_scrape(use_apify=use_apify)
            except Exception as e:
                # guarantee a terminal event so the UI never spins forever
                traceback.print_exc()
                self._emit(type="failed", error=str(e)[:200])
        finally:
            _scrape_lock.release()

    def _handle_geocode(self, query: str):
        """POST /geocode?q=<address> → {lat, lng} or 404 if not found.
        Lets the UI re-center the distance filter on any pasted address."""
        from urllib.parse import parse_qs
        from . import db
        from .geocode import geocode_address

        address = (parse_qs(query).get("q") or [""])[0].strip()
        if not address:
            self.send_error(400, "missing q")
            return
        result = geocode_address(address, conn=db.connect())
        if result is None:
            self.send_error(404, "address not found")
            return
        self._send_json({"lat": result[0], "lng": result[1], "address": address})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_parse_search(self):
        """POST /parse-search {text, answers?} → {criteria, questions, summary}.
        Parses a plain-text housing request into structured criteria, asking
        clarifying questions only when an essential field is missing."""
        from .search_intent import parse_request
        try:
            data = self._read_json_body()
            parsed = parse_request(data.get("text", ""), data.get("answers"))
            self._send_json(parsed)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)[:200]}, status=500)

    def _handle_apply_criteria(self):
        """POST /apply-criteria {parsed} OR {criteria} → writes criteria.yaml.
        Accepts either the raw parse-search output (with a `criteria` sub-object)
        or a ready criteria.yaml dict, so the manual params panel can post directly."""
        from . import db
        from .search_intent import criteria_from_parsed, write_criteria
        try:
            data = self._read_json_body()
            if "criteria" in data and "move_in" not in data.get("criteria", {}):
                # parse-search shape → convert (geocodes center address)
                criteria = criteria_from_parsed(data, conn=db.connect())
            else:
                # already a criteria.yaml-shaped dict (from manual params)
                criteria = data.get("criteria", data)
                addr = criteria.pop("_center_address", None)
                if addr:
                    from .geocode import geocode_address
                    loc = geocode_address(addr, conn=db.connect())
                    if loc:
                        criteria["location"] = {"lat": loc[0], "lng": loc[1], "radius_km": 15}
            write_criteria(criteria)
            self._send_json({"ok": True, "criteria": criteria})
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)[:200]}, status=500)

    _emit_lock = threading.Lock()

    def _emit(self, **event) -> None:
        """Send one JSON status event as a line (newline-delimited JSON stream).

        Thread-safe: fetch workers run concurrently and all write to the same socket.
        """
        with self._emit_lock, contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write((json.dumps(event) + "\n").encode())
            self.wfile.flush()

    def _run_scrape(self, use_apify: bool = False) -> None:
        # import here so the server starts fast and errors surface per-request
        from . import db
        from .config import load_criteria
        from .extract import normalize
        from .models import Listing
        from .report import listing_to_dict, render
        from .score import score_all
        from .sources.craigslist import CraigslistSource
        from .sources.livrent import LivRentSource
        from .sources.wesbrook import WesbrookSource

        criteria = load_criteria()
        conn = db.connect()
        # Persistent: listings accumulate across runs. Each run stamps the
        # listings it sees with `run_id`; anything from a finished source with
        # an older stamp is "gone" (delisted). Brand-new listings get is_new.
        run_id = datetime.now().isoformat()
        sources = {
            "wesbrook": WesbrookSource(),
            "livrent": LivRentSource(),
            "craigslist": CraigslistSource(),
        }
        # each source declares its access method for the tile subtitle
        methods = {"wesbrook": "admin-ajax", "livrent": "GraphQL/SSR",
                   "craigslist": "HTML + DeepSeek"}
        if use_apify:
            try:
                from .sources.apify_actors import apify_sources
                extra = apify_sources()
                sources.update(extra)
                methods.update({k: "Apify actor" for k in extra})
            except Exception as e:
                self._emit(type="note", text=f"Apify sources skipped ({e})")

        run_start = time.monotonic()

        # announce the full plan up-front so the UI can lay out all tiles at once
        self._emit(type="plan", sources=[
            {"id": name, "method": methods.get(name, "")} for name in sources
        ], timeout_s=SOURCE_TIMEOUT_S)

        # Heartbeat: a background ping every HEARTBEAT_S with overall elapsed time.
        # Lets the browser distinguish "still running" from "connection died".
        stop_heartbeat = threading.Event()

        def heartbeat():
            while not stop_heartbeat.wait(HEARTBEAT_S):
                self._emit(type="heartbeat", elapsed=round(time.monotonic() - run_start, 1))

        hb_thread = threading.Thread(target=heartbeat, daemon=True)
        hb_thread.start()

        # Fetch every source concurrently (all network-bound). Each tile goes
        # "fetching" immediately; results are normalized on the main thread as
        # they arrive, since the SQLite connection and extract cache aren't
        # safe to share across threads.
        def fetch_one(name, source):
            t0 = time.monotonic()
            self._emit(type="source", id=name, status="fetching")
            # throttle progress emits so a fast source doesn't flood the stream
            last = [0.0]

            def on_progress(done, total):
                now = time.monotonic()
                if now - last[0] >= 0.4 or done == total:
                    last[0] = now
                    self._emit(type="source", id=name, status="fetching",
                               fetched=done, fetch_total=total)

            raws = source.fetch(criteria, on_progress)
            return name, raws, round(time.monotonic() - t0, 1)

        new_ids: set[str] = set()
        source_stats: list[dict] = []
        pool = ThreadPoolExecutor(max_workers=len(sources))
        futures = {pool.submit(fetch_one, n, s): n for n, s in sources.items()}
        pending = set(futures)
        try:
            # Drain futures as each finishes. Each source has its OWN deadline
            # (SOURCE_TIMEOUT_S from run start), so a slow-but-healthy source
            # (Craigslist takes minutes by design) never falsely times out
            # another one. We wake up at least once a second to re-check.
            while pending:
                done, pending = _wait_any(pending, timeout=1.0)
                if not done:
                    # nobody finished this second — time out only sources past their deadline
                    elapsed = time.monotonic() - run_start
                    if elapsed < SOURCE_TIMEOUT_S:
                        continue  # still within budget; keep waiting (heartbeat keeps UI alive)
                    for fut in list(pending):
                        name = futures[fut]
                        self._emit(type="source", id=name, status="timeout",
                                   error=f"no response in {SOURCE_TIMEOUT_S}s")
                        source_stats.append({"id": name, "status": "timeout"})
                    break
                for fut in done:
                    name = futures[fut]
                    try:
                        _, raws, fetch_s = fut.result()
                    except Exception as e:
                        self._emit(type="source", id=name, status="error", error=str(e)[:160])
                        source_stats.append({"id": name, "status": "error"})
                        continue
                    total = len(raws)
                    self._emit(type="source", id=name, status="normalizing",
                               found=total, done=0, fetch_s=fetch_s)
                    norm_start = time.monotonic()
                    kept = 0
                    this_source: list = []
                    for i, raw in enumerate(raws, 1):
                        try:
                            listing = normalize(raw, conn, use_llm=True)
                        except Exception:
                            continue
                        kept += 1
                        listing.is_new = db.upsert(conn, listing, run_id)
                        this_source.append(listing)
                        if i % 5 == 0 or i == total:
                            self._emit(type="source", id=name, status="normalizing",
                                       found=total, done=i,
                                       elapsed=round(time.monotonic() - norm_start, 1))
                    conn.commit()
                    # Any listing from this source not seen this run is delisted.
                    # Load those too, flag them gone, so the UI can grey them out.
                    gone = []
                    for gid in db.gone_ids(conn, name, run_id):
                        row = conn.execute("SELECT json FROM listings WHERE id = ?", (gid,)).fetchone()
                        if row:
                            gl = Listing.model_validate_json(row[0])
                            gl.gone = True
                            gone.append(gl)
                    # Score this source's listings and push them to the UI right
                    # away, so fast sources (Wesbrook ~7s) appear without waiting
                    # for slow ones (Craigslist ~4min). Scoring uses the criteria;
                    # scam market-median is refined in the final full pass.
                    batch = this_source + gone
                    score_all(batch, criteria)
                    self._emit(type="listings", id=name,
                               items=[listing_to_dict(l, criteria) for l in batch])
                    took = round(time.monotonic() - norm_start + fetch_s, 1)
                    self._emit(type="source", id=name, status="done",
                               found=total, kept=kept, took=took)
                    source_stats.append({"id": name, "status": "done",
                                         "found": total, "took": took})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # NOTE: heartbeat stays running through scoring + geocoding below.
        # Geocoding hits Nominatim (~1s/uncached address) and can take minutes;
        # without the heartbeat the stream goes silent and the browser gives up
        # before the final "complete" event.
        try:
            self._emit(type="stage", text="Scoring & ranking…")
            scraped = set(sources)  # only sources we ran can have 'gone' listings
            listings = []
            for l, last_seen in db.all_listings(conn):
                l.gone = l.source in scraped and last_seen != run_id
                l.is_new = db.is_new_since_last_run(conn, l.id, run_id)
                listings.append(l)
            score_all(listings, criteria)
            for l in listings:
                db.update_json(conn, l)               # persist scores, keep last_seen
            conn.commit()
            # show matches, plus any gone listing that still matches (greyed in UI)
            matches = [l for l in listings if l.match_score >= 40]

            self._emit(type="stage", text="Geocoding addresses…")

            geo_last = [0.0]

            def geo_progress(done, total):
                now = time.monotonic()
                if now - geo_last[0] >= 0.5 or done == total:
                    geo_last[0] = now
                    self._emit(type="stage",
                               text=f"Geocoding addresses… {done}/{total}")

            # render writes web/listings.json (open_browser off — we're headless here);
            # passing conn enables the geocode pass with its Nominatim cache.
            with contextlib.redirect_stdout(io.StringIO()):
                render(matches, criteria, new_ids, open_browser=False, conn=conn,
                       geocode_progress=geo_progress)
            # render() populated lat/lng on the listings — persist so coords stick
            # in the DB (otherwise they only live in geocode_cache and are lost).
            for l in matches:
                db.update_json(conn, l)
            conn.commit()
        finally:
            stop_heartbeat.set()

        self._emit(type="complete", total=len(listings), shown=len(matches),
                   elapsed=round(time.monotonic() - run_start, 1),
                   sources=source_stats)

    def log_message(self, *args):  # quieter console
        pass


def serve(port: int = 5173) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(DashboardHandler))
    url = f"http://localhost:{port}"
    print(f"Dashboard: {url}   (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()
