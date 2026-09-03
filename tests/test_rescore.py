"""Пересчёт корпуса: находки, размеченные без работающего классификатора."""
from datetime import datetime, timedelta, timezone

import app
from monitoring.config import load_config

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)

ROW = {
    "hit_id": "hit_1", "url": "https://x.invalid/1", "url_hash": "h1",
    "title": "Wildberries меняет тариф хранения с 3 сентября",
    "excerpt": "Базовая ставка вырастет с 0,3 до 0,5 рубля за литр в сутки.",
    "source_key": "src_new_retail", "source_tier": "T2",
    "platforms": ["WILDBERRIES"], "topics": ["seller_money"],
    "discovered_at": NOW - timedelta(hours=2), "published_at": NOW - timedelta(hours=3),
    "decision": "BACKLOG",
}


class Repo:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [dict(ROW)]
        self.updated = []

    def hits_to_rescore(self, hours, limit):
        return self.rows[:limit]

    def update_score(self, hit_id, result, reason):
        self.updated.append((hit_id, result.decision, result.score, reason))

    def rollback(self):
        pass


class Judge:
    """Размечает так, как разметил бы работающий классификатор."""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        if self.payload is not None:
            return self.payload
        import json
        return json.dumps({"h1": {
            "seller_money_impact": "расходы на хранение",
            "rules_change": "новая тарифная сетка",
            "has_practical_takeaway": "пересчитать себестоимость",
            "mass_effect": "все поставки FBO",
        }}, ensure_ascii=False)


def deps_with(repo, judge):
    return app.Deps(cfg=load_config(app.ROOT), repo=repo, judge=judge)


def test_rescore_lifts_a_find_that_was_scored_blind():
    """Корпус размечался, пока классификатор лежал: без семи факторов
    из четырнадцати потолок 65 из 160, и почти всё уходило в DROP."""
    repo = Repo()
    report = app.rescore(deps_with(repo, Judge()), limit=10)

    assert repo.updated, report
    hit_id, decision, score, reason = repo.updated[0]
    assert hit_id == "hit_1"
    assert decision in ("URGENT", "QUEUE")
    assert "пересчёт" in reason


def test_unchanged_band_is_not_written():
    """Лишний переход в журнале выглядел бы как движение, которого не было.
    Без факторов-суждений находка набирает те же 55 — это её прежняя полоса."""
    repo = Repo()
    app.rescore(deps_with(repo, Judge("{}")), limit=10)
    assert repo.updated == []


def test_report_says_plainly_that_marking_was_not_the_problem():
    report = app.rescore(deps_with(Repo(), Judge("{}")), limit=10)
    assert "не в разметке" in report
    assert "источники" in report


def test_backlog_is_rescored_too():
    """Без факторов-суждений типичная находка набирает около 55 — это
    BACKLOG, и до QUEUE ей не хватает пяти баллов. Ограничься мы
    отброшенными, мимо прошли бы те, кому подняться проще всего."""
    repo = Repo()
    app.rescore(deps_with(repo, Judge()), limit=10)
    assert repo.updated[0][1] in ("URGENT", "QUEUE")


def test_nothing_to_rescore_is_stated_not_silent():
    report = app.rescore(deps_with(Repo(rows=[]), Judge()), limit=10)
    assert "нечего" in report


def test_missing_model_refuses_instead_of_pretending():
    report = app.rescore(deps_with(Repo(), None), limit=10)
    assert "нет ключа модели" in report


def test_classifier_failure_is_reported_and_nothing_is_written():
    """Записать оценку без факторов-суждений значило бы повторить
    ту самую ошибку, ради которой пересчёт и делается."""
    class Broken:
        def complete(self, prompt):
            raise RuntimeError("402 Payment Required")

    repo = Repo()
    report = app.rescore(deps_with(repo, Broken()), limit=10)

    assert "не ответил" in report
    assert repo.updated == []


def test_limit_is_bounded_by_default():
    """Классификатор берёт около $0,0007 за материал: фоновая задача
    без потолка однажды съела бы бюджет молча."""
    assert app.RESCORE_LIMIT <= 200
