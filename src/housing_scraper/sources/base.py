"""Source interface + shared HTTP helper."""

from __future__ import annotations

from abc import ABC, abstractmethod

from curl_cffi import requests

from ..config import Criteria
from ..models import RawListing

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def http_session() -> requests.Session:
    return requests.Session(impersonate="chrome", headers={"User-Agent": UA}, timeout=30)


class Source(ABC):
    name: str

    @abstractmethod
    def fetch(self, criteria: Criteria) -> list[RawListing]: ...
