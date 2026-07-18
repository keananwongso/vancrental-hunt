"""Load criteria.yaml and .env."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")


class MoveInWindow(BaseModel):
    target: date
    window_days: int = 14

    @property
    def start(self) -> date:
        return self.target - timedelta(days=self.window_days)

    @property
    def end(self) -> date:
        return self.target + timedelta(days=self.window_days)


class Location(BaseModel):
    lat: float
    lng: float
    radius_km: float = 10.0


class Criteria(BaseModel):
    beds: float = 2
    baths: float = 2
    max_price: float = 3500
    min_price: float = 1500
    move_in: MoveInWindow
    areas: list[str] = []
    craigslist_queries: list[str] = [""]
    location: Location | None = None


def load_criteria(path: Path | None = None) -> Criteria:
    path = path or PROJECT_ROOT / "criteria.yaml"
    # First run (fresh clone): seed criteria.yaml from the tracked template.
    if not path.exists():
        example = PROJECT_ROOT / "criteria.example.yaml"
        if example.exists():
            path.write_text(example.read_text())
    with open(path) as f:
        return Criteria.model_validate(yaml.safe_load(f))
