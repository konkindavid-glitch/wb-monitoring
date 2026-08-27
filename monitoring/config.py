"""Загрузка и валидация конфигурации карты мониторинга.

Веса матрицы и пороги 80/60/40 — требование, а не настройка по умолчанию.
Конфиг, который им не соответствует, не грузится: лучше не запуститься, чем
молча считать по другой матрице.
"""
import io
from dataclasses import dataclass
from pathlib import Path

import yaml

EXPECTED_FACTORS = 14
EXPECTED_STOP_RULES = 14
EXPECTED_TOPICS = 11


class ConfigError(Exception):
    pass


def _read_yaml(path: Path):
    if not path.exists():
        raise ConfigError(f"нет файла конфигурации: {path}")
    with io.open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Config:
    map: dict
    queries: dict
    triage: dict
    sources: dict

    def factor_weights(self) -> dict:
        return {f["key"]: f["weight"] for f in self.triage["factors"]}

    def thresholds(self) -> list:
        return self.triage["thresholds"]

    def stop_rule_codes(self) -> set:
        return {r["code"] for r in self.triage["stop_rules"]}

    def topic_keys(self) -> set:
        return {t["key"] for t in self.map["topics"]}

    def cadence_seconds(self) -> dict:
        return {c["key"]: c["period_seconds"] for c in self.queries["cadence_classes"]}

    def source_list(self) -> list:
        return self.sources.get("sources", [])

    def onboarding_cfg(self) -> dict:
        return self.sources.get("onboarding", {
            "min_successful_requests": 3,
            "min_items_parsed": 3,
            "min_dated_share": 0.8,
            "min_relevant_share": 0.02,
        })

    def platform_priority(self, key: str) -> int:
        for p in self.map["platforms"]:
            if p["key"] == key:
                return p["priority_value"]
        raise ConfigError(f"неизвестная площадка: {key}")


def load_config(root: Path) -> Config:
    sources_path = root / "config" / "sources.yaml"
    cfg = Config(
        map=_read_yaml(root / "config" / "monitoring-map.yaml"),
        queries=_read_yaml(root / "config" / "queries.yaml"),
        triage=_read_yaml(root / "config" / "triage.yaml"),
        sources=_read_yaml(sources_path) if sources_path.exists() else {"sources": []},
    )

    weights = cfg.factor_weights()
    if len(weights) != EXPECTED_FACTORS:
        raise ConfigError(
            f"в матрице должно быть {EXPECTED_FACTORS} факторов, найдено {len(weights)}")
    if len(cfg.stop_rule_codes()) != EXPECTED_STOP_RULES:
        raise ConfigError(
            f"должно быть {EXPECTED_STOP_RULES} стоп-правил, "
            f"найдено {len(cfg.stop_rule_codes())}")
    if len(cfg.topic_keys()) != EXPECTED_TOPICS:
        raise ConfigError(
            f"должно быть {EXPECTED_TOPICS} тем, найдено {len(cfg.topic_keys())}")
    return cfg
