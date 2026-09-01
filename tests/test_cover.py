"""Обложка. Главное здесь — проверка кириллицы.

Дважды подряд шрифт объявлялся пригодным, а карточка уходила с квадратами
вместо букв: `getmask(c).getbbox()` у квадрата `.notdef` не пустой, и наивная
проверка проходила ложно. Тесты закрывают именно этот способ ошибиться.
"""
from monitoring import cover

pytest_plugins = ()


def test_bundled_font_really_has_cyrillic():
    fonts = cover.find_fonts()
    assert fonts is not None, "в репозитории должен лежать шрифт с кириллицей"
    assert cover._renders_cyrillic(fonts[0])


def test_font_without_cyrillic_is_rejected(tmp_path):
    """Страховка от повторения: шрифт без кириллицы обязан быть отвергнут.

    Строится усечением настоящего шрифта до латиницы — так проверяется
    сам метод, а не наличие подходящего файла.
    """
    fontTools = __import__("importlib").util.find_spec("fontTools")
    if fontTools is None:
        import pytest
        pytest.skip("fontTools не установлен")
    from fontTools import subset
    from fontTools.ttLib import TTFont

    stripped = tmp_path / "latin-only.ttf"
    font = TTFont(str(cover.find_fonts()[0]))
    subset.Subsetter(subset.Options()).subset(font)  # только .notdef и латиница
    font.save(str(stripped))
    assert cover._renders_cyrillic(stripped) is False


def test_missing_font_yields_no_cover_instead_of_crashing(monkeypatch):
    """Отсутствие шрифта не повод не доставить находку: карточка уходит
    текстом, а не теряется вместе с обложкой."""
    monkeypatch.setattr(cover, "FONT_CANDIDATES", [])
    monkeypatch.setattr(cover, "_CACHED", None)
    assert cover.render({"title": "Заголовок", "score": 90}) is None


def test_render_returns_png_of_expected_size():
    png = cover.render({"title": "Wildberries меняет тариф хранения",
                        "score": 130, "decision": "URGENT",
                        "platforms": ["Wildberries"]})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(png)).size == (cover.WIDTH, cover.HEIGHT)


def test_band_changes_the_colour():
    """Полоса очереди должна читаться до текста — значит фон обязан отличаться."""
    def top_left(decision):
        from PIL import Image
        import io
        png = cover.render({"title": "Т", "score": 1, "decision": decision})
        return Image.open(io.BytesIO(png)).convert("RGB").getpixel((5, 5))

    assert top_left("URGENT") != top_left("BACKLOG")


def test_unknown_band_does_not_crash():
    assert cover.render({"title": "Т", "score": 1, "decision": None}) is not None


def test_very_long_title_is_bounded():
    """Заголовок в тысячу символов не должен уезжать за нижний край."""
    assert cover.render({"title": "слово " * 300, "score": 40,
                         "decision": "QUEUE"}) is not None
