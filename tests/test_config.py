from pathlib import Path

import pytest

from monitoring.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_loads_all_configs():
    cfg = load_config(ROOT)
    assert len(cfg.factor_weights()) == 14
    assert cfg.factor_weights()["platform_wb"] == 25
    assert cfg.factor_weights()["no_confirmation"] == -50
    assert len(cfg.stop_rule_codes()) == 14
    assert len(cfg.topic_keys()) == 11


def test_platform_priorities_match_the_map():
    cfg = load_config(ROOT)
    assert cfg.platform_priority("WILDBERRIES") == 25
    assert cfg.platform_priority("OZON") == 15
    assert cfg.platform_priority("YANDEX_MARKET") == 10
    with pytest.raises(ConfigError, match="неизвестная площадка"):
        cfg.platform_priority("NOT_A_PLATFORM")


def test_thresholds_are_the_ones_from_the_requirements():
    bands = load_config(ROOT).thresholds()
    assert [b["decision"] for b in bands] == ["URGENT", "QUEUE", "BACKLOG", "DROP"]
    assert [b["min_score"] for b in bands[:3]] == [80, 60, 40]


def test_cadence_class_a_is_five_minutes():
    assert load_config(ROOT).cadence_seconds()["A"] == 300


def test_rejects_matrix_with_wrong_factor_count(tmp_path):
    """Матрица из 13 факторов — требование нарушено, грузить нельзя."""
    (tmp_path / "config").mkdir()
    for name in ("monitoring-map.yaml", "queries.yaml"):
        (tmp_path / "config" / name).write_text(
            (ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")

    triage = (ROOT / "config" / "triage.yaml").read_text(encoding="utf-8")
    triage = triage.replace(
        '  - key: is_advertising\n    title: "Рекламный материал"\n    weight: -60\n', "")
    (tmp_path / "config" / "triage.yaml").write_text(triage, encoding="utf-8")

    with pytest.raises(ConfigError, match="14"):
        load_config(tmp_path)


def test_missing_config_file_is_reported_clearly(tmp_path):
    with pytest.raises(ConfigError, match="нет файла конфигурации"):
        load_config(tmp_path)
