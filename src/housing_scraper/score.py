"""Match scoring (criteria fit, move-in window) and scam scoring."""

from __future__ import annotations

import re
from datetime import date
from statistics import median

from .config import Criteria
from .models import Listing

PAYMENT_RED_FLAGS = re.compile(
    r"wire transfer|western union|moneygram|gift card|bitcoin|crypto|zelle|cash\s?app"
    r"|deposit to (hold|secure)|send (the )?deposit",
    re.I,
)
STORY_RED_FLAGS = re.compile(
    r"out of (the )?country|overseas|missionary|deployed|mail(ing)? (you )?the keys"
    r"|can'?t show|no (viewing|showing)s?|god bless",
    re.I,
)
URGENCY_RED_FLAGS = re.compile(
    r"act (fast|now)|first come first serve|many (people )?interested|decide today|won'?t last",
    re.I,
)
FREE_EMAIL = re.compile(r"\b[\w.+-]+@(gmail|yahoo|hotmail|outlook|aol)\.\w+", re.I)

LLM_SIGNAL_WEIGHTS = {
    "wire_or_gift_payment": 35,
    "deposit_before_viewing": 30,
    "landlord_abroad": 30,
    "no_viewing_offered": 20,
    "urgency_pressure": 10,
    "too_good_price": 15,
    "off_platform_contact": 10,
    "excessive_personal_info": 15,
}


def score_all(listings: list[Listing], criteria: Criteria) -> None:
    price_per_bed = [
        l.price / l.beds for l in listings if l.price and l.beds and l.price > 500
    ]
    market = median(price_per_bed) if price_per_bed else None
    for l in listings:
        l.match_score = _match(l, criteria)
        l.scam_score, extra_flags = _scam(l, market)
        l.scam_flags = sorted(set(l.scam_flags) | extra_flags)


def _match(l: Listing, c: Criteria) -> float:
    score = 0.0
    # beds/baths (35)
    if l.beds == c.beds:
        score += 20
    elif l.beds and abs(l.beds - c.beds) <= 1:
        score += 5
    if l.baths and l.baths >= c.baths:
        score += 15
    # price (20)
    if l.price:
        if l.price <= c.max_price:
            score += 20
        elif l.price <= c.max_price * 1.1:
            score += 8  # slightly over budget, maybe negotiable
    # move-in window (30) — the core criterion
    mi = l.move_in_date
    if isinstance(mi, date):
        if c.move_in.start <= mi <= c.move_in.end:
            score += 30
        elif mi < c.move_in.start:
            score += 5  # earlier than window; you'd pay dead months
        else:
            score += 12  # later; might still work
    elif mi in ("now", "flexible") or mi is None:
        score += 15  # unknown — worth asking
    # area preference (25), earlier in the list = stronger
    hay = " ".join(filter(None, [l.area, l.address, l.title, l.description or ""])).lower()
    for rank, area in enumerate(c.areas):
        if area.lower() in hay:
            score += max(25 - rank * 4, 5)
            break
    return round(score, 1)


def _scam(l: Listing, market_price_per_bed: float | None) -> tuple[float, set[str]]:
    score = 0.0
    flags: set[str] = set()
    text = l.description or ""
    if PAYMENT_RED_FLAGS.search(text):
        score += 35
        flags.add("payment_red_flag")
    if STORY_RED_FLAGS.search(text):
        score += 25
        flags.add("landlord_story")
    if URGENCY_RED_FLAGS.search(text):
        score += 10
        flags.add("urgency")
    if FREE_EMAIL.search(text):
        score += 10
        flags.add("free_email_contact")
    if market_price_per_bed and l.price and l.beds:
        ratio = (l.price / l.beds) / market_price_per_bed
        if ratio < 0.55:
            score += 35
            flags.add("price_far_below_market")
        elif ratio < 0.7:
            score += 20
            flags.add("price_below_market")
    for signal in l.scam_flags:  # LLM-detected narrative signals
        score += LLM_SIGNAL_WEIGHTS.get(signal, 10) * 0.6  # discounted vs hard regex hits
    if l.source in ("wesbrook", "livrent"):
        score *= 0.3  # verified/managed platforms
    return min(round(score, 1), 100.0), flags


def scam_badge(score: float) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"
