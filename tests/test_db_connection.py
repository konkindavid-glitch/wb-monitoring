"""Долгоживущее соединение не должно закрываться само.

psycopg локально не установлен — он нужен только в Амвере, — поэтому модуль
подменяется заглушкой. Проверяется не работа с базой, а форма владения
соединением: именно она дала дефект, который на боевом запуске выглядел как
«the connection is closed» на первой же операции.
"""
import gc
import sys
import types

import pytest


@pytest.fixture
def db(monkeypatch):
    """Подсовывает заглушку psycopg и отдаёт свежий monitoring.db."""
    class FakeConn:
        def __init__(self, **kw):
            self.closed = False
            self.kwargs = kw
            self.autocommit = kw.get("autocommit", False)

        def close(self):
            self.closed = True

        def commit(self):
            pass

        def rollback(self):
            pass

    created = []

    def fake_connect(dsn=None, **kw):
        conn = FakeConn(dsn=dsn, **kw)
        created.append(conn)
        return conn

    fake = types.ModuleType("psycopg")
    fake.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.delitem(sys.modules, "monitoring.db", raising=False)

    import importlib
    module = importlib.import_module("monitoring.db")
    module._created = created
    return module


def test_open_connection_survives_garbage_collection(db):
    """Главный тест.

    Соединение бралось как connect(...).__enter__(), но ссылку на сам генератор
    никто не держал. Сборщик мусора убирал генератор, в yield прилетал
    GeneratorExit, срабатывал finally с conn.close() — и соединение умирало
    ещё до первого запроса.
    """
    conn = db.open_connection(dsn="postgresql://x")
    gc.collect()
    gc.collect()
    assert not conn.closed, "соединение закрылось само — вернулась старая ошибка"


def test_open_connection_accepts_separate_parts(db):
    conn = db.open_connection(host="h", user="u", password="п@роль", dbname="d")
    assert conn.kwargs["password"] == "п@роль"
    assert conn.kwargs["host"] == "h"


def test_open_connection_prefers_dsn_when_given(db):
    conn = db.open_connection("postgresql://x", host="ignored")
    assert conn.kwargs["dsn"] == "postgresql://x"


def test_context_manager_still_closes_on_exit(db):
    """connect() остаётся контекстменеджером для короткоживущих операций."""
    with db.connect(dsn="postgresql://x") as conn:
        assert not conn.closed
    assert conn.closed, "контекстменеджер обязан закрывать соединение"


def test_context_manager_rolls_back_on_error(db):
    rolled = []

    with pytest.raises(RuntimeError):
        with db.connect(dsn="postgresql://x") as conn:
            conn.rollback = lambda: rolled.append(True)
            raise RuntimeError("сбой внутри транзакции")

    assert rolled, "при ошибке транзакция должна откатываться"
    assert conn.closed
