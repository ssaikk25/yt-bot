# -*- coding: utf-8 -*-
"""EXPANSION WAVE 2 — 900 дополнительных discovery-запросов.

Отдельный пул (не трогает существующие BASE/EXPANSION/EXPERIMENT/EXPANSION_950).
Загружается из `config/expansion_wave2.yaml`.

Категории в YAML записаны в свободной форме («Съёмки с дронов, GoPro», «Цветы»,
«Сад / огород», «Аниме / анимация» …). При загрузке приводим их к каноническим
названиям проекта, чтобы работал существующий фильтр `EXCLUDED_QUERY_CATEGORIES`.

ВАЖНО: запросы категории «Аниме / анимация» ОСТАЮТСЯ в YAML, но приводятся к
канонической «Аниме/Анимации» и потому отсекаются действующим фильтром проекта.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .category_mapper import canonicalize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WAVE2_YAML = PROJECT_ROOT / "config" / "expansion_wave2.yaml"

# Категории, которых нет в ALIASES/canonicalize, но которые нужно отсечь
# существующим фильтром проекта.
_CATEGORY_FIX = {
    "аниме / анимация": "Аниме/Анимации",
    "аниме": "Аниме/Анимации",
}


def load_expansion_wave2(path: str | Path | None = None) -> list[dict]:
    """Читает YAML волны 2 и нормализует поля к формату проекта."""
    p = Path(path) if path else WAVE2_YAML
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("query", "")).strip()
        if not text:
            continue
        raw_cat = str(item.get("category", "")).strip()
        fixed = _CATEGORY_FIX.get(raw_cat.lower())
        cat = fixed or canonicalize(raw_cat) or raw_cat
        out.append({
            "query": text,
            "language": str(item.get("language", "")).strip().lower(),
            "category": cat,
            "subtheme": str(item.get("subtheme", "") or "").strip(),
            "method": str(item.get("method", "") or "").strip(),
            "pool": "expansion_wave2",
        })
    return out


EXPANSION_WAVE2: list[dict] = load_expansion_wave2()
