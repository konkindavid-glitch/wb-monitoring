"""Подтверждение события несколькими независимыми источниками.

Одна и та же новость приходит из трёх изданий — это три независимых
подтверждения, и материал не должен получать штраф −50 за их отсутствие.
Без этого модуля фактор no_confirmation срабатывает на всём, что не пришло
из официального источника, и весь механизм подъёма из BACKLOG (+65) мёртв.

Сходство считается по пересечению значимых слов заголовка, а не по эмбеддингам:
для «та же новость в другом издании» этого достаточно, а зависимостей и
стоимости не добавляет. Тонкая дедупликация — задача downstream.
"""
import re

MIN_WORD = 4
MIN_OVERLAP = 0.5
MIN_WORDS = 3

_STOP = {
    "который", "которая", "которые", "после", "перед", "будет", "будут",
    "может", "могут", "своих", "этого", "более", "менее", "также", "чтобы",
    "стал", "стали", "года", "году", "если", "чего", "além",
}


def _key_words(title: str) -> set:
    words = {w for w in re.findall(r"\w+", title.lower())
             if len(w) >= MIN_WORD and w not in _STOP}
    return words


def same_story(a, b) -> bool:
    """Один ли это сюжет.

    Требуется и пересечение слов, и совпадение площадки: «Wildberries меняет
    тариф» и «Ozon меняет тариф» делят половину слов, но это разные события.
    """
    if a.platform != b.platform:
        return False

    wa, wb = _key_words(a.title), _key_words(b.title)
    if len(wa) < MIN_WORDS or len(wb) < MIN_WORDS:
        return False

    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= MIN_OVERLAP


def count_independent_sources(item, pool) -> int:
    """Сколько различных источников освещают тот же сюжет, включая сам item.

    Считаются именно источники, а не элементы: три перепечатки одного агентства
    на одном сайте — это одно подтверждение, а не три.
    """
    sources = {item.source_key}
    for other in pool:
        if other.url_hash == item.url_hash:
            continue
        if other.source_key in sources:
            continue
        if same_story(item, other):
            sources.add(other.source_key)
    return len(sources)


def repeats_of(item, pool) -> bool:
    """Есть ли в пуле тот же сюжет из того же источника.

    Это уже повтор, а не подтверждение: издание переписало свою же новость.
    """
    return any(other.url_hash != item.url_hash
               and other.source_key == item.source_key
               and same_story(item, other)
               for other in pool)
