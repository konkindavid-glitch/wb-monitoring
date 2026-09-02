"""Разбор: расписание выпусков и сборка текста."""
from datetime import datetime, timezone

import app
from monitoring.explainer import (MIN_SOURCE_CHARS, gather_sources,
                                  write_explainer)

SLOTS = ["10:00", "18:00"]

MATERIALS = [
    {"title": "Wildberries вводит проверку GTIN",
     "url": "https://x.invalid/1",
     "text": "С 1 октября вступает в силу закон о платформенной экономике. "
             "Wildberries начнёт проверять GTIN в карточке товара. "
             "Если он не указан или недействителен, карточку заблокируют. " * 6},
    {"title": "Что такое GTIN", "url": "https://x.invalid/2",
     "text": "GTIN — международный номер из 14 знаков, который присваивается "
             "каждому виду товара. Получить его можно у изготовителя. " * 6},
]

TEXT = ("Проверка GTIN с 1 октября\n\nWildberries начнёт проверять GTIN "
        "в карточке товара.\n\n📌 Что нужно сделать?\nУказать GTIN в поле "
        "«Баркоды».")


class Client:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def moscow(hour, minute=0):
    return datetime(2026, 9, 2, hour, minute, tzinfo=timezone.utc) - app.MOSCOW_OFFSET


# --- расписание -------------------------------------------------------------

def test_no_slot_before_the_first_release():
    assert app.due_slot(moscow(9, 30), SLOTS) == ""


def test_morning_slot_after_ten():
    assert app.due_slot(moscow(10, 1), SLOTS) == "10:00"


def test_evening_slot_supersedes_the_morning_one():
    """Если контейнер пролежал до вечера, утренний разбор в 18:20 не нужен —
    он уже неактуален."""
    assert app.due_slot(moscow(18, 20), SLOTS) == "18:00"


def test_slot_boundary_is_inclusive():
    assert app.due_slot(moscow(10, 0), SLOTS) == "10:00"


def test_broken_slot_values_are_ignored_not_fatal():
    assert app.due_slot(moscow(23, 0), ["мусор", "18:00", ""]) == "18:00"


def test_slots_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("EXPLAINER_TIMES", "07:30, 21:00")
    assert app.explainer_slots() == ["07:30", "21:00"]


# --- сборка текста ----------------------------------------------------------

def test_sources_are_joined_with_titles_and_links():
    joined = gather_sources(MATERIALS)
    assert "Wildberries вводит проверку GTIN" in joined
    assert "https://x.invalid/2" in joined


def test_material_without_text_is_skipped():
    joined = gather_sources(MATERIALS + [{"title": "Пустой", "text": ""}])
    assert "Пустой" not in joined


def test_thin_material_is_refused_without_calling_the_model():
    """По одной новости инструкцию не напишешь, а попытка — сочинительство."""
    client = Client("что угодно")
    result = write_explainer("Тема", "коротко", client)
    assert not result
    assert "не хватает" in result.reason
    assert client.prompts == []


def test_threshold_is_higher_than_for_a_news_post():
    from monitoring.writer import MIN_SOURCE_CHARS as NEWS_MINIMUM
    assert MIN_SOURCE_CHARS > NEWS_MINIMUM


def test_clean_explainer_passes():
    result = write_explainer("GTIN", gather_sources(MATERIALS), Client(TEXT))
    assert result.text.startswith("Проверка GTIN")


def test_invented_numbers_are_rejected_after_a_retry():
    """Выдуманный срок в инструкции «что делать» дороже отсутствия инструкции."""
    client = Client("Штраф 87 тысяч рублей.", "И ещё 42 дня на исправление.")
    result = write_explainer("GTIN", gather_sources(MATERIALS), client)
    assert not result
    assert "42" in result.reason


def test_retry_names_the_invented_numbers():
    client = Client("Штраф 87 тысяч.", TEXT)
    assert write_explainer("GTIN", gather_sources(MATERIALS), client)
    assert "87" in client.prompts[1]


def test_missing_model_is_reported():
    result = write_explainer("GTIN", gather_sources(MATERIALS), None)
    assert not result
    assert "ключа модели" in result.reason


def test_explainer_fits_a_photo_caption():
    """Разбор уходит одним постом — картинкой с подписью, — а подпись
    Телеграм ограничивает 1024 знаками."""
    from monitoring.delivery import CAPTION_LIMIT
    from monitoring.explainer import MAX_LENGTH

    client = Client("я" * 9000)
    result = write_explainer("GTIN", gather_sources(MATERIALS), client)
    assert len(result.text) <= MAX_LENGTH == CAPTION_LIMIT == 1024


# --- частота попыток --------------------------------------------------------

def test_first_attempt_is_allowed(monkeypatch):
    monkeypatch.setattr(app, "_EXPLAINER_TRIED", {})
    assert app.explainer_attempt_due("10:00", moscow(11))


def test_attempt_is_not_repeated_every_minute(monkeypatch):
    """Первый выпуск за час сделал полсотни одинаковых записей «нет тем»."""
    monkeypatch.setattr(app, "_EXPLAINER_TRIED", {"10:00": moscow(11)})
    assert not app.explainer_attempt_due("10:00", moscow(11, 5))


def test_attempt_is_retried_later_because_a_topic_may_appear(monkeypatch):
    """Пропуск не окончателен: подходящая тема может появиться днём."""
    monkeypatch.setattr(app, "_EXPLAINER_TRIED", {"10:00": moscow(11)})
    assert app.explainer_attempt_due("10:00", moscow(11, 31))


def test_slots_are_throttled_independently(monkeypatch):
    monkeypatch.setattr(app, "_EXPLAINER_TRIED", {"10:00": moscow(11)})
    assert app.explainer_attempt_due("18:00", moscow(11, 5))


def test_window_is_wide_enough_for_rules_topics():
    """Разбор про правила не протухает за трое суток, а весомых тем
    за такое окно может не набраться вовсе — что и случилось."""
    assert app.EXPLAINER_WINDOW_HOURS >= 168


# --- бюджет времени ---------------------------------------------------------

class Repo:
    def __init__(self, candidates):
        self.candidates = candidates

    def explainer_candidates(self, hours, limit=5):
        return self.candidates[:limit]

    def related_hits(self, hit, hours, limit=6):
        return []


def test_assembly_gives_up_instead_of_stalling_the_loop(monkeypatch):
    """Пока собирается разбор, бот не слышит кнопок и не делает тиков.
    Пять тем по семь статей с повторами — это полчаса молчания."""
    monkeypatch.setattr(app, "EXPLAINER_BUDGET_SECONDS", 0)
    deps = app.Deps(cfg=None, repo=Repo([{"hit_id": "h1", "title": "Т",
                                          "url": "https://x.invalid/1"}]))

    hit, reason = app.build_explainer(deps, "10:00")
    assert hit is None
    assert "время" in reason


def test_only_a_few_topics_are_tried():
    """Перебирать всё найденное дороже, чем выпустить по первой подходящей."""
    assert app.EXPLAINER_CANDIDATES <= 3
    assert app.EXPLAINER_RELATED <= 3


def test_slow_sources_do_not_consume_the_whole_budget(monkeypatch):
    """Бюджет проверяется и внутри темы, а не только между темами."""
    fetched = []
    monkeypatch.setattr(app, "EXPLAINER_BUDGET_SECONDS", 1)
    monkeypatch.setattr(app, "article_text",
                        lambda url, fetcher: fetched.append(url) or "")

    class Slow(Repo):
        def related_hits(self, hit, hours, limit=6):
            return [{"hit_id": f"r{i}", "title": "Р",
                     "url": f"https://x.invalid/r{i}"} for i in range(limit)]

    deps = app.Deps(cfg=None, repo=Slow([{"hit_id": "h1", "title": "Т",
                                          "url": "https://x.invalid/1"}]))
    app.build_explainer(deps, "10:00")
    assert len(fetched) <= 1 + app.EXPLAINER_RELATED


def test_hit_without_platforms_asks_the_database_nothing():
    """Пустой массив площадок в запросе не нужен: ответ заведомо пуст,
    а приведение типов на пустом массиве — лишний способ ошибиться."""
    import importlib.util

    import pytest
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg локально не установлен")

    class Spy:
        def __init__(self):
            self.asked = False

        def cursor(self):
            self.asked = True
            raise AssertionError("запрос не должен уходить")

    from monitoring.db import Repo
    repo = Repo.__new__(Repo)
    repo.conn = Spy()
    assert repo.related_hits({"hit_id": "h1", "platforms": []}, 168) == []
    assert not repo.conn.asked
