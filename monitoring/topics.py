"""Тематическая разметка находок.

Шов под поиск: сегодня 89 запросов из config/queries.yaml работают матчерами
темы и площадки, завтра те же строки уходят в поисковый API, и этот модуль
не меняется.

Совпадением считается присутствие ВСЕХ значимых слов запроса. «Wildberries»
в одиночку не должен давать тему комиссий — иначе разметка вырождается
в «всё про WB».
"""
import re
from collections import Counter
from dataclasses import dataclass

MIN_WORD = 3


@dataclass(frozen=True)
class Matcher:
    words: tuple
    topic: str
    platform: str


def _words(text: str) -> list:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) >= MIN_WORD]


def build_matchers(cfg) -> list:
    matchers = []
    for group in cfg.queries["query_groups"]:
        for query in group["queries"]:
            matchers.append(Matcher(tuple(_words(query["text"])),
                                    query["topic"], query["platform"]))
    return matchers


def _topic_categories(cfg, topic_key: str, haystack: set) -> list:
    """Категории подтем, чьи слова встретились в тексте."""
    found = []
    for topic in cfg.map["topics"]:
        if topic["key"] != topic_key:
            continue
        for sub in topic["subtopics"]:
            sub_words = set(_words(sub["title"]))
            if sub_words and sub_words <= haystack:
                found.append(sub["category"])
        if not found:
            # Тема совпала, конкретная подтема — нет. Берём преобладающую
            # категорию темы, чтобы находка не осталась вовсе без категории.
            counts = Counter(s["category"] for s in topic["subtopics"])
            found.append(counts.most_common(1)[0][0])
    return found


def detect_platform(item, matchers: list, cfg):
    """Площадка по содержанию материала, а не по источнику.

    Статья про Wildberries на отраслевом сайте приходит из источника с
    площадкой CROSS_PLATFORM. Если брать площадку из источника, такой материал
    теряет +25 — самый весомый фактор матрицы, — и вся приоритизация по главной
    площадке канала перестаёт работать ровно там, где она нужнее всего.

    Возвращает площадку с наибольшим приоритетом среди совпавших или None.
    """
    haystack = set(_words(f"{item.title} {item.body}"))

    found = {m.platform for m in matchers
             if m.words and set(m.words) <= haystack
             and m.platform != "CROSS_PLATFORM"}
    if not found:
        return None
    return max(found, key=cfg.platform_priority)


def classify(item, matchers: list, cfg) -> tuple:
    """Возвращает (темы, категории). Пустые кортежи, если ничего не совпало."""
    haystack = set(_words(f"{item.title} {item.body}"))

    topics = []
    for matcher in matchers:
        if matcher.words and set(matcher.words) <= haystack:
            if matcher.topic not in topics:
                topics.append(matcher.topic)

    if not topics:
        return (), ()

    categories = []
    for topic_key in topics:
        for category in _topic_categories(cfg, topic_key, haystack):
            if category not in categories:
                categories.append(category)

    return tuple(topics), tuple(categories)
