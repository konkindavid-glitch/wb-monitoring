"""Согласие запросов в коде со схемой в миграции.

Пересчёт сорвался в бою на строке `invalid input syntax for type bigint:
"tr_4c544aaed2b042e2"`: я подставил в INSERT колонку transition_id, не
сверившись с её типом. Она bigserial — номер выдаёт база.

Модульные тесты такое не ловят: у поддельного репозитория схемы нет.
Поэтому проверка сверяет тексты запросов с самой миграцией — и ловит
не одну эту ошибку, а весь её класс.
"""
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL = io.open(ROOT / "sql" / "001_monitoring_map.sql", encoding="utf-8").read()
CODE = io.open(ROOT / "monitoring" / "db.py", encoding="utf-8").read()

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                    re.S)
_INSERT = re.compile(r"INSERT INTO (\w+)\s*\(([^)]*)\)", re.S)


def generated_columns() -> dict:
    """Колонки, которые заполняет сама база: serial и bigserial."""
    out = {}
    for table, body in _TABLE.findall(SQL):
        columns = set()
        for line in body.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].lower() in ("serial", "bigserial"):
                columns.add(parts[0])
        if columns:
            out[table] = columns
    return out


def inserts() -> list:
    """(таблица, перечисленные колонки) для каждого INSERT в db.py."""
    out = []
    for table, raw in _INSERT.findall(CODE):
        columns = {c.strip() for c in raw.replace("\n", " ").split(",")}
        out.append((table, {c for c in columns if c}))
    return out


def test_migration_has_generated_columns_to_check():
    """Если их не стало, проверка молча перестанет что-либо проверять."""
    assert generated_columns()


def test_no_insert_fills_a_generated_column():
    """Колонку bigserial заполняет база. Подставленный текстовый
    идентификатор уронил пересчёт целиком."""
    generated = generated_columns()
    for table, columns in inserts():
        clash = columns & generated.get(table, set())
        assert not clash, f"{table}: {sorted(clash)} заполняет база"


def test_every_insert_targets_a_known_table():
    """Опечатка в имени таблицы иначе всплывёт только в бою."""
    tables = {name for name, _ in _TABLE.findall(SQL)}
    for table, _ in inserts():
        assert table in tables, table


def test_inserted_columns_exist_in_the_migration():
    """Колонка, которой нет в схеме, — та же ошибка, только с другим
    текстом: column ... does not exist."""
    known = {}
    for table, body in _TABLE.findall(SQL):
        names = set()
        for line in body.splitlines():
            parts = line.strip().split()
            if parts and parts[0].isidentifier() and \
                    parts[0].upper() not in ("CONSTRAINT", "PRIMARY", "UNIQUE",
                                             "FOREIGN", "CHECK"):
                names.add(parts[0])
        known[table] = names

    # Раздел 11 добавляет колонку через ALTER — учитываем и такие.
    for table, column in re.findall(
            r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", SQL):
        known.setdefault(table, set()).add(column)

    for table, columns in inserts():
        missing = columns - known.get(table, set())
        assert not missing, f"{table}: нет колонок {sorted(missing)}"
