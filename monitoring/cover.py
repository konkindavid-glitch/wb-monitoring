"""Обложка к карточке: заголовок на фирменном фоне.

Генерируется локально через Pillow — бесплатно, предсказуемо, без обращений
к внешним сервисам. Цвет фона зависит от полосы очереди, так что срочное
видно раньше, чем прочитан текст.

Шрифт лежит в репозитории намеренно: в контейнере Амверы Linux, шрифтов
Windows там нет. Взят DejaVu Sans — полная кириллица и лицензия, прямо
разрешающая распространение вместе с программой, в отличие от Arial
и Segoe UI, которые в публичный репозиторий класть нельзя.
"""
import io
from pathlib import Path

WIDTH, HEIGHT = 1280, 720
MARGIN = 90

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Порядок поиска: сначала положенное в репозиторий, потом системные пути.
# Debian кладёт DejaVu именно так, и в образе Амверы он может уже быть.
FONT_CANDIDATES = [
    (FONTS_DIR / "DejaVuSans-Bold.ttf", FONTS_DIR / "DejaVuSans.ttf"),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
     Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
    (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
     Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")),
    (Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
     Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf")),
]

_CACHED = None


def _renders_cyrillic(path) -> bool:
    """Есть ли в шрифте настоящая кириллица.

    Проверять наличие глифа по getmask(...).getbbox() нельзя: у квадрата
    .notdef bbox тоже есть, и проверка проходит ложно — на этом я уже
    обжёгся, обложка ушла с квадратами вместо букв. Надёжный способ —
    сравнить растр буквы с растром символа из приватной зоны, которого
    в шрифте заведомо нет.
    """
    from PIL import Image, ImageDraw, ImageFont

    def raster(char):
        font = ImageFont.truetype(str(path), 48)
        image = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(image).text((5, 5), char, font=font, fill=255)
        return image.tobytes()

    try:
        notdef = raster("")
        return all(raster(c) != notdef for c in "ЖЯЮДЩ")
    except Exception:
        return False


def find_fonts():
    """Пара (жирный, обычный) с кириллицей или None."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED or None

    for bold, regular in FONT_CANDIDATES:
        if bold.exists() and regular.exists() and _renders_cyrillic(bold):
            _CACHED = (bold, regular)
            return _CACHED
    _CACHED = ()
    return None

# Полоса очереди задаёт цвет: срочное видно до того, как прочитан текст.
BAND_STYLE = {
    "URGENT":  {"top": (127, 29, 29),  "bottom": (24, 24, 27), "accent": (248, 113, 113),
                "label": "СРОЧНО"},
    "QUEUE":   {"top": (120, 83, 12),  "bottom": (24, 24, 27), "accent": (250, 204, 21),
                "label": "В РАБОТУ"},
    "BACKLOG": {"top": (30, 58, 95),   "bottom": (24, 24, 27), "accent": (96, 165, 250),
                "label": "В ЗАПАС"},
    "DROP":    {"top": (39, 39, 42),   "bottom": (24, 24, 27), "accent": (113, 113, 122),
                "label": "ОТБРОШЕНО"},
}
DEFAULT_STYLE = BAND_STYLE["BACKLOG"]

BRAND = "Карта мониторинга"


def fonts_available() -> bool:
    return find_fonts() is not None


def _gradient(draw, width, height, top, bottom):
    for y in range(height):
        t = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))


def _wrap(draw, text, font, max_width):
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render(hit: dict):
    """Обложка в PNG или None, если подходящего шрифта нет.

    None вместо исключения намеренно: отсутствие шрифта не повод не доставить
    находку — карточка уйдёт текстом.
    """
    from PIL import Image, ImageDraw, ImageFont

    found = find_fonts()
    if not found:
        return None
    font_bold, font_regular = found

    style = BAND_STYLE.get(hit.get("decision"), DEFAULT_STYLE)
    image = Image.new("RGB", (WIDTH, HEIGHT), style["bottom"])
    draw = ImageDraw.Draw(image)
    _gradient(draw, WIDTH, HEIGHT, style["top"], style["bottom"])

    title_font = ImageFont.truetype(str(font_bold), 62)
    label_font = ImageFont.truetype(str(font_bold), 30)
    small_font = ImageFont.truetype(str(font_regular), 26)

    # Плашка полосы и баллы
    label = f"{style['label']} · {hit.get('score', '?')}"
    draw.rectangle([MARGIN, MARGIN, MARGIN + 14, MARGIN + 44],
                   fill=style["accent"])
    draw.text((MARGIN + 34, MARGIN + 4), label, font=label_font,
              fill=style["accent"])

    # Заголовок: кегль уменьшается, пока не влезет в пять строк
    title = hit.get("title", "без заголовка")
    for size in (62, 54, 46, 40, 34):
        title_font = ImageFont.truetype(str(font_bold), size)
        lines = _wrap(draw, title, title_font, WIDTH - MARGIN * 2)
        if len(lines) <= 5:
            break
    lines = lines[:5]

    y = MARGIN + 130
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=(244, 244, 245))
        y += int(title_font.size * 1.28)

    draw.line([(MARGIN, HEIGHT - 118), (WIDTH - MARGIN, HEIGHT - 118)],
              fill=(63, 63, 70), width=2)
    draw.text((MARGIN, HEIGHT - 96), BRAND, font=small_font, fill=(161, 161, 170))

    platforms = ", ".join(hit.get("platforms") or [])
    if platforms:
        width = draw.textlength(platforms, font=small_font)
        draw.text((WIDTH - MARGIN - width, HEIGHT - 96), platforms,
                  font=small_font, fill=(161, 161, 170))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
