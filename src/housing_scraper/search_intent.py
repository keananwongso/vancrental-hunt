"""Turn a plain-text housing request into structured search criteria.

Used by the landing page: the user types "2 bed 2 bath near UBC under $3500,
moving in September" and we parse it into the fields criteria.yaml needs,
asking clarifying questions only when an essential field is missing.
"""

from __future__ import annotations

import json
import os

import yaml
from openai import OpenAI

from .config import PROJECT_ROOT
from .geocode import geocode_address

PARSE_PROMPT = """You convert a renter's plain-text request into structured search criteria for Vancouver, BC.

Return ONLY JSON:
{
  "criteria": {
    "beds": number or null,
    "baths": number or null,
    "max_price": number or null,        // CAD/month
    "min_price": number or null,        // optional; default null
    "move_in_date": "YYYY-MM-DD" or null,   // their target move-in
    "move_in_flex_days": number or null,    // +/- days of flexibility, default 14 if a date is given
    "areas": [string, ...],             // neighborhoods/areas mentioned, best-first; [] if none
    "center_address": string or null    // a specific address/landmark to measure distance from, if stated
  },
  "questions": [                        // ONLY for missing ESSENTIAL fields (beds, max_price, move_in_date)
    {"field": "max_price", "question": "What's your monthly budget (CAD)?"}
  ],
  "summary": "one short sentence restating what they're looking for"
}

Rules:
- Essentials are: beds, max_price, move_in_date. If any is missing/unclear, add ONE question for it. Ask nothing about non-essentials.
- If they clearly imply a value (e.g. "for two people" -> beds likely 2), fill it and don't ask.
- Never invent a budget or date. If not stated, leave null and ask.
- areas: extract place names as written (e.g. "UBC", "Kitsilano", "near Wesbrook")."""


def _client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    return OpenAI(api_key=key, base_url="https://api.deepseek.com")


def parse_request(text: str, prior_answers: dict | None = None) -> dict:
    """Parse free text (plus any prior clarifying answers) into criteria + questions."""
    from datetime import date

    user = text.strip()
    if prior_answers:
        user += "\n\nAdditional answers:\n" + "\n".join(
            f"- {k}: {v}" for k, v in prior_answers.items()
        )
    # anchor relative dates ("September", "next month") to today
    system = PARSE_PROMPT + f"\n\nToday's date is {date.today().isoformat()}. " \
        "Interpret relative dates (e.g. a bare month) as the NEXT such date from today, never in the past."
    resp = _client().chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user[:4000]},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


def criteria_from_parsed(parsed: dict, conn=None) -> dict:
    """Turn parsed criteria into the criteria.yaml structure (geocoding the
    center address if one was given)."""
    c = parsed.get("criteria", {})
    from datetime import date, timedelta

    target = c.get("move_in_date")
    flex = c.get("move_in_flex_days") or 14
    move_in = {"target": target, "window_days": flex} if target else {
        "target": date.today().isoformat(), "window_days": 30
    }

    out: dict = {
        "beds": c.get("beds") or 2,
        "baths": c.get("baths") or 1,
        "max_price": c.get("max_price") or 5000,
        "min_price": c.get("min_price") or 0,
        "move_in": move_in,
        "areas": [a.lower() for a in (c.get("areas") or [])] or ["vancouver"],
        "craigslist_queries": _craigslist_queries(c.get("areas") or []),
    }

    addr = c.get("center_address")
    if addr:
        loc = geocode_address(addr, conn=conn)
        if loc:
            out["location"] = {"lat": loc[0], "lng": loc[1], "radius_km": 15}
    return out


def _craigslist_queries(areas: list[str]) -> list[str]:
    # first area as a targeted query, plus an empty citywide sweep
    queries = []
    if areas:
        queries.append(areas[0].lower())
    queries.append("")
    return queries


def write_criteria(criteria: dict) -> None:
    """Persist to criteria.yaml so the next scrape uses it."""
    path = PROJECT_ROOT / "criteria.yaml"
    with open(path, "w") as f:
        f.write("# Search criteria — generated from your search request\n")
        yaml.safe_dump(criteria, f, sort_keys=False, default_flow_style=False)
