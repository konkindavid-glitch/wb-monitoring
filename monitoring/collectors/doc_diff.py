"""Обнаружение тихих правок документов сравнением снапшотов.

Самый ценный сигнал карты: правку оферты или тарифа не сопровождает ни одна
публикация, и узнать о ней можно только сравнив состояние страницы.

Главная опасность — шум. Площадки правят вёрстку, крутят счётчики просмотров
и пишут время генерации страницы. Без нормализации коллектор кричал бы на
каждом опросе, и ему перестали бы верить. Поэтому normalize_dom вырезает
служебное, а хеш считается от смысла, а не от разметки.
"""
import difflib
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from monitoring.normalize import make_item

_DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript")
_DROP_SELECTORS = ".views, .counter, .timestamp, .updated, .rendered-at"
_DROP_PATTERNS = re.compile(
    r"просмотр\w*\s*:?\s*\d+"
    r"|сформировано\s+[\d.:\s]+"
    r"|обновлено\s+[\d.:\s]+"
    r"|\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}",
    re.I)


class SnapshotStore:
    """Снапшоты страниц.

    В Амвере живут на /data: без постоянного диска doc_diff после каждого
    перезапуска не с чем сравнивать и молчит ровно один цикл.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, key: str):
        path = self.root / f"{key}.txt"
        if not path.exists():
            return None
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def save(self, key: str, text: str) -> None:
        with io.open(self.root / f"{key}.txt", "w", encoding="utf-8") as fh:
            fh.write(text)


def normalize_dom(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for node in soup.select(_DROP_SELECTORS):
        node.decompose()
    text = soup.get_text(" ")
    text = _DROP_PATTERNS.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def content_hash(html: str) -> str:
    return hashlib.sha256(normalize_dom(html).encode("utf-8")).hexdigest()


def collect(source: dict, fetcher, store, now: datetime) -> list:
    result = fetcher.get(source["url"], source.get("etag"),
                         source.get("last_modified"))
    if result.status != 200 or not result.text:
        return []

    fresh = normalize_dom(result.text)
    previous = store.load(source["key"])
    store.save(source["key"], fresh)

    # Первый проход не с чем сравнивать — это не изменение, а начало наблюдения.
    if previous is None or previous == fresh:
        return []

    diff = [line for line in difflib.unified_diff(
        previous.split(". "), fresh.split(". "), lineterm="", n=1)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    if not diff:
        return []

    return [make_item(source, {
        "url": source["url"],
        "title": f"Изменение документа: {source.get('title', source['key'])}",
        "body": " ".join(diff)[:4000],
        "published_at": now,
        "signal": source.get("signal", "doc_change"),
    }, now)]
