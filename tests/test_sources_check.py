"""Проверка источников: та, что запускается из чата.

Проверять источники с машины разработки бесполезно — сеть здесь режет
половину адресов, и рабочий oborot возвращает ноль наравне с мёртвыми.
Правду знает только контейнер.
"""
import app
from monitoring.collectors.base import FetchResult
from monitoring.collectors.onboarding import MIN_PAGE_TEXT, validate_source

SPA = "<html><head>" + ("<meta/>" * 400) + "</head><body>Wildberries</body></html>"
PAGE = "<html><body><article>" + ("Настоящий текст страницы. " * 60) + \
       "</article></body></html>"


class Fetcher:
    def __init__(self, html, status=200):
        self.html, self.status = html, status

    def get(self, url, etag=None, last_modified=None):
        return FetchResult(self.status, self.html)


def check(html, status=200):
    from datetime import datetime, timezone
    return validate_source({"key": "s", "url": "https://x.invalid/",
                            "method": "doc_diff"},
                           Fetcher(html, status),
                           datetime(2026, 9, 3, tzinfo=timezone.utc), {})


def test_single_page_app_is_not_mistaken_for_a_live_source():
    """seller.wildberries.ru отдаёт 200 и 24 КБ разметки, а текста в ней
    11 знаков. Прежняя проверка «есть хоть какой-то текст» такую страницу
    пропускала, и источник месяцами числился рабочим, не давая находок."""
    report = check(SPA)
    assert not report.ok
    assert "без содержания" in report.reason


def test_page_with_real_text_passes():
    assert check(PAGE).ok


def test_unavailable_source_is_reported_with_its_code():
    report = check("", status=403)
    assert not report.ok
    assert "403" in report.reason


def test_threshold_is_high_enough_to_reject_a_shell():
    assert MIN_PAGE_TEXT >= 200


# --- отчёт в чат ------------------------------------------------------------

def test_report_lists_every_source_and_counts_the_living():
    text = app.format_sources([
        ("src_new_retail", True, "элементов: 40"),
        ("src_wb_seller_news", False, "страница без содержания: 11 знаков"),
    ])
    assert "src_new_retail" in text
    assert "src_wb_seller_news" in text
    assert "Живых: 1 из 2" in text


def test_report_fits_a_telegram_message():
    reports = [(f"src_{i}", False, "п" * 200) for i in range(60)]
    assert len(app.format_sources(reports)) <= 4096


def test_broken_check_does_not_lose_the_rest(monkeypatch):
    """Один упавший источник не должен отменить проверку остальных."""
    class Cfg:
        def source_list(self):
            return [{"key": "плохой", "url": "x", "method": "rss"},
                    {"key": "хороший", "url": "y", "method": "doc_diff"}]

        def onboarding_cfg(self):
            return {}

    def boom(source, fetcher, now, cfg):
        if source["key"] == "плохой":
            raise RuntimeError("разбор упал")
        from monitoring.collectors.onboarding import OnboardingReport
        return OnboardingReport(True, "", {})

    monkeypatch.setattr("monitoring.collectors.onboarding.validate_source", boom)
    reports = app.check_sources(Cfg(), Fetcher(PAGE))

    assert [r[0] for r in reports] == ["плохой", "хороший"]
    assert reports[0][1] is False and "разбор упал" in reports[0][2]
    assert reports[1][1] is True
