"""Render ranked listings to a self-contained report.html + listings.csv."""

from __future__ import annotations

import csv
import json
import webbrowser
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment

from .config import PROJECT_ROOT, Criteria
from .models import Listing
from .score import scam_badge

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Vancouver Rentals — {{ generated }}</title>
<style>
  :root { --ok:#1a7f37; --warn:#b58900; --bad:#c0392b; --muted:#777; }
  body { font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto; max-width: 1200px; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  .meta { color: var(--muted); margin-bottom: 1.5rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #eee; vertical-align: top; }
  th { position: sticky; top: 0; background: #fff; cursor: default; font-size: .8rem; text-transform: uppercase; color: var(--muted); }
  tr.new td:first-child { border-left: 3px solid #2e86de; }
  .badge { display: inline-block; padding: .05rem .5rem; border-radius: 99px; font-size: .75rem; color: #fff; }
  .badge.low { background: var(--ok); } .badge.medium { background: var(--warn); } .badge.high { background: var(--bad); }
  .pill { display:inline-block; background:#f0f0f0; border-radius:4px; padding:0 .4rem; font-size:.75rem; margin-right:.2rem; }
  .in-window { color: var(--ok); font-weight: 600; }
  .off-window { color: var(--muted); }
  .price-over { color: var(--bad); }
  .score { font-weight: 700; }
  a { color: #2e5cde; text-decoration: none; } a:hover { text-decoration: underline; }
  .src { font-size: .75rem; color: var(--muted); }
</style></head><body>
<h1>🏠 Vancouver Rentals — ranked for you</h1>
<div class="meta">{{ listings|length }} matches · generated {{ generated }} ·
criteria: {{ criteria.beds|int }}bd/{{ criteria.baths|int }}ba ≤ ${{ criteria.max_price|int }} ·
move-in {{ criteria.move_in.start }} → {{ criteria.move_in.end }} · {{ new_count }} new since last run</div>
<table>
<tr><th>Score</th><th>Listing</th><th>Price</th><th>Beds/Baths</th><th>Move-in</th><th>Area</th><th>Sketchy?</th><th>Source</th></tr>
{% for l in listings %}
<tr class="{{ 'new' if l.id in new_ids else '' }}">
  <td class="score">{{ l.match_score|int }}</td>
  <td><a href="{{ l.url }}" target="_blank">{{ l.title[:80] }}</a>
      {% if l.id in new_ids %}<span class="pill" style="background:#dbeafe">NEW</span>{% endif %}
      {% if l.sqft %}<span class="pill">{{ l.sqft }} sqft</span>{% endif %}
      {% if l.furnished %}<span class="pill">furnished</span>{% endif %}
      {% if l.pets %}<span class="pill">pets ok</span>{% endif %}</td>
  <td class="{{ 'price-over' if l.price and l.price > criteria.max_price else '' }}">
      {{ "${:,.0f}".format(l.price) if l.price else "?" }}</td>
  <td>{{ l.beds|int if l.beds else "?" }}bd / {{ l.baths|int if l.baths else "?" }}ba</td>
  <td class="{{ 'in-window' if l.in_window else 'off-window' }}">{{ l.move_in_label }}</td>
  <td>{{ l.area or l.address or "—" }}</td>
  <td><span class="badge {{ l.badge }}">{{ l.badge }}</span>
      {% if l.scam_flags %}<div class="src">{{ l.scam_flags|join(", ") }}</div>{% endif %}</td>
  <td class="src">{{ l.source }}</td>
</tr>
{% endfor %}
</table>
</body></html>"""


def render(
    listings: list[Listing],
    criteria: Criteria,
    new_ids: set[str],
    open_browser: bool = True,
    conn=None,
    geocode_progress=None,
) -> Path:
    for l in listings:
        set_display_fields(l, criteria)

    if conn is not None:
        from .geocode import geocode_listings, haversine_km
        uncached = sum(
            1 for l in listings
            if l.address and not l.lat
        )
        if uncached:
            print(f"Geocoding {uncached} addresses via Nominatim (~{uncached}s)...")
        geocode_listings(listings, conn, progress=geocode_progress)
        if criteria.location:
            clat, clng = criteria.location.lat, criteria.location.lng
            for l in listings:
                if l.lat is not None and l.lng is not None:
                    l.dist_km = round(haversine_km(clat, clng, l.lat, l.lng), 2)

    listings = sorted(listings, key=lambda l: (-l.match_score, l.scam_score))
    html = Environment().from_string(TEMPLATE).render(
        listings=listings,
        criteria=criteria,
        new_ids=new_ids,
        new_count=sum(1 for l in listings if l.id in new_ids),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out = PROJECT_ROOT / "report.html"
    out.write_text(html)

    with open(PROJECT_ROOT / "listings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "title", "price", "beds", "baths", "sqft", "move_in",
                    "area", "scam", "flags", "source", "url"])
        for l in listings:
            w.writerow([l.match_score, l.title, l.price, l.beds, l.baths, l.sqft,
                        l.move_in_label, l.area or l.address, l.badge,  # type: ignore[attr-defined]
                        ";".join(l.scam_flags), l.source, l.url])

    _write_web_json(listings, criteria, new_ids)

    if open_browser:
        webbrowser.open(out.as_uri())
    return out


def set_display_fields(l: Listing, criteria: Criteria) -> None:
    """Fill in_window / move_in_label / badge — the fields the UI shows.
    Called by render() and also by the server when streaming listings early."""
    mi = l.move_in_date
    if isinstance(mi, date):
        l.in_window = criteria.move_in.start <= mi <= criteria.move_in.end  # type: ignore[attr-defined]
        l.move_in_label = mi.strftime("%b %-d, %Y")  # type: ignore[attr-defined]
    else:
        l.in_window = False  # type: ignore[attr-defined]
        l.move_in_label = {"now": "now", "flexible": "flexible"}.get(mi, "unknown — ask")  # type: ignore[attr-defined]
    l.badge = scam_badge(l.scam_score)  # type: ignore[attr-defined]


def listing_to_dict(l: Listing, criteria: Criteria) -> dict:
    """One listing in the shape the dashboard expects. Sets display fields first."""
    set_display_fields(l, criteria)
    return {
        "id": l.id, "source": l.source, "url": l.url, "title": l.title,
        "price": l.price, "beds": l.beds, "baths": l.baths, "sqft": l.sqft,
        "area": l.area or l.address, "move_in": l.move_in_label,  # type: ignore[attr-defined]
        "in_window": l.in_window,  # type: ignore[attr-defined]
        "match_score": l.match_score, "scam": l.badge,  # type: ignore[attr-defined]
        "scam_flags": l.scam_flags, "furnished": l.furnished, "pets": l.pets,
        "lat": l.lat, "lng": l.lng, "maps_url": l.maps_url, "dist_km": l.dist_km,
    }


def criteria_to_dict(criteria: Criteria) -> dict:
    return {
        "beds": criteria.beds,
        "baths": criteria.baths,
        "max_price": criteria.max_price,
        "move_in_start": criteria.move_in.start.isoformat(),
        "move_in_end": criteria.move_in.end.isoformat(),
        "location": {
            "lat": criteria.location.lat,
            "lng": criteria.location.lng,
            "radius_km": criteria.location.radius_km,
        } if criteria.location else None,
    }


def _write_web_json(listings: list[Listing], criteria: Criteria, new_ids: set[str]) -> None:
    """Snapshot for the localhost dashboard at web/index.html."""
    web_dir = PROJECT_ROOT / "web"
    web_dir.mkdir(exist_ok=True)
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "criteria": criteria_to_dict(criteria),
        "listings": [
            {**listing_to_dict(l, criteria), "is_new": l.id in new_ids}
            for l in listings
        ],
    }
    (web_dir / "listings.json").write_text(json.dumps(payload, indent=2))
