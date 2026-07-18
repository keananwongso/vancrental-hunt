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
# timed-out, so the run can finish instead of spinning forever.
SOURCE_TIMEOUT_S = 300
HEARTBEAT_S = 2.0  # keep-alive ping so the UI can tell "still alive" from "died"


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_POST(self):
        path, _, query = self.path.partition("?")
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
        from .report import render
        from .score import score_all
        from .sources.craigslist import CraigslistSource
        from .sources.livrent import LivRentSource
        from .sources.wesbrook import WesbrookSource

        criteria = load_criteria()
        conn = db.connect()
        known = {l.url for l, _ in db.all_listings(conn)}
        sources = {
            "wesbrook": WesbrookSource(),
            "livrent": LivRentSource(),
            "craigslist": CraigslistSource(known_urls=known),
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
            raws = source.fetch(criteria)
            return name, raws, round(time.monotonic() - t0, 1)

        new_ids: set[str] = set()
        source_stats: list[dict] = []
        pool = ThreadPoolExecutor(max_workers=len(sources))
        futures = {pool.submit(fetch_one, n, s): n for n, s in sources.items()}
        pending = set(futures)
        try:
            # Drain futures with an overall deadline so one hung source can't wedge the run.
            while pending:
                remaining = SOURCE_TIMEOUT_S - (time.monotonic() - run_start)
                done, pending = _wait_any(pending, timeout=max(remaining, 0))
                if not done:  # deadline hit — mark every still-pending source as timed out
                    for fut in pending:
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
                    for i, raw in enumerate(raws, 1):
                        try:
                            listing = normalize(raw, conn, use_llm=True)
                        except Exception:
                            continue
                        kept += 1
                        if db.upsert(conn, listing):
                            new_ids.add(listing.id)
                        if i % 5 == 0 or i == total:
                            self._emit(type="source", id=name, status="normalizing",
                                       found=total, done=i,
                                       elapsed=round(time.monotonic() - norm_start, 1))
                    conn.commit()
                    took = round(time.monotonic() - norm_start + fetch_s, 1)
                    self._emit(type="source", id=name, status="done",
                               found=total, kept=kept, took=took)
                    source_stats.append({"id": name, "status": "done",
                                         "found": total, "took": took})
        finally:
            stop_heartbeat.set()
            pool.shutdown(wait=False, cancel_futures=True)

        self._emit(type="stage", text="Scoring & ranking…")
        listings = [l for l, _ in db.all_listings(conn)]
        score_all(listings, criteria)
        for l in listings:
            db.upsert(conn, l)
        conn.commit()
        matches = [l for l in listings if l.match_score >= 40]

        self._emit(type="stage", text="Geocoding & writing report…")
        # render writes web/listings.json (open_browser off — we're headless here);
        # passing conn enables the geocode pass with its Nominatim cache.
        with contextlib.redirect_stdout(io.StringIO()):
            render(matches, criteria, new_ids, open_browser=False, conn=conn)

        self._emit(type="complete", total=len(listings), shown=len(matches),
                   new=len(new_ids), elapsed=round(time.monotonic() - run_start, 1),
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
