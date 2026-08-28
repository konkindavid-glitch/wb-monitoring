"""Сбор из RSS и Atom-лент."""
import calendar
from datetime import datetime, timezone

import feedparser

from monitoring.normalize import make_item


def _published(entry):
    """Дата публикации или None.

    Источник без дат ломает свежесть, поэтому None здесь честнее любой догадки:
    такой элемент не сможет получить фактор is_fresh.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def collect(source: dict, fetcher, now: datetime) -> list:
    result = fetcher.get(source["url"], source.get("etag"),
                         source.get("last_modified"))
    if result.status != 200 or not result.text:
        return []

    feed = feedparser.parse(result.text)
    items = []
    for entry in feed.entries:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "")
        if not url or not title:
            continue
        body = getattr(entry, "summary", "") or getattr(entry, "description", "")
        items.append(make_item(source, {
            "url": url,
            "title": title,
            "body": body,
            "published_at": _published(entry),
        }, now))
    return items
