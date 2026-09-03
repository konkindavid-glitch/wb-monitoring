"""Дочитывание материалов перед разметкой.

RSS отдаёт заголовок и пару строк анонса. Классификатор при этом получал
`item.body` длиной в сотню знаков и честно не находил ни одного фактора:
по анонсу «Продавцы пожаловались на задержку выплат» нельзя сказать,
меняются ли правила и есть ли практический вывод.

Видно это стало на боевых данных. После снятия штрафа за неподтверждённость
семь находок поднялись ровно до 40 баллов — это `platform_wb` +25 плюс
`is_fresh` +15 и ни одного из семи факторов-суждений. Ровное число у всех
семи и выдало причину: судить было не по чему.

Поэтому перед разметкой материал дочитывается со страницы. Дороже это
не становится — в подсказку и так уходило не больше 1200 знаков, просто
теперь они осмысленные.
"""
import time
from dataclasses import replace

# Ниже этого в теле нечего разбирать: это анонс, а не материал.
THIN_BODY = 400

# Потолок на дочитывание. Загрузчик делает до трёх попыток с нарастающим
# ожиданием, и сотня мёртвых адресов иначе останавливает цикл надолго:
# пока идёт тик, бот не слышит кнопок.
BUDGET_SECONDS = 90


def enrich(items, fetcher, *, budget: float = BUDGET_SECONDS, stats=None):
    """Дочитывает тонкие материалы. Возвращает новый список.

    Ошибка загрузки — не повод терять находку: остаётся то, что было.
    Исчерпался бюджет — остальные тоже остаются как есть, но об этом
    говорится в stats, а не замалчивается.
    """
    from monitoring.writer import article_text

    counters = {"fetched": 0, "skipped": 0, "thin": 0}
    deadline = time.monotonic() + budget
    out = []

    for item in items:
        if len(item.body or "") >= THIN_BODY or not item.url:
            out.append(item)
            continue

        counters["thin"] += 1
        if time.monotonic() >= deadline:
            counters["skipped"] += 1
            out.append(item)
            continue

        text = article_text(item.url, fetcher)
        if len(text) > len(item.body or ""):
            counters["fetched"] += 1
            out.append(replace(item, body=text))
        else:
            out.append(item)

    if stats is not None:
        stats.update(counters)
    return out
