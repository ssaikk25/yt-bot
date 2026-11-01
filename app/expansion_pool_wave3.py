# -*- coding: utf-8 -*-
"""EXPANSION WAVE 3 — дополнительный пул discovery-запросов.

Отдельный пул (не трогает BASE/EXPANSION/EXPERIMENT/EXPANSION_950/WAVE_2).
Загружается из `config/expansion_wave3.yaml`.

Категории приводятся к каноническим названиям проекта; «Аниме / анимация»
мапится в «Аниме/Анимации» и отсекается существующим фильтром.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .category_mapper import canonicalize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WAVE3_YAML = PROJECT_ROOT / "config" / "expansion_wave3.yaml"

_CATEGORY_FIX = {
    "аниме / анимация": "Аниме/Анимации",
    "аниме": "Аниме/Анимации",
}


def load_expansion_wave3(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else WAVE3_YAML
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
            "pool": "expansion_wave3",
        })
    return out


EXPANSION_WAVE3: list[dict] = load_expansion_wave3()
