"""Разбор массивов, приходящих из Postgres.

Драйвер отдаёт массив перечисления строкой «{A,B}» — адаптера для
monitoring_platform[] у него нет. list() резал такую строку посимвольно,
и в запрос уходил массив из «{», «W», «I»…, на что Postgres отвечал
`invalid input value for enum monitoring_platform: "{"`. Та же строка тихо
ломала обложку: platforms[0] был «{», и площадка всегда падала в запасной
стиль вместо фирменного цвета.
"""
from monitoring.db import ARRAY_COLUMNS, as_list, rows_to_dicts


class Column:
    def __init__(self, name):
        self.name = name


class Cursor:
    def __init__(self, columns, rows):
        self.description = [Column(c) for c in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows


def test_postgres_array_string_becomes_a_list():
    assert as_list("{WILDBERRIES,OZON}") == ["WILDBERRIES", "OZON"]


def test_single_element_array_is_not_split_into_characters():
    """Ровно та ошибка: list('{WILDBERRIES}') давал ['{', 'W', 'I', ...]."""
    assert as_list("{WILDBERRIES}") == ["WILDBERRIES"]


def test_empty_array_and_none_give_nothing():
    assert as_list("{}") == []
    assert as_list(None) == []


def test_real_list_passes_through():
    assert as_list(["OZON"]) == ["OZON"]


def test_quoted_elements_are_unwrapped():
    """Postgres берёт элемент в кавычки, если в нём есть запятая или пробел."""
    assert as_list('{"OZON","YANDEX MARKET"}') == ["OZON", "YANDEX MARKET"]


def test_rows_are_parsed_column_by_column():
    cursor = Cursor(["hit_id", "platforms", "topics", "title"],
                    [("h1", "{OZON}", "{seller_money,logistics}", "Заголовок")])
    row = rows_to_dicts(cursor)[0]
    assert row["platforms"] == ["OZON"]
    assert row["topics"] == ["seller_money", "logistics"]
    assert row["title"] == "Заголовок"


def test_non_array_columns_are_left_alone():
    cursor = Cursor(["title"], [("{не массив, а текст со скобками}",)])
    assert rows_to_dicts(cursor)[0]["title"] == "{не массив, а текст со скобками}"


def test_missing_array_column_is_not_invented():
    cursor = Cursor(["hit_id"], [("h1",)])
    assert "platforms" not in rows_to_dicts(cursor)[0]


def test_every_array_column_is_covered():
    assert set(ARRAY_COLUMNS) == {"platforms", "topics", "categories"}
