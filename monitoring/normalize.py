"""Приведение любого источника к единому SourceItem."""
import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from monitoring.models import SourceItem

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
             "utm_content", "yclid", "gclid", "fbclid", "from", "ref"}


def url_hash(url: str) -> str:
    """Отпечаток адреса без меток трекинга.

    Один материал приходит из нескольких источников с разными utm-хвостами.
    Без очистки он выглядит как несколько разных находок и трижды тратит
    оценку модели.
    """
    parsed = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query)
                       if k.lower() not in _TRACKING])
    normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(),
                             parsed.path.rstrip("/"), "", query, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def clean_text(html: str) -> str:
    """Текст без разметки и лишних пробелов."""
    text = BeautifulSoup(html or "", "lxml").get_text(" ")
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def make_item(source: dict, raw: dict, now: datetime) -> SourceItem:
    return SourceItem(
        source_key=source["key"],
        url=raw["url"],
        url_hash=url_hash(raw["url"]),
        title=clean_text(raw.get("title", "")),
        body=clean_text(raw.get("body", "")),
        discovered_at=now,
        published_at=raw.get("published_at"),
        tier=source.get("tier", "T3"),
        platform=source.get("platform", "CROSS_PLATFORM"),
        signal=raw.get("signal"),
    )
